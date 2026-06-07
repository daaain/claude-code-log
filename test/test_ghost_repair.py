"""Regression coverage for ``_repair_stale_anchor_refs`` (Phase 2 of the
ghosting epic).

When ``_ghost_template_by_detail`` sets a fork-point's slot to ``None``,
every cached anchor-target reference must be sanitized so no rendered
``#msg-d-{N}`` href dangles. Pre-ghost, ``_reindex_filtered_context``
guaranteed this by remapping or dropping refs to filtered messages;
``TestReindexBranchBackrefs`` pinned that behavior on the old function
directly. The function is gone, but the *invariant* moved into the
repair pass — these tests re-pin it on the new code path.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.models import DetailLevel, SessionHeaderMessage
from claude_code_log.renderer import generate_template_messages


def _user(
    uid: str,
    parent: str | None,
    ts: str,
    text: str,
    session_id: str = "sess-fork",
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": session_id,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant(
    uid: str,
    parent: str | None,
    ts: str,
    text: str,
    session_id: str = "sess-fork",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": session_id,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0",
        "requestId": f"req_{uid}",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _fork_fixture(path: Path) -> Path:
    """A within-session fork whose anchor is an ASSISTANT message.

    Topology::

        u-trunk (user)
          └── a-anchor (assistant)         <- fork point; ghosted at USER_ONLY
                ├── a-branch1 → u-reply1   (branch 1: assistant-first sibling)
                └── a-branch2 → u-reply2   (branch 2: assistant-first sibling)

    Both branch first-entries are assistants to bypass the DAG's
    fork-collapse heuristic (see ``_stitch_tool_results``), and each
    branch carries a user reply so it isn't a single-entry dead-end.
    """
    sid = "sess-fork-repair"
    entries: list[dict[str, Any]] = [
        _user("u-trunk", None, "2026-02-01T10:00:00Z", "hello", session_id=sid),
        _assistant(
            "a-anchor", "u-trunk", "2026-02-01T10:00:01Z", "fork point", session_id=sid
        ),
        # Branch 1
        _assistant(
            "a-branch1",
            "a-anchor",
            "2026-02-01T10:00:02Z",
            "branch one",
            session_id=sid,
        ),
        _user(
            "u-reply1",
            "a-branch1",
            "2026-02-01T10:00:03Z",
            "reply on branch one",
            session_id=sid,
        ),
        # Branch 2
        _assistant(
            "a-branch2",
            "a-anchor",
            "2026-02-01T10:00:04Z",
            "branch two",
            session_id=sid,
        ),
        _user(
            "u-reply2",
            "a-branch2",
            "2026-02-01T10:00:05Z",
            "reply on branch two",
            session_id=sid,
        ),
    ]
    return _write_jsonl(path, entries)


class TestGhostedForkAnchorBackrefRepair:
    """The deleted-guard coverage monk required before Phase 2 merges.

    `_repair_stale_anchor_refs` (called at the end of
    `_ghost_template_by_detail`) must null any
    ``SessionHeaderMessage.parent_message_index`` whose target slot
    was ghosted by the detail filter — otherwise the branch header's
    ``#msg-d-{N}`` back-link resolves to a phantom slot and renders
    as a dead anchor.
    """

    def test_branch_backref_is_sanitized_when_anchor_ghosted(
        self, tmp_path: Path
    ) -> None:
        """Render the fork at USER_ONLY (ghosts assistant text), then
        assert NO branch header carries a non-None
        ``parent_message_index`` that resolves to a ghost.

        The forbidden state is exactly the dead ``#msg-d-N``: a
        non-None index whose ``ctx.get(...)`` returns None.
        """
        messages = load_transcript(_fork_fixture(tmp_path / "fork.jsonl"))
        _, _, ctx = generate_template_messages(messages, detail=DetailLevel.USER_ONLY)

        survivors = [m for m in ctx.messages if m is not None]

        # Precondition: the fork anchor MUST be ghosted at USER_ONLY,
        # otherwise this test exercises nothing — the repair pass would be a
        # no-op and the regression could rot silently. Assert it directly so
        # a preserved anchor fails here instead of passing vacuously below.
        anchor_idx = next(
            (
                i
                for i, m in enumerate(ctx.messages)
                if m is not None and m.meta.uuid == "a-anchor"
            ),
            None,
        )
        assert anchor_idx is None, (
            "expected fork-anchor assistant 'a-anchor' to be ghosted at "
            "USER_ONLY; otherwise this test does not validate stale-anchor "
            f"repair. anchor still present at index {anchor_idx}."
        )

        # The actual invariant: no branch header points at a ghost slot.
        branch_headers = [
            m
            for m in survivors
            if isinstance(m.content, SessionHeaderMessage) and m.content.is_branch
        ]
        assert len(branch_headers) >= 1, (
            "expected at least one within-session branch header in the "
            "rendered USER_ONLY output; got 0. The fork fixture didn't "
            "produce branches as designed."
        )
        for bh in branch_headers:
            assert isinstance(bh.content, SessionHeaderMessage)
            parent_idx = bh.content.parent_message_index
            if parent_idx is None:
                continue
            target = ctx.get(parent_idx)
            assert target is not None, (
                f"branch header session_id={bh.content.session_id!r} "
                f"carries parent_message_index={parent_idx} that resolves "
                f"to a GHOST slot — this is the dead-anchor failure that "
                f"`_repair_stale_anchor_refs` is supposed to prevent. "
                f"Either the repair pass didn't run, the predicate is "
                f"wrong, or `ctx.get()` semantics drifted."
            )


class TestHtmlAnchorIntegrity:
    """Folds in monk's Phase-1 Note 2: every rendered ``#msg-d-{N}``
    href must have a matching ``id='msg-d-{N}'`` somewhere in the
    same HTML document. With the move to sparse (ghost-aware) indices,
    the snapshot suite no longer guards this invariant — make it
    machine-checked.
    """

    @staticmethod
    def _anchor_invariant(html: str) -> None:
        ids = set(re.findall(r"id=['\"]msg-d-(\d+)['\"]", html))
        hrefs = set(re.findall(r"href=['\"]#msg-d-(\d+)['\"]", html))
        dangling = hrefs - ids
        assert not dangling, (
            f"rendered HTML contains {len(dangling)} dead `#msg-d-{{N}}` "
            f"hrefs whose target id is missing: {sorted(dangling)}. "
            f"This is the dead-anchor failure the ghosting repair pass "
            f"is supposed to prevent."
        )

    def test_no_dead_anchors_at_user_only(self, tmp_path: Path) -> None:
        """Render the ghosted-fork fixture at USER_ONLY and verify
        every href targets a live id."""
        messages = load_transcript(_fork_fixture(tmp_path / "fork.jsonl"))
        renderer = HtmlRenderer()
        renderer.detail = DetailLevel.USER_ONLY
        html = renderer.generate(messages, "ghost-repair USER_ONLY")
        self._anchor_invariant(html)

    def test_no_dead_anchors_at_minimal(self, tmp_path: Path) -> None:
        """Same invariant at MINIMAL (drops tools/thinking but keeps
        assistant text — the fork anchor survives, exercising the
        no-op path of the repair pass)."""
        messages = load_transcript(_fork_fixture(tmp_path / "fork.jsonl"))
        renderer = HtmlRenderer()
        renderer.detail = DetailLevel.MINIMAL
        html = renderer.generate(messages, "ghost-repair MINIMAL")
        self._anchor_invariant(html)

    def test_no_dead_anchors_at_full(self, tmp_path: Path) -> None:
        """And at FULL (no detail filter runs at all — guards against
        regressions in the non-ghosted pre-render path)."""
        messages = load_transcript(_fork_fixture(tmp_path / "fork.jsonl"))
        renderer = HtmlRenderer()
        renderer.detail = DetailLevel.FULL
        html = renderer.generate(messages, "ghost-repair FULL")
        self._anchor_invariant(html)
