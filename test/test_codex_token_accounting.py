"""Codex token accounting — session/project totals from ``token_count`` events.

Codex records token usage as *cumulative* ``token_count`` events (one after
nearly every agent-loop step), not as per-assistant-message ``usage`` the way
Claude does. This module pins the extraction and the wholesale/index threading:

- the mapping onto the index's four token columns (THE subtraction that must
  stay disjoint, or index totals double-count the cache-read column),
- "session total = the LAST cumulative record", never a sum of the per-step
  deltas (the double-count main flagged), holding across compaction,
- the degenerate-record rule (zero components, non-zero total → stored total
  is authoritative, never recomputed),
- totals OMITTED (``None``) — not zeroed — for pre-accounting sessions,
- the wholesale path populating project-card and per-session-row totals while
  never leaking account-level ``rate_limits`` data into output.

Each test is written so neutering its target guard turns it RED (e.g. folding
``cached`` back into ``input`` breaks ``test_map_subtraction_is_disjoint``).
"""

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from claude_code_log.converter import (
    _sum_provider_token_totals,
    render_provider_wholesale,
)
import pytest

from claude_code_log.models import TranscriptEntry
from claude_code_log.providers.base import (
    BaseProvider,
    ProviderTokenTotals,
    SessionInfo,
)
from claude_code_log.providers.codex import (
    CodexProvider,
    _DecodedRecord,
    _map_cumulative_usage,
    _token_totals_from_records,
)


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------
def _usage(
    input_tokens: int,
    cached: int,
    output: int,
    reasoning: int,
    total: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
    }


def _token_count_record(total_usage: dict[str, int], ts: str) -> dict[str, object]:
    """A ``token_count`` event carrying a cumulative ``total_token_usage`` plus
    the account-level ``rate_limits`` block a real rollout attaches (so the
    no-leak pin exercises the real shape)."""
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": total_usage,
                "last_token_usage": total_usage,
                "model_context_window": 258400,
            },
            "rate_limits": {
                "limit_id": "codex",
                "plan_type": "team",
                "primary": {"used_percent": 92.0, "resets_at": 1786983340},
            },
        },
    }


def _rollout_with_tokens(
    tmp: Path,
    rel: str,
    thread_id: str,
    cwd: str | None,
    token_totals: list[dict[str, Any]],
) -> Path:
    """Write a rollout whose token_count events carry the given cumulative
    totals in order (the LAST one is the session total).

    Values are ``Any``, not ``int``: the malformed-record tests deliberately
    feed a string / bool / absent ``total_tokens`` to exercise the skip, and a
    fixture builder that could only express well-formed input could not
    express the bug being pinned.
    """
    payload: dict[str, object] = {"id": thread_id, "timestamp": "2026-01-02T00:00:00Z"}
    if cwd is not None:
        payload["cwd"] = cwd
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "type": "session_meta",
            "payload": payload,
        },
        {
            "timestamp": "2026-01-02T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"hi {thread_id[:4]}"},
        },
    ]
    for i, tu in enumerate(token_totals):
        records.append(
            {
                "timestamp": "2026-01-02T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": f"step {i}"},
            }
        )
        # Zero-padded: an unpadded f"…:0{3 + i}" emits "00:00:010Z" from the
        # 7th record on, and this fixture is load-bearing for tests that
        # assert on the literal timestamp.
        records.append(_token_count_record(tu, f"2026-01-02T00:00:{3 + i:02d}Z"))
    rel_path = Path(rel)
    path = tmp / rel_path.parent / f"rollout-{rel_path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _decoded(records_source: Path) -> list[_DecodedRecord]:
    """Decoded records, typed as what ``_decode_records`` yields.

    Returning ``list[object]`` forced an ``arg-type`` suppression at every call
    site, which meant the type checkers could not verify the very contract these
    tests exist to pin.
    """
    return list(CodexProvider()._decode_records(records_source))


# --------------------------------------------------------------------------
# _map_cumulative_usage — the mapping onto index columns
# --------------------------------------------------------------------------
def test_no_token_field_accepts_a_boolean() -> None:
    """``bool`` is an ``int`` subclass, so a JSON ``true`` must not become a 1
    in any token column.

    Raised in review for the *component* fields after the same exclusion had
    been added for ``total_tokens`` only — the record-selection guard rejects a
    boolean total, but a record with a well-formed total and a boolean
    ``input_tokens`` reaches the mapping untouched. One supplier fixed, the
    others left green: fixing only the field named in the report would repeat
    exactly that.

    So the loop derives its field list from the usage dict itself rather than
    naming fields inline. Add a component to ``_usage``/the mapping and it is
    covered here automatically; an inline list of four names would silently
    stop covering the fifth.
    """
    baseline = _usage(100, 20, 10, 0, 110)

    for field in baseline:
        poisoned = {**baseline, field: True}
        totals = _map_cumulative_usage(poisoned)
        assert 1 not in (
            totals.input_tokens,
            totals.cache_read_tokens,
            totals.output_tokens,
            totals.total_tokens,
        ), f"boolean in {field!r} produced a phantom 1: {totals}"

    # And False must not read as a legitimate zero-from-data either — same
    # coercion, opposite value, and it would silently zero a real column.
    for field in baseline:
        totals = _map_cumulative_usage({**baseline, field: False})
        assert isinstance(totals.total_tokens, int)


def test_map_subtraction_is_disjoint() -> None:
    """THE double-count pin: billable input EXCLUDES the cached portion, and
    cache_read is the SOLE home of the cached tokens. If a future edit folded
    ``cached`` back into input (or dropped the subtraction), input+cache_read
    would exceed the original input_tokens and index totals would balloon."""
    result = _map_cumulative_usage(_usage(100, 30, 50, 20, 150))
    assert result.input_tokens == 70  # 100 - 30, NOT 100 and NOT 130
    assert result.cache_read_tokens == 30
    # input + cache_read reconstructs the original input_tokens exactly — the
    # cached tokens are counted once, in cache_read only.
    assert result.input_tokens + result.cache_read_tokens == 100


def test_map_output_includes_reasoning_not_added() -> None:
    """output_tokens already subsumes reasoning_output_tokens — reasoning must
    not be added a second time."""
    result = _map_cumulative_usage(_usage(100, 0, 50, 20, 150))
    assert result.output_tokens == 50  # NOT 50 + 20


def test_map_reconstructs_total_for_wellformed() -> None:
    """For a well-formed cumulative record the mapped columns reconstruct the
    stored total: (input-cached) + cached + output == total."""
    result = _map_cumulative_usage(_usage(100, 30, 50, 20, 150))
    assert (
        result.input_tokens + result.cache_read_tokens + result.output_tokens
        == result.total_tokens
        == 150
    )


def test_map_total_authoritative_for_degenerate_record() -> None:
    """Degenerate record — every component zero but a non-zero total. The
    stored total is authoritative and carried through untouched; it is NEVER
    recomputed from the (zero) components down to zero."""
    result = _map_cumulative_usage(_usage(0, 0, 0, 0, 4242))
    assert result.total_tokens == 4242
    assert result.input_tokens == 0
    assert result.cache_read_tokens == 0
    assert result.output_tokens == 0


def test_map_clamps_negative_input() -> None:
    """A malformed record where cached > input must not yield a negative
    billable-input column."""
    result = _map_cumulative_usage(_usage(10, 40, 5, 0, 15))
    assert result.input_tokens == 0


def test_map_coerces_missing_fields_to_zero() -> None:
    result = _map_cumulative_usage({"total_tokens": 7})
    assert result == ProviderTokenTotals(
        input_tokens=0, cache_read_tokens=0, output_tokens=0, total_tokens=7
    )


# --------------------------------------------------------------------------
# _token_totals_from_records — last cumulative record wins, never a sum
# --------------------------------------------------------------------------
def test_last_record_wins_not_sum(tmp_path: Path) -> None:
    """Session total is the LAST cumulative record, NOT a sum of the per-step
    records. Three monotonically-growing cumulative records → the result is the
    third, not their sum."""
    path = _rollout_with_tokens(
        tmp_path,
        "s/one.jsonl",
        "10000000-0000-4000-8000-000000000001",
        "/p",
        [
            _usage(100, 20, 10, 0, 110),
            _usage(300, 60, 25, 0, 325),
            _usage(500, 100, 40, 0, 540),  # <- the session total
        ],
    )
    result = _token_totals_from_records(_decoded(path))
    assert result is not None
    assert result.total_tokens == 540  # last record, not 110+325+540
    assert result.input_tokens == 400  # 500 - 100
    assert result.cache_read_tokens == 100
    assert result.output_tokens == 40


def test_last_record_wins_across_compaction(tmp_path: Path) -> None:
    """Compaction lowers the live context window but the cumulative counter
    keeps climbing. Even when a mid-session record's per-step delta looks like a
    reset, the LAST cumulative total_token_usage is still the session total."""
    path = _rollout_with_tokens(
        tmp_path,
        "s/two.jsonl",
        "10000000-0000-4000-8000-000000000002",
        "/p",
        [
            _usage(1000, 200, 50, 10, 1050),
            _usage(2000, 400, 90, 20, 2090),  # pre-compaction
            _usage(2100, 1800, 95, 22, 2195),  # post-compaction: cumulative up
        ],
    )
    result = _token_totals_from_records(_decoded(path))
    assert result is not None
    assert result.total_tokens == 2195  # last cumulative record, monotonic


def test_none_when_no_token_count(tmp_path: Path) -> None:
    """A pre-accounting session (no token_count events) yields None — totals
    are OMITTED, not zeroed."""
    path = _rollout_with_tokens(
        tmp_path,
        "s/three.jsonl",
        "10000000-0000-4000-8000-000000000003",
        "/p",
        [],  # no token_count records
    )
    assert _token_totals_from_records(_decoded(path)) is None


def test_monotonicity_violation_omits_totals(tmp_path: Path) -> None:
    """Cumulative total_tokens must never decrease. If it does (a hypothetical
    future counter reset mid-session), the totals are OMITTED — not the
    post-reset tail ('last', which understates) nor the pre-reset peak ('max',
    which is also wrong). Fail closed: a wrong number is worse than an absent
    one. Nothing in the corpus exercises this (0 violations measured), so the
    guard fires only on a spec change."""
    path = _rollout_with_tokens(
        tmp_path,
        "s/reset.jsonl",
        "10000000-0000-4000-8000-000000000004",
        "/p",
        [
            _usage(1000, 200, 50, 10, 1050),
            _usage(2000, 400, 90, 20, 2090),
            _usage(300, 60, 20, 5, 320),  # DECREASE: 320 < 2090 → omit
        ],
    )
    assert _token_totals_from_records(_decoded(path)) is None


# --------------------------------------------------------------------------
# session_token_totals — provider seam, post-strip consistency
# --------------------------------------------------------------------------
def test_session_token_totals_matches_last_record(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _rollout_with_tokens(
        root,
        "a/one.jsonl",
        "10000000-0000-4000-8000-000000000001",
        "/proj/a",
        [_usage(200, 50, 20, 5, 220), _usage(400, 120, 35, 8, 435)],
    )
    totals = CodexProvider().session_token_totals(
        root, "10000000-0000-4000-8000-000000000001"
    )
    assert totals == ProviderTokenTotals(
        input_tokens=280, cache_read_tokens=120, output_tokens=35, total_tokens=435
    )


def test_default_seam_returns_none() -> None:
    """The base seam default is None so non-cumulative providers leave every
    token surface untouched. Agy inherits the default."""
    from claude_code_log.providers.agy import AgyProvider

    assert AgyProvider().session_token_totals(Path("/nonexistent"), "whatever") is None


# --------------------------------------------------------------------------
# converter helpers
# --------------------------------------------------------------------------
def test_sum_skips_none_and_pins_cache_creation_zero() -> None:
    """Summing across sessions: None (pre-accounting) sessions contribute
    nothing; cache_creation stays 0 for shape parity (never displayed)."""
    summed = _sum_provider_token_totals(
        [
            ProviderTokenTotals(70, 30, 50, 150),
            None,
            ProviderTokenTotals(5, 1, 2, 8),
        ]
    )
    assert summed == {
        "total_input_tokens": 75,
        "total_output_tokens": 52,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 31,
    }


# --------------------------------------------------------------------------
# Wholesale/index threading — the #296 deferral
# --------------------------------------------------------------------------
def _render(tmp_path: Path) -> str:
    root = tmp_path / "sessions"
    # Two sessions in one project, one in another, one pre-accounting session.
    _rollout_with_tokens(
        root,
        "a/one.jsonl",
        "10000000-0000-4000-8000-000000000001",
        "/proj/a",
        [_usage(100, 20, 10, 0, 110), _usage(1000, 200, 100, 0, 1100)],
    )
    _rollout_with_tokens(
        root,
        "a/two.jsonl",
        "10000000-0000-4000-8000-000000000002",
        "/proj/a",
        [_usage(500, 100, 50, 0, 550)],
    )
    _rollout_with_tokens(
        root,
        "b/one.jsonl",
        "20000000-0000-4000-8000-000000000001",
        "/proj/b",
        [_usage(7, 2, 3, 0, 10)],
    )
    _rollout_with_tokens(
        root,
        "c/none.jsonl",
        "30000000-0000-4000-8000-000000000001",
        "/proj/c",
        [],  # pre-accounting: no token_count
    )
    out = tmp_path / "out"
    index = render_provider_wholesale("codex", root, out, use_cache=True, silent=True)
    return index.read_text(encoding="utf-8")


def test_wholesale_project_card_totals_use_last_cumulative(tmp_path: Path) -> None:
    """Project /proj/a: session one's LAST cumulative is 1100 (input 800,
    cache_read 200, output 100), session two is 550 (input 400, cache_read 100,
    output 50). The card sums the two SESSION totals — and each session total is
    its last cumulative, NOT the sum of its per-step records (else session one
    would be 110+1100)."""
    html = _render(tmp_path)
    # /proj/a card: input 800+400=1200, output 100+50=150, cache_read 200+100=300
    assert "Input: 1200 | Output: 150 | Cache Read: 300" in html
    # /proj/b card: input 5, output 3, cache_read 2
    assert "Input: 5 | Output: 3 | Cache Read: 2" in html


def test_wholesale_session_cache_stores_per_session_totals(tmp_path: Path) -> None:
    """Per-session cumulative totals are written to the session cache (the
    durable 'session totals'), each session's LAST cumulative — NOT the summed
    per-message usage (which is zero for Codex) and NOT the per-step sum."""
    from claude_code_log.cache import (
        CacheManager,
        get_cache_db_path,
        get_library_version,
    )

    root = tmp_path / "sessions"
    _rollout_with_tokens(
        root,
        "a/one.jsonl",
        "10000000-0000-4000-8000-000000000001",
        "/proj/a",
        [_usage(100, 20, 10, 0, 110), _usage(1000, 200, 100, 0, 1100)],
    )
    _rollout_with_tokens(
        root,
        "a/two.jsonl",
        "10000000-0000-4000-8000-000000000002",
        "/proj/a",
        [_usage(500, 100, 50, 0, 550)],
    )
    out = tmp_path / "out"
    render_provider_wholesale("codex", root, out, use_cache=True, silent=True)

    # Locate the project dest dir by the session page it rendered, then read its
    # session cache rows directly.
    session_page = next(out.rglob("session-10000000-0000-4000-8000-000000000001*"))
    dest = session_page.parent
    cache = CacheManager(dest, get_library_version(), db_path=get_cache_db_path(out))
    project_cache = cache.get_cached_project_data()
    assert project_cache is not None
    cached = project_cache.sessions
    one = cached["10000000-0000-4000-8000-000000000001"]
    assert one.total_input_tokens == 800  # 1000 - 200, last record (not 110+1100)
    assert one.total_cache_read_tokens == 200
    assert one.total_output_tokens == 100
    assert one.total_cache_creation_tokens == 0  # omitted for Codex
    two = cached["10000000-0000-4000-8000-000000000002"]
    assert two.total_input_tokens == 400
    assert two.total_cache_read_tokens == 100


def test_wholesale_no_zero_token_line_leaks(tmp_path: Path) -> None:
    """The pre-accounting project /proj/c contributes no tokens — no zero-valued
    token figure leaks anywhere, and 'Cache Creation' never renders."""
    html = _render(tmp_path)
    assert "Input: 0" not in html
    assert "Cache Creation" not in html


def test_wholesale_never_leaks_account_level_data(tmp_path: Path) -> None:
    """rate_limits / model_context_window live in the token_count payload but
    must NEVER reach output — only the four token integers are surfaced."""
    html = _render(tmp_path)
    for term in (
        "rate_limit",
        "used_percent",
        "resets_at",
        "model_context_window",
        "plan_type",
        "limit_id",
    ):
        assert term not in html, f"account-level term leaked: {term}"


# --------------------------------------------------------------------------
# Review follow-ups: malformed totals, and the asymmetric project override
# --------------------------------------------------------------------------
# Both were raised in review on the open PR. Neither is reachable from the
# real corpus — every ``token_count`` there carries a valid int total, and the
# only non-Codex provider has no per-message usage to lose — so both need
# synthetic pins.


def test_malformed_total_does_not_omit_the_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A record whose ``total_tokens`` is absent or not an int must be SKIPPED,
    not coerced to 0.

    Coercing made a data-quality problem look like a counter reset: 0 compares
    less than the running total, the monotonicity guard fired, and the whole
    session's totals were omitted. The session here is strictly increasing —
    110 then 1100 — with one malformed record wedged between, so nothing about
    the counter is actually broken.

    Mutation-check: restore ``total = total_raw if isinstance(total_raw, int)
    else 0`` (dropping the skip) and this goes RED — the guard fires on the
    malformed record and ``session_token_totals`` returns None.
    """
    import logging

    records = [
        _usage(100, 20, 10, 0, 110),
        {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 50},
        _usage(1000, 200, 100, 0, 1100),
    ]
    root = tmp_path / "sessions"
    _rollout_with_tokens(
        root, "a/one.jsonl", "10000000-0000-4000-8000-00000000000a", "/proj/a", records
    )

    with caplog.at_level(logging.WARNING):
        totals = CodexProvider().session_token_totals(
            root, "10000000-0000-4000-8000-00000000000a"
        )

    # Not omitted: the valid records still describe the session.
    assert totals is not None
    # And the LAST VALID record wins — the malformed one is out of last-record
    # selection too, so its zero components never reach _map_cumulative_usage.
    assert totals.input_tokens == 800
    assert totals.cache_read_tokens == 200
    assert totals.output_tokens == 100
    assert totals.total_tokens == 1100

    # Degraded visibly, naming what was seen — ONE line per session (a
    # pathological file must not emit thousands), but carrying the count AND
    # the first offending record's timestamp, so the reader can open the
    # rollout at that record instead of re-scanning it. The malformed record
    # is the second token_count, at ...:04Z.
    # Filter and collect through the SAME accessor. ``r.message`` is only
    # populated once a formatter has run over the record, so collecting it while
    # filtering on ``r.getMessage()`` can see something different from what was
    # matched — and only the count is used here anyway, so collect the records.
    warnings = [r for r in caplog.records if "total_tokens was" in r.getMessage()]
    assert len(warnings) == 1
    assert "absent" in caplog.text
    assert "skipped 1 record" in caplog.text
    assert "2026-01-02T00:00:04Z" in caplog.text
    # The monotonicity guard did NOT fire — that message must be absent.
    assert "monotonicity broken" not in caplog.text


def test_malformed_total_names_the_type_it_saw(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning names the actual shape, so the next surprise is diagnosable
    from the log line. A JSON string total reports ``str``.

    ``True`` is reported as ``bool`` rather than silently accepted as the int
    1 — ``bool`` is an ``int`` subclass, so a bare ``isinstance(x, int)``
    would let it through and record a session total of 1.
    """
    import logging

    root = tmp_path / "sessions"
    _rollout_with_tokens(
        root,
        "a/one.jsonl",
        "10000000-0000-4000-8000-00000000000b",
        "/proj/a",
        [
            _usage(100, 20, 10, 0, 110),
            {**_usage(0, 0, 0, 0, 0), "total_tokens": "1200"},
            {**_usage(0, 0, 0, 0, 0), "total_tokens": True},
        ],
    )

    with caplog.at_level(logging.WARNING):
        totals = CodexProvider().session_token_totals(
            root, "10000000-0000-4000-8000-00000000000b"
        )

    assert totals is not None
    assert totals.total_tokens == 110  # the only well-formed record
    assert "bool" in caplog.text
    assert "str" in caplog.text


def test_real_counter_reset_still_omits_totals(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary of the change: skipping malformed records must not have
    disarmed the guard for a GENUINE decrease between two valid records."""
    import logging

    root = tmp_path / "sessions"
    _rollout_with_tokens(
        root,
        "a/one.jsonl",
        "10000000-0000-4000-8000-00000000000c",
        "/proj/a",
        [_usage(1000, 200, 100, 0, 1100), _usage(100, 20, 10, 0, 110)],
    )

    with caplog.at_level(logging.WARNING):
        totals = CodexProvider().session_token_totals(
            root, "10000000-0000-4000-8000-00000000000c"
        )

    assert totals is None
    assert "monotonicity broken" in caplog.text


class _PerMessageUsageProvider(BaseProvider):
    """A provider whose usage lives ON THE MESSAGES, with NO cumulative seam.

    This is the discriminating fixture for the project-aggregate override.
    The obvious candidate — the other real non-Codex provider — cannot express
    the bug: it has no token accounting at all, so its per-message totals are
    already zero and overwriting them with zeros is a no-op. A test built on it
    passes whether or not the guard exists.

    So this double reports real per-assistant-message ``usage`` (the Claude
    shape, which the ordinary accumulators do sum) and inherits
    ``session_token_totals`` from the base — i.e. ``None``, the DEFAULT that
    every provider but Codex has.
    """

    SESSION_ID = "40000000-0000-4000-8000-000000000001"

    def get_provider_name(self) -> str:
        return "permsg"

    def get_session_format(self) -> str:
        return "permsg"

    def get_data_dir(self) -> Optional[Path]:
        return None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        return iter(())

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        return iter(())

    def _info(self, root: Path) -> SessionInfo:
        return SessionInfo(
            provider="permsg",
            session_id=self.SESSION_ID,
            project_path=Path("/proj/permsg"),
            source_path=root / "permsg.jsonl",
        )

    def discover_sessions_under(self, root: Path) -> Iterator[SessionInfo]:
        yield self._info(root)

    def load_session_under(
        self, root: Path, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        from claude_code_log.factories.transcript_factory import create_transcript_entry

        for index in (1, 2):
            entry = create_transcript_entry(
                {
                    "type": "assistant",
                    "uuid": f"m{index}",
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": "/proj/permsg",
                    "sessionId": self.SESSION_ID,
                    "version": "1.0.0",
                    "timestamp": f"2026-07-11T07:0{index}:00.000Z",
                    "requestId": f"req-{index}",
                    "message": {
                        "id": f"msg-{index}",
                        "type": "message",
                        "role": "assistant",
                        "model": "test-model",
                        "content": [{"type": "text", "text": f"reply {index}"}],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "cache_read_input_tokens": 5,
                        },
                    },
                }
            )
            if entry is not None:
                yield entry


def _render_permsg(tmp_path: Path, monkeypatch, *, use_cache: bool) -> str:
    from claude_code_log.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(_PerMessageUsageProvider())
    # render_provider_wholesale imports this lazily from the package, so the
    # package attribute is the one that must be patched.
    monkeypatch.setattr(
        "claude_code_log.providers.discover_providers", lambda: registry
    )
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    # The cache keys source-mtime staleness off SessionInfo.source_path, so the
    # file must exist even though this double parses nothing from it.
    (root / "permsg.jsonl").write_text("", encoding="utf-8")
    index = render_provider_wholesale(
        "permsg", root, tmp_path / "out", use_cache=use_cache, silent=True
    )
    return index.read_text(encoding="utf-8")


def test_project_card_keeps_per_message_totals_without_cumulative_seam(
    tmp_path: Path, monkeypatch
) -> None:
    """A provider with no ``session_token_totals`` seam must keep its
    per-message project totals on the index card.

    ``_sum_provider_token_totals`` returns an ALL-ZERO dict when every session
    returned None, and the override used to apply it unconditionally — zeroing
    real aggregates. Two messages x (input 100, output 10, cache_read 5).

    Mutation-check: drop the ``has_provider_token_totals`` guard at either
    project-level site and this goes RED (the card renders no token line at
    all, because a zero column is omitted rather than printed as 0).
    """
    html = _render_permsg(tmp_path, monkeypatch, use_cache=False)
    assert "Input: 200 | Output: 20 | Cache Read: 10" in html


def test_cached_project_aggregates_keep_per_message_totals(
    tmp_path: Path, monkeypatch
) -> None:
    """The cache-side sibling of the same override, which writes the durable
    project aggregates. Guarded symmetrically with the session-level override
    twelve lines above it, which was already gated on ``is not None``."""
    from claude_code_log.cache import (
        CacheManager,
        get_cache_db_path,
        get_library_version,
    )

    _render_permsg(tmp_path, monkeypatch, use_cache=True)
    out = tmp_path / "out"
    session_page = next(out.rglob(f"session-{_PerMessageUsageProvider.SESSION_ID}*"))
    cache = CacheManager(
        session_page.parent, get_library_version(), db_path=get_cache_db_path(out)
    )
    project_cache = cache.get_cached_project_data()
    assert project_cache is not None
    assert project_cache.total_input_tokens == 200
    assert project_cache.total_output_tokens == 20
    assert project_cache.total_cache_read_tokens == 10
