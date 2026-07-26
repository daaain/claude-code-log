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

from claude_code_log.converter import (
    _sum_provider_token_totals,
    render_provider_wholesale,
)
from claude_code_log.providers.base import ProviderTokenTotals
from claude_code_log.providers.codex import (
    CodexProvider,
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
    token_totals: list[dict[str, int]],
) -> Path:
    """Write a rollout whose token_count events carry the given cumulative
    totals in order (the LAST one is the session total)."""
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
        records.append(_token_count_record(tu, f"2026-01-02T00:00:0{3 + i}Z"))
    rel_path = Path(rel)
    path = tmp / rel_path.parent / f"rollout-{rel_path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _decoded(records_source: Path) -> list[object]:
    return list(CodexProvider()._decode_records(records_source))


# --------------------------------------------------------------------------
# _map_cumulative_usage — the mapping onto index columns
# --------------------------------------------------------------------------
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
    result = _token_totals_from_records(_decoded(path))  # type: ignore[arg-type]
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
    result = _token_totals_from_records(_decoded(path))  # type: ignore[arg-type]
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
    assert _token_totals_from_records(_decoded(path)) is None  # type: ignore[arg-type]


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
    assert _token_totals_from_records(_decoded(path)) is None  # type: ignore[arg-type]


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
