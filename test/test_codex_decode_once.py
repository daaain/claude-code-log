"""Asking a provider for token totals must cost no extra rollout decodes.

Entries and cumulative token totals both come from a rollout's decoded
records. When the walker asked the provider for them separately, the totals
call repeated the whole resolution — index lookup, identity, decode,
inherited-prefix strip — and threw the records away again. On a real 34-rollout
archive that was 354 decodes where 236 sufficed: +118 decodes and +636 MB
re-decoded to recompute what the first pass had already produced.

The property under test is deliberately *not* "the output is correct".
**Correct output is compatible with arbitrarily redundant decoding** — that is
precisely why the redundancy survived a green suite — so these tests count the
primitive instead.

It is equally deliberately not "once per distinct path". That is the end state
of the wider decode work (a rollout is still decoded during discovery, and a
forked session still re-decodes its parent — 202 of the 320 redundant decodes
in that archive, pre-existing and out of scope here). Pinning the end state
would fail for reasons this change never claimed to fix. What this change owns
is the *delta the totals seam adds*, so that delta — measured as a two-arm
comparison against the base-class default — is what is pinned.

The second test guards the trap that a decode count cannot see. Totals must
stay subject to the same date filter as the messages they accompany: a session
emptied by ``--from-date`` contributes no messages and must contribute no
tokens either. Hoisting the totals out of that filter would remove decodes
*and* silently change project totals — a behaviour change wearing a
performance fix's clothing.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Iterator, Optional

from claude_code_log.converter import render_provider_wholesale
from claude_code_log.providers import codex as codex_module
from claude_code_log.providers.base import LoadedSession, ProviderTokenTotals
from claude_code_log.providers.codex import CodexProvider, _DecodedRecord

_CWD = "/proj/decode"


def _rollout(tmp: Path, name: str, thread_id: str, day: str, steps: int = 2) -> Path:
    """A minimal rollout on ``day`` (YYYY-MM-DD) carrying cumulative totals.

    ``day`` is a parameter because the date-filter test needs two sessions on
    *different* days; a builder with a fixed timestamp could not express the
    case it has to pin.
    """
    records: list[dict[str, object]] = [
        {
            "timestamp": f"{day}T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": f"{day}T00:00:00Z",
                "cwd": _CWD,
            },
        },
        {
            "timestamp": f"{day}T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"hi {thread_id[:4]}"},
        },
    ]
    for i in range(steps):
        records.append(
            {
                "timestamp": f"{day}T00:00:0{2 + i}Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": f"step {i}"},
            }
        )
        cumulative = 100 * (i + 1)
        records.append(
            {
                "timestamp": f"{day}T00:00:0{2 + i}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": cumulative,
                            "cached_input_tokens": 0,
                            "output_tokens": cumulative,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 2 * cumulative,
                        },
                        "model_context_window": 258400,
                    },
                },
            }
        )
    path = tmp / f"rollout-{day}T00-00-00-{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _render_counting_decodes(
    root: Path, out: Path, *, stub_totals: bool = False
) -> Counter:
    """Run a wholesale render with ``_decode_records`` counted per path.

    Wraps the provider primitive at class level (the attach point used when
    these figures were first measured), so the count is comparable to the
    recorded baseline.

    ``stub_totals`` makes the provider report no session totals — the
    base-class default, not an approximation of one — which is the second arm
    of the comparison.

    It stubs **both** totals entry points, and that is load-bearing rather than
    belt-and-braces: stubbing only the combined seam makes the comparison
    vacuous the moment the walker stops using it, because both arms then take
    the same two-call path and trivially agree. Disabling totals at the
    provider level instead means arm B is "this provider records no totals"
    however the walker asks — which is the property the arms are meant to
    differ by. (Verified: with only the combined seam stubbed, reverting the
    walker to two calls left this test GREEN.)
    """
    counts: Counter = Counter()
    original = CodexProvider._decode_records
    original_combined = CodexProvider.load_session_with_totals
    original_totals = CodexProvider.session_token_totals

    # The stubs are annotated to match what they shadow, rather than suppressed:
    # a stub whose signature has drifted from the real method would silently stop
    # exercising the same call, which is the failure mode these arms exist to
    # avoid. Type-compatibility here is part of the test, not lint appeasement.
    def wrapped(self, path: Path) -> Iterator[_DecodedRecord]:
        counts[str(path)] += 1
        return iter(list(original(self, path)))

    def no_combined(
        self,
        root: Path,
        session_id: str,
        max_messages: Optional[int] = None,
    ) -> LoadedSession:
        return LoadedSession(
            entries=list(self.load_session_under(root, session_id, max_messages)),
            token_totals=None,
        )

    def no_totals(self, root: Path, session_id: str) -> Optional[ProviderTokenTotals]:
        return None

    # ty: assigning a plain function to a method attribute is flagged as
    # implicit shadowing even when the signature matches exactly (verified:
    # ty prints both sides identically and still errors). The signatures ARE
    # mirrored deliberately -- see the comment above.
    codex_module.CodexProvider._decode_records = wrapped  # ty: ignore[invalid-assignment]
    if stub_totals:
        codex_module.CodexProvider.load_session_with_totals = no_combined  # ty: ignore[invalid-assignment]
        codex_module.CodexProvider.session_token_totals = no_totals  # ty: ignore[invalid-assignment]
    try:
        render_provider_wholesale("codex", root, out, use_cache=False, silent=True)
    finally:
        codex_module.CodexProvider._decode_records = original
        codex_module.CodexProvider.load_session_with_totals = original_combined
        codex_module.CodexProvider.session_token_totals = original_totals
    return counts


def test_token_totals_cost_no_extra_decodes(tmp_path: Path) -> None:
    """Asking for token totals must cost **zero** additional rollout decodes.

    The measurable property of this change, stated as the two-arm comparison
    the corpus measurement used: render once with the totals seam live, render
    again with it stubbed to the base-class default (``None`` — the behaviour
    of every provider that records no session totals), and require the decode
    counts to be *equal*, per file and in total.

    Deliberately NOT "once per distinct path". That is the end state of the
    wider decode work and it is not reached here: a rollout is still decoded by
    ``_read_identity`` during discovery, and forked sessions still re-decode
    their parent. Those are pre-existing and out of scope — asserting the end
    state would make this test fail for reasons this change never claimed to
    fix, and would have to be weakened later by someone who does not know which
    part was load-bearing. What this change owns is the *delta*, so the delta
    is what it pins.

    Three sessions, so an accidental equality on a single file cannot carry it.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    for i in (1, 2, 3):
        _rollout(
            root, f"s{i}", f"1000000{i}-0000-4000-8000-00000000000{i}", "2026-01-02"
        )

    with_totals = _render_counting_decodes(root, tmp_path / "out-a")
    without_totals = _render_counting_decodes(
        root, tmp_path / "out-b", stub_totals=True
    )

    assert len(with_totals) == 3, f"expected 3 rollouts, saw {sorted(with_totals)}"
    assert sum(with_totals.values()) == sum(without_totals.values()), (
        f"totals cost {sum(with_totals.values()) - sum(without_totals.values())} "
        f"extra decodes: with={dict(with_totals)} without={dict(without_totals)}"
    )
    assert with_totals == without_totals, (
        "per-file decode counts differ between the two arms: "
        f"with={dict(with_totals)} without={dict(without_totals)}"
    )


def test_filtered_out_session_contributes_no_tokens(tmp_path: Path) -> None:
    """A session removed by the date filter must contribute neither messages
    nor tokens.

    The totals travel with the entries through the same survival test. If they
    were collected before it — the obvious way to fetch totals once per
    session — the filtered-out session's tokens would still land in the project
    card, and the decode count would look *better* while the numbers got worse.

    Two sessions on different days, filtering out exactly one, so the fixture
    can express the difference: with both included the project total is the sum
    of two sessions, and the assertion below would hold vacuously if the filter
    removed nothing.

    Mutation notes, because the obvious mutation does *not* discriminate here:
    moving the totals collection above the ``if messages:`` gate alone leaves
    this test GREEN, since the survival test is enforced where the totals are
    *consumed* (the comprehension over ``loaded``), not where they are
    collected. The mutation that reddens it — and the actual failure mode — is
    hoisting the collection **and** summing every collected total instead of
    the survivors'. If you move that gate, this test is what should catch you.
    """
    root = tmp_path / "sessions"
    root.mkdir()
    _rollout(root, "keep", "20000001-0000-4000-8000-000000000001", "2026-01-05")
    _rollout(root, "drop", "20000002-0000-4000-8000-000000000002", "2026-01-02")

    both = tmp_path / "out-both"
    index_both = render_provider_wholesale(
        "codex", root, both, use_cache=False, silent=True
    )
    html_both = index_both.read_text(encoding="utf-8")

    filtered = tmp_path / "out-filtered"
    index_filtered = render_provider_wholesale(
        "codex",
        root,
        filtered,
        from_date="2026-01-04",
        use_cache=False,
        silent=True,
    )
    html_filtered = index_filtered.read_text(encoding="utf-8")

    # Sanity: the filter actually removed a session, or the totals assertion
    # below would pass for the wrong reason.
    assert "20000002" not in html_filtered
    assert "20000002" in html_both

    # Each session's last cumulative is input 200 / output 200, so the project
    # card reads 400/400 with both sessions and must read 200/200 once one is
    # filtered out. (No "Cache Read" component: it is zero, and this renderer
    # OMITS a zero component rather than printing it — so asserting on it would
    # fail for a reason unrelated to the property under test.)
    assert "Input: 400 | Output: 400" in html_both, (
        "expected the two-session project total"
    )
    assert "Input: 200 | Output: 200" in html_filtered, (
        "filtered render should show only the surviving session's totals"
    )
    assert "Input: 400 | Output: 400" not in html_filtered, (
        "filtered-out session is still contributing to project totals"
    )
