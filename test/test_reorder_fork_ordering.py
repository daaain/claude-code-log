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
