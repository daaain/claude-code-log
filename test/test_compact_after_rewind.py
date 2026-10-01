"""Regression: a ``/compact`` after a rewind continues the live branch.

A ``compact_boundary`` has ``parentUuid: null``, so the DAG walk sees it as a
fresh root. Walked as a trunk root, the post-compaction conversation landed on
the plain trunk line, resuming the trunk *after* its own rewind branches: the
branch blocks (earlier timestamps) rendered at the very bottom of the session,
after the conversation's real tail. The boundary records the message it
continues in ``logicalParentUuid`` — or, for compactions that keep a
preserved segment, in ``compactMetadata`` — and that message sits on the
branch the user was on, so the boundary must continue that branch.

Fixtures (shapes taken from real transcripts, content synthetic):

* ``dag_compact_after_rewind.jsonl`` — ``logicalParentUuid`` is the live
  branch's last message (``b2a2``).
* ``dag_compact_after_rewind_preserved.jsonl`` — the preserved tail
  (``b2a2``, also the ``logicalParentUuid``) is re-parented under the
  summary anchor, so only the segment's head (``b2a``) is on the branch.

Also pins nested subagents inside a rewind branch (``nested_agents`` plus a
rewind sibling): every branch message keeps the branch header in its
ancestry, and the sidechain dedup removes the same messages as unforked.
"""

import json
import shutil
from pathlib import Path

import pytest

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.renderer import TemplateMessage, generate_template_messages

TEST_DATA = Path(__file__).parent / "test_data"


def _render(project: Path) -> tuple[list[TemplateMessage], dict[int, TemplateMessage]]:
    """Render a project directory; return messages in display order + index."""
    entries, tree = load_directory_transcripts(project, silent=True)
    roots, _, _ = generate_template_messages(entries, session_tree=tree)
    flat: list[TemplateMessage] = []

    def walk(messages: list[TemplateMessage]) -> None:
        for message in messages:
            flat.append(message)
            walk(message.children)

    walk(roots)
    by_index = {m.message_index: m for m in flat if m.message_index is not None}
    return flat, by_index


def _uuid(message: TemplateMessage) -> str:
    return message.meta.uuid if message.meta else ""


def _header_sids(
    message: TemplateMessage, by_index: dict[int, TemplateMessage]
) -> list[str]:
    return [
        by_index[i].render_session_id or ""
        for i in message.ancestry
        if i in by_index and by_index[i].is_session_header
    ]


@pytest.mark.parametrize(
    ("fixture", "sid", "live_branch"),
    [
        (
            "dag_compact_after_rewind",
            "s1",
            ["b2u", "b2a", "b2u2", "b2a2", "cb", "cs", "p1u", "p1a"],
        ),
        (
            "dag_compact_after_rewind_preserved",
            "s2",
            ["b2u", "b2a", "b2u2", "cb", "cs", "b2a2", "p1u", "p1a"],
        ),
    ],
)
class TestCompactAfterRewind:
    @pytest.fixture()
    def project(self, tmp_path: Path, fixture: str) -> Path:
        shutil.copy(TEST_DATA / f"{fixture}.jsonl", tmp_path)
        return tmp_path

    def test_compaction_continues_live_branch(
        self, project: Path, sid: str, live_branch: list[str]
    ) -> None:
        """The trunk ends at the fork; the boundary extends the live branch."""
        _, tree = load_directory_transcripts(project, silent=True)
        assert tree is not None
        assert tree.sessions[sid].uuids == ["u1", "a1"]
        assert tree.sessions[f"{sid}@b1u"].uuids == ["b1u", "b1a"]
        assert tree.sessions[f"{sid}@b2u"].uuids == live_branch

    def test_render_order_ends_on_the_real_tail(
        self, project: Path, sid: str, live_branch: list[str]
    ) -> None:
        """The abandoned branch is not stranded after the session's tail."""
        flat, _ = _render(project)
        order = [_uuid(m) for m in flat if not m.is_session_header]
        assert order == ["u1", "a1", "b1u", "b1a"] + live_branch

    def test_post_compaction_messages_nest_under_branch(
        self, project: Path, sid: str, live_branch: list[str]
    ) -> None:
        """Folding the live branch hides its post-compaction messages too."""
        flat, by_index = _render(project)
        for message in flat:
            if _uuid(message) in ("p1u", "p1a"):
                assert _header_sids(message, by_index) == [sid, f"{sid}@b2u"]


NESTED_SID = "33330000-0000-4000-8000-000000000001"


def _nested_agents_project(tmp_path: Path, rewind: bool) -> Path:
    project = tmp_path / ("rewind" if rewind else "plain")
    shutil.copytree(TEST_DATA / "nested_agents", project)
    if rewind:
        # Give ns-u1 a second, later assistant child so all the agent work
        # becomes the ``@ns-a1`` rewind branch.
        trunk = project / f"{NESTED_SID}.jsonl"
        lines = [json.loads(line) for line in trunk.read_text().splitlines()]
        alt = json.loads(json.dumps(lines[1]))  # copy of ns-a1
        alt.update(uuid="alt-a1", timestamp="2026-06-12T10:00:00.000Z")
        alt["message"].update(id="alt", content=[{"type": "text", "text": "retry"}])
        lines.append(alt)
        trunk.write_text("\n".join(map(json.dumps, lines)) + "\n")
    return project


def test_nested_agents_inside_rewind_branch(tmp_path: Path) -> None:
    plain, _ = _render(_nested_agents_project(tmp_path, rewind=False))
    flat, by_index = _render(_nested_agents_project(tmp_path, rewind=True))

    branch = f"{NESTED_SID}@ns-a1"
    on_branch = [
        m
        for m in flat
        if not m.is_session_header
        and (m.render_session_id or "") != f"{NESTED_SID}@alt-a1"
        and _uuid(m) != "ns-u1"
    ]
    assert on_branch
    for message in on_branch:
        assert branch in _header_sids(message, by_index), _uuid(message)

    # Sidechain dedup (subagent prompt/answer duplicating the Task input/
    # result) drops the same messages whether or not the work is a branch.
    def rendered(messages: list[TemplateMessage]) -> set[str]:
        return {_uuid(m) for m in messages if not m.is_session_header} - {"alt-a1"}

    assert rendered(flat) == rendered(plain)


def test_queue_op_inside_branch_stays_in_branch(tmp_path: Path) -> None:
    """A uuid-less queue-op anchored inside a branch renders in that branch.

    Queue-ops carry only the raw ``sessionId`` (the trunk). They are spliced
    after their same-session anchor entry, but without the anchor's DAG line
    the session regrouping hoisted them under the trunk header — ahead of
    every branch, however late they arrived.
    """
    shutil.copy(TEST_DATA / "dag_compact_after_rewind.jsonl", tmp_path)
    steering = {
        "type": "queue-operation",
        "operation": "remove",
        "timestamp": "2025-07-01T10:15:00.000Z",
        "content": "steer the second attempt",
        "sessionId": "s1",
    }
    with (tmp_path / "dag_compact_after_rewind.jsonl").open("a") as f:
        f.write(json.dumps(steering) + "\n")

    flat, by_index = _render(tmp_path)
    order = [
        _uuid(m) or "STEER"
        for m in flat
        if not m.is_session_header and m.type in ("user", "assistant", "system")
    ]
    assert order.index("STEER") == order.index("b2a") + 1
    steer = next(m for m in flat if not m.is_session_header and not _uuid(m))
    assert steer.render_session_id == "s1@b2u"
    assert _header_sids(steer, by_index) == ["s1", "s1@b2u"]
