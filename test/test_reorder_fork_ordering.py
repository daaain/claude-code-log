"""Intra-session fork ordering in ``_reorder_session_template_messages``.

A within-session rewind splits a trunk into runs interleaved with branch
(``{trunk}@{uuid}``) sub-lines. The DAG traversal already lays these out in
chronological order — trunk-so-far, the (earlier-ending) branch block, then the
trunk's later continuation. ``_reorder_session_template_messages`` used to
re-gather every message that shares the trunk sid under the trunk's first
header, hoisting the trunk's later continuation ahead of the branch and
stranding the earlier-timestamped branch block at the very end of the page.

Regression: the branch block must stay in its DAG position; the page must end
on the trunk's latest message.
"""

from __future__ import annotations

from claude_code_log.models import (
    AssistantTextMessage,
    MessageMeta,
    SessionHeaderMessage,
    TextContent,
)
from claude_code_log.renderer import (
    TemplateMessage,
    _build_message_hierarchy,
    _reorder_session_template_messages,
)

TRUNK = "s1"
BRANCH = "s1@b_abandoned"


def _msg(uuid: str, ts: str, render_sid: str) -> TemplateMessage:
    m = TemplateMessage(
        AssistantTextMessage(
            meta=MessageMeta(session_id=TRUNK, timestamp=ts, uuid=uuid),
            items=[TextContent(type="text", text=uuid)],
        )
    )
    m.render_session_id = render_sid
    return m


def _header(render_sid: str, *, is_branch: bool) -> TemplateMessage:
    h = TemplateMessage(
        SessionHeaderMessage(
            meta=MessageMeta(session_id=TRUNK, timestamp="", uuid=f"hdr-{render_sid}"),
            title=render_sid,
            session_id=render_sid,
            is_branch=is_branch,
        )
    )
    h.render_session_id = render_sid
    return h


def _build_dag_ordered_input() -> list[TemplateMessage]:
    """Input in DAG (chronological) order: trunk, branch, trunk continuation."""
    return [
        _header(TRUNK, is_branch=False),
        _msg("t0", "2026-09-22T10:00:00.000Z", TRUNK),
        _msg("t1", "2026-09-22T10:02:00.000Z", TRUNK),
        # Abandoned rewind branch — forks early, ends EARLY (13:35).
        _header(BRANCH, is_branch=True),
        _msg("b0", "2026-09-22T11:35:00.000Z", BRANCH),
        _msg("b1", "2026-09-22T13:35:00.000Z", BRANCH),
        # Trunk resumes and runs to the real end (16:23).
        _msg("t2", "2026-09-22T15:00:00.000Z", TRUNK),
        _msg("t3", "2026-09-22T16:23:00.000Z", TRUNK),
    ]


def test_fork_branch_not_stranded_at_end() -> None:
    result = _reorder_session_template_messages(_build_dag_ordered_input())

    body = [m for m in result if not m.is_session_header]
    uuids = [m.meta.uuid for m in body]

    # The trunk's latest message ends the page — not the branch's 13:35 tail.
    assert uuids[-1] == "t3", (
        f"expected trunk end 't3' last; got {uuids}. The abandoned branch was "
        "stranded after the trunk's later continuation."
    )
    # The branch block keeps its DAG position, before the trunk continuation.
    assert uuids.index("b1") < uuids.index("t2") < uuids.index("t3"), (
        f"branch block must precede the trunk continuation; got {uuids}"
    )


def test_fork_ordering_preserves_all_messages() -> None:
    result = _reorder_session_template_messages(_build_dag_ordered_input())
    body_uuids = {m.meta.uuid for m in result if not m.is_session_header}
    assert body_uuids == {"t0", "t1", "b0", "b1", "t2", "t3"}


def _hierarchy(messages: list[TemplateMessage]) -> dict[str, set[str]]:
    """Reorder, index, build ancestry; return each message's ancestor uuids."""
    result = _reorder_session_template_messages(messages)
    for i, m in enumerate(result):
        m.message_index = i
    _build_message_hierarchy(result)
    by_index = {m.message_index: m.meta.uuid for m in result}
    return {
        m.meta.uuid: {by_index[i] for i in m.ancestry if i in by_index}
        for m in result
    }


def test_branch_scope_closes_when_trunk_resumes() -> None:
    """The trunk's continuation must not nest under the abandoned branch header.

    Branch headers live at fractional level 0.5, below the level-1+ trunk
    continuation, so a level-only stack keeps the branch header in ``t2``/``t3``'s
    ancestry — folding the abandoned branch would then hide the session's real
    ending. The trunk continuation must sit under the trunk header only.
    """
    ancestry = _hierarchy(_build_dag_ordered_input())
    branch_header = f"hdr-{BRANCH}"
    trunk_header = f"hdr-{TRUNK}"

    # Branch messages still nest under the branch header.
    assert branch_header in ancestry["b0"]
    assert branch_header in ancestry["b1"]

    # Trunk continuation nests under the trunk header, not the abandoned branch.
    for uuid in ("t2", "t3"):
        assert branch_header not in ancestry[uuid], (
            f"{uuid} (trunk continuation) must not nest under the abandoned "
            f"branch header; ancestry={ancestry[uuid]}"
        )
        assert trunk_header in ancestry[uuid]


def _sess_msg(uuid: str, session_id: str) -> TemplateMessage:
    m = TemplateMessage(
        AssistantTextMessage(
            meta=MessageMeta(
                session_id=session_id, timestamp="2026-01-01T00:00:00.000Z", uuid=uuid
            ),
            items=[TextContent(type="text", text=uuid)],
        )
    )
    m.render_session_id = session_id
    return m


def _sess_header(session_id: str, *, is_branch: bool) -> TemplateMessage:
    h = TemplateMessage(
        SessionHeaderMessage(
            meta=MessageMeta(
                session_id=session_id.split("@")[0], timestamp="", uuid=f"hdr-{session_id}"
            ),
            title=session_id,
            session_id=session_id,
            is_branch=is_branch,
        )
    )
    h.render_session_id = session_id
    return h


def test_neighbouring_session_does_not_adopt_fork_family() -> None:
    """A second session's header must not become an ancestor of a fork family.

    When session A is a forked trunk (so its family stays in DAG order) and
    session B's header is interleaved between A's runs, A's later continuation
    must not nest under B's header.
    """
    a_trunk = "sA"
    a_branch = "sA@x"
    messages = [
        _sess_header(a_trunk, is_branch=False),
        _sess_msg("a0", a_trunk),
        _sess_header(a_branch, is_branch=True),
        _sess_msg("x0", a_branch),
        _sess_header("sB", is_branch=False),
        _sess_msg("b0", "sB"),
        _sess_msg("a1", a_trunk),  # A's continuation, after B's header
    ]
    ancestry = _hierarchy(messages)

    assert "hdr-sB" not in ancestry["a1"], (
        f"A's continuation must not nest under session B's header; "
        f"ancestry={ancestry['a1']}"
    )
    # B's own message still nests under B's header.
    assert "hdr-sB" in ancestry["b0"]
