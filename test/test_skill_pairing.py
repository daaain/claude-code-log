"""Tests for Skill tool_use / isMeta slash-command pairing (issue #93).

Claude Code's Skill invocation produces three discrete entries in the
transcript:

1. assistant `Skill` tool_use (one row)
2. user tool_result with the literal text "Launching skill: <name>"
3. user `isMeta=True` entry whose `sourceToolUseID` matches (1) and
   whose text is the expanded skill body (markdown, 100+ lines)

`_pair_skill_tool_uses` in `renderer.py` folds (3) into (1) as
`ToolUseMessage.skill_body` and drops (2) and (3) from `ctx.messages`
so the Skill invocation renders as a single visual unit.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    ToolResultMessage,
    ToolUseMessage,
    UserSlashCommandMessage,
)
from claude_code_log.renderer import generate_template_messages


# -- Fixtures ----------------------------------------------------------------


def _user(
    uid: str,
    parent: str | None,
    ts: str,
    content: list[dict],
    is_meta: bool = False,
    source_tool_use_id: str | None = None,
    session_id: str = "sess-skill",
) -> dict:
    e: dict = {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": session_id,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0",
        "message": {"role": "user", "content": content},
    }
    if is_meta:
        e["isMeta"] = True
    if source_tool_use_id is not None:
        e["sourceToolUseID"] = source_tool_use_id
    return e


def _assistant_tool_use(
    uid: str,
    parent: str,
    ts: str,
    tool_name: str,
    tool_use_id: str,
    input_obj: dict,
    session_id: str = "sess-skill",
) -> dict:
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
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": input_obj,
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _assistant_text(
    uid: str,
    parent: str,
    ts: str,
    text: str,
    session_id: str = "sess-skill",
) -> dict:
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


def _write_jsonl(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _skill_invocation_jsonl(
    path: Path,
    skill_name: str = "my-skill",
    skill_body: str = "# My Skill\n\nThe **body** of the skill.",
    tool_use_id: str = "toolu_SKILL_A",
) -> Path:
    entries = [
        _user("u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]),
        _assistant_tool_use(
            "a-001",
            "u-001",
            "2026-01-01T10:00:01Z",
            "Skill",
            tool_use_id,
            {"skill": skill_name, "args": ""},
        ),
        _user(
            "u-002",
            "a-001",
            "2026-01-01T10:00:02Z",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"Launching skill: {skill_name}",
                    "is_error": False,
                }
            ],
        ),
        _user(
            "u-003",
            "u-002",
            "2026-01-01T10:00:03Z",
            [{"type": "text", "text": skill_body}],
            is_meta=True,
            source_tool_use_id=tool_use_id,
        ),
        _assistant_text("a-002", "u-003", "2026-01-01T10:00:04Z", "Skill ran."),
    ]
    return _write_jsonl(path, entries)


# -- Template-level pairing --------------------------------------------------


class TestSkillPairing:
    """The three-entity Skill pattern collapses to one ToolUseMessage."""

    def test_skill_body_folded_into_tool_use(self, tmp_path: Path) -> None:
        body = "# Read Mail\n\nReads and displays **mail** by ID."
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="clmail:read", skill_body=body
            )
        )

        _, _, ctx = generate_template_messages(messages)

        skill_tool_uses = [
            m.content
            for m in ctx.messages
            if m is not None
            and isinstance(m.content, ToolUseMessage)
            and m.content.tool_name == "Skill"
        ]
        assert len(skill_tool_uses) == 1
        assert skill_tool_uses[0].skill_body == body

    def test_slash_command_consumed(self, tmp_path: Path) -> None:
        messages = load_transcript(_skill_invocation_jsonl(tmp_path / "t.jsonl"))
        _, _, ctx = generate_template_messages(messages)

        slash = [
            m
            for m in ctx.messages
            if m is not None and isinstance(m.content, UserSlashCommandMessage)
        ]
        assert slash == [], (
            f"UserSlashCommandMessage should be consumed when paired; got "
            f"{[type(m.content).__name__ if m is not None else 'GHOST' for m in ctx.messages]}"
        )

    def test_launching_skill_tool_result_dropped(self, tmp_path: Path) -> None:
        messages = load_transcript(
            _skill_invocation_jsonl(tmp_path / "t.jsonl", tool_use_id="toolu_X")
        )
        _, _, ctx = generate_template_messages(messages)

        tr = [
            m
            for m in ctx.messages
            if m is not None
            and isinstance(m.content, ToolResultMessage)
            and m.content.tool_use_id == "toolu_X"
        ]
        assert tr == [], (
            "The redundant 'Launching skill: X' tool_result should be dropped"
        )

    def test_non_skill_tool_use_unchanged(self, tmp_path: Path) -> None:
        """Other tool_use entries (e.g. Bash) keep their separate tool_result."""
        entries = [
            _user(
                "u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]
            ),
            _assistant_tool_use(
                "a-001",
                "u-001",
                "2026-01-01T10:00:01Z",
                "Bash",
                "toolu_BASH_1",
                {"command": "echo hi"},
            ),
            _user(
                "u-002",
                "a-001",
                "2026-01-01T10:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_BASH_1",
                        "content": "hi",
                        "is_error": False,
                    }
                ],
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "t.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        tool_uses = [
            m.content
            for m in ctx.messages
            if m is not None and isinstance(m.content, ToolUseMessage)
        ]
        results = [
            m
            for m in ctx.messages
            if m is not None and isinstance(m.content, ToolResultMessage)
        ]
        assert len(tool_uses) == 1
        assert tool_uses[0].skill_body is None
        assert len(results) == 1  # Bash tool_result is NOT dropped

    def test_meta_without_source_tool_use_id_unchanged(self, tmp_path: Path) -> None:
        """isMeta=True entries without sourceToolUseID render as slash-commands."""
        entries = [
            _user(
                "u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]
            ),
            _user(
                "u-002",
                "u-001",
                "2026-01-01T10:00:01Z",
                [{"type": "text", "text": "# Standalone meta\n\nNot a skill body."}],
                is_meta=True,
                # no sourceToolUseID
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "t.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        slash = [
            m
            for m in ctx.messages
            if m is not None and isinstance(m.content, UserSlashCommandMessage)
        ]
        assert len(slash) == 1  # Still rendered as a standalone slash-command

    def test_orphan_skill_body_unpaired(self, tmp_path: Path) -> None:
        """If the matching Skill tool_use is missing, the body survives as-is."""
        entries = [
            _user(
                "u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]
            ),
            _user(
                "u-002",
                "u-001",
                "2026-01-01T10:00:01Z",
                [{"type": "text", "text": "# Orphan\n\nNo tool_use with this id."}],
                is_meta=True,
                source_tool_use_id="toolu_MISSING",
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "t.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        slash = [
            m
            for m in ctx.messages
            if m is not None and isinstance(m.content, UserSlashCommandMessage)
        ]
        # No pair found → slash-command is not consumed, renders standalone.
        assert len(slash) == 1

    def test_same_tool_use_id_across_sessions_does_not_cross_pair(
        self, tmp_path: Path
    ) -> None:
        """Two independent sessions reusing the same tool_use_id keep their
        Skill bodies separate. The lookup key must be (session_id, tool_use_id)
        — combined transcripts traverse multiple sessions, and Anthropic
        tool_use ids are only session-unique. A global key would let session
        B's slash body fold into session A's Skill (or vice versa) on a stray
        collision.
        """
        # Session A: Skill tool_use + body A.
        session_a = "sess-a"
        body_a = "# Body A\n\nfrom session A."
        entries_a = [
            _user(
                "ua-001",
                None,
                "2026-01-01T10:00:00Z",
                [{"type": "text", "text": "Go A"}],
                session_id=session_a,
            ),
            _assistant_tool_use(
                "aa-001",
                "ua-001",
                "2026-01-01T10:00:01Z",
                "Skill",
                "toolu_DUP",
                {"skill": "alpha"},
                session_id=session_a,
            ),
            _user(
                "ua-002",
                "aa-001",
                "2026-01-01T10:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_DUP",
                        "content": "Launching skill: alpha",
                        "is_error": False,
                    }
                ],
                session_id=session_a,
            ),
            _user(
                "ua-003",
                "ua-002",
                "2026-01-01T10:00:03Z",
                [{"type": "text", "text": body_a}],
                is_meta=True,
                source_tool_use_id="toolu_DUP",
                session_id=session_a,
            ),
        ]
        # Session B: same tool_use_id, different body.
        session_b = "sess-b"
        body_b = "# Body B\n\nfrom session B."
        entries_b = [
            _user(
                "ub-001",
                None,
                "2026-01-01T11:00:00Z",
                [{"type": "text", "text": "Go B"}],
                session_id=session_b,
            ),
            _assistant_tool_use(
                "ab-001",
                "ub-001",
                "2026-01-01T11:00:01Z",
                "Skill",
                "toolu_DUP",
                {"skill": "beta"},
                session_id=session_b,
            ),
            _user(
                "ub-002",
                "ab-001",
                "2026-01-01T11:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_DUP",
                        "content": "Launching skill: beta",
                        "is_error": False,
                    }
                ],
                session_id=session_b,
            ),
            _user(
                "ub-003",
                "ub-002",
                "2026-01-01T11:00:03Z",
                [{"type": "text", "text": body_b}],
                is_meta=True,
                source_tool_use_id="toolu_DUP",
                session_id=session_b,
            ),
        ]
        # Render both as a combined transcript.
        messages = load_transcript(
            _write_jsonl(tmp_path / "combined.jsonl", entries_a + entries_b)
        )
        _, _, ctx = generate_template_messages(messages)

        skill_uses_by_session: dict[str, ToolUseMessage] = {}
        for m in ctx.messages:
            if m is None:
                continue
            if isinstance(m.content, ToolUseMessage) and m.content.tool_name == "Skill":
                skill_uses_by_session[m.meta.session_id] = m.content
        assert set(skill_uses_by_session) == {session_a, session_b}, (
            f"Both Skill tool_uses should survive — got {set(skill_uses_by_session)}"
        )
        # Each Skill keeps its OWN session's body, not the other session's.
        assert skill_uses_by_session[session_a].skill_body == body_a
        assert skill_uses_by_session[session_b].skill_body == body_b

    def test_error_tool_result_with_same_id_is_preserved(self, tmp_path: Path) -> None:
        """A real error tool_result sharing the Skill's tool_use_id must NOT
        be silently dropped — even though the canonical 'Launching skill:'
        result IS dropped. Without the is_error guard, a Skill that failed
        to launch would lose the error message entirely.
        """
        skill_body = "# Real Body\n\npaired with the launch result."
        entries = [
            _user(
                "u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]
            ),
            _assistant_tool_use(
                "a-001",
                "u-001",
                "2026-01-01T10:00:01Z",
                "Skill",
                "toolu_ERR",
                {"skill": "broken"},
            ),
            # Canonical "Launching skill:" result — should be dropped.
            _user(
                "u-002",
                "a-001",
                "2026-01-01T10:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_ERR",
                        "content": "Launching skill: broken",
                        "is_error": False,
                    }
                ],
            ),
            # Error result with the SAME tool_use_id — must survive.
            _user(
                "u-003",
                "u-002",
                "2026-01-01T10:00:03Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_ERR",
                        "content": "Skill 'broken' not found",
                        "is_error": True,
                    }
                ],
            ),
            _user(
                "u-004",
                "u-003",
                "2026-01-01T10:00:04Z",
                [{"type": "text", "text": skill_body}],
                is_meta=True,
                source_tool_use_id="toolu_ERR",
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "t.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        results = [
            m.content
            for m in ctx.messages
            if m is not None
            and isinstance(m.content, ToolResultMessage)
            and m.content.tool_use_id == "toolu_ERR"
        ]
        assert len(results) == 1, (
            "Exactly the error result should survive; the canonical "
            "'Launching skill:' result should be dropped"
        )
        assert results[0].is_error is True

    def test_non_launching_skill_result_with_same_id_is_preserved(
        self, tmp_path: Path
    ) -> None:
        """A tool_result with the Skill's tool_use_id but a payload that
        does NOT start with 'Launching skill:' must NOT be dropped. The
        canonical-payload prefix check defends against a malformed transcript
        where some other content shares the id."""
        skill_body = "# Body\n\nbody text."
        entries = [
            _user(
                "u-001", None, "2026-01-01T10:00:00Z", [{"type": "text", "text": "Go"}]
            ),
            _assistant_tool_use(
                "a-001",
                "u-001",
                "2026-01-01T10:00:01Z",
                "Skill",
                "toolu_ODD",
                {"skill": "weird"},
            ),
            # Divergent (non-canonical) tool_result sharing the id — must survive.
            _user(
                "u-002",
                "a-001",
                "2026-01-01T10:00:02Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_ODD",
                        "content": "Some unrelated payload",
                        "is_error": False,
                    }
                ],
            ),
            _user(
                "u-003",
                "u-002",
                "2026-01-01T10:00:03Z",
                [{"type": "text", "text": skill_body}],
                is_meta=True,
                source_tool_use_id="toolu_ODD",
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "t.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        results = [
            m.content
            for m in ctx.messages
            if m is not None
            and isinstance(m.content, ToolResultMessage)
            and m.content.tool_use_id == "toolu_ODD"
        ]
        assert len(results) == 1
        # The non-canonical payload survived.
        from claude_code_log.models import ToolResultContent

        output = results[0].output
        assert isinstance(output, ToolResultContent)
        assert output.content == "Some unrelated payload"


class TestSkillFoldOnFork:
    """The D12-prerequisite test the verifier flagged as missing.

    Skill-fold happens inside a within-session branch, rendered at
    FULL detail. Under the ghosting model,
    ``_pair_skill_tool_uses`` ghosts the consumed slots in place
    instead of deleting + reindexing — so the branch header's
    ``parent_message_index`` (set at register time in
    ``_render_messages``) is never touched. The fork-anchor index
    stays valid because no slot before it was deleted.

    The test asserts the visible-output contract: skill folded,
    slash + launching-skill tool_result ghosted, branch header
    backlink resolves to the actual fork anchor (NOT to whatever
    shifted into the old index after a phantom reindex). This is
    the failure mode PR #131 introduced (under the old reindex
    path) and the verifier called out as missing coverage for D12;
    landing it under the ghosting path means D12 inherits the
    coverage when it lands.
    """

    def test_skill_fold_inside_fork_at_full_keeps_branch_backref(
        self, tmp_path: Path
    ) -> None:
        from claude_code_log.models import SessionHeaderMessage, ToolUseMessage

        # Trunk: u-trunk-1 → a-trunk-1 → c (fork point).
        # Both children of c are ASSISTANTS — bypasses the DAG's
        # fork-collapse heuristic that absorbs an assistant +
        # user-child pair as a tool-result side-branch (see
        # ``_stitch_tool_results`` in dag.py). With two assistant
        # siblings + distinct timestamps, the DAG sees a real fork.
        # Branch 1: a-skill (assistant Skill tool_use) → u-launch
        #   tool_result → u-meta-body (isMeta sourceToolUseID) →
        #   a-skill-reply.
        # Branch 2 sibling: a-other (assistant text) → u-other-reply.
        sid = "sess-fork-skill"
        tool_id = "toolu_SKILL_F"
        skill_body = "# Forky Skill\n\nBody **inside** the branch."
        entries = [
            _user(
                "u-trunk-1",
                None,
                "2026-01-01T10:00:00Z",
                [{"type": "text", "text": "hello"}],
                session_id=sid,
            ),
            _assistant_text(
                "a-trunk-1", "u-trunk-1", "2026-01-01T10:00:01Z", "ok", session_id=sid
            ),
            _user(
                "c",
                "a-trunk-1",
                "2026-01-01T10:00:02Z",
                [{"type": "text", "text": "fork point"}],
                session_id=sid,
            ),
            # ---- Branch 1: Skill spawn inside the branch
            _assistant_tool_use(
                "a-skill",
                "c",
                "2026-01-01T10:00:03Z",
                "Skill",
                tool_id,
                {"skill": "forky:skill", "args": ""},
                session_id=sid,
            ),
            _user(
                "u-launch",
                "a-skill",
                "2026-01-01T10:00:04Z",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "Launching skill: forky:skill",
                        "is_error": False,
                    }
                ],
                session_id=sid,
            ),
            _user(
                "u-meta-body",
                "u-launch",
                "2026-01-01T10:00:05Z",
                [{"type": "text", "text": skill_body}],
                is_meta=True,
                source_tool_use_id=tool_id,
                session_id=sid,
            ),
            _assistant_text(
                "a-skill-reply",
                "u-meta-body",
                "2026-01-01T10:00:06Z",
                "skill ran",
                session_id=sid,
            ),
            # ---- Branch 2 sibling: starts with an ASSISTANT
            # (same role as branch 1's first entry) to bypass the
            # fork-collapse heuristic, then a user reply so the
            # branch isn't a single-entry dead-end.
            _assistant_text(
                "a-other",
                "c",
                "2026-01-01T10:00:07Z",
                "other path",
                session_id=sid,
            ),
            _user(
                "u-other-reply",
                "a-other",
                "2026-01-01T10:00:08Z",
                [{"type": "text", "text": "other reply"}],
                session_id=sid,
            ),
        ]
        messages = load_transcript(_write_jsonl(tmp_path / "fork-skill.jsonl", entries))
        _, _, ctx = generate_template_messages(messages)

        # === 1) Skill body folded into the Skill tool_use.
        skill_tool_uses = [
            m.content
            for m in ctx.messages
            if m is not None
            and isinstance(m.content, ToolUseMessage)
            and m.content.tool_name == "Skill"
        ]
        assert len(skill_tool_uses) == 1, (
            f"expected exactly one Skill tool_use, got {len(skill_tool_uses)}"
        )
        assert skill_tool_uses[0].skill_body == skill_body, (
            "Skill body should be folded onto the tool_use's skill_body field; "
            f"got skill_body={skill_tool_uses[0].skill_body!r}"
        )

        # === 2) The slash-command body slot AND the launching-skill
        # tool_result slot are GHOSTED (None) — not removed from
        # ctx.messages. This is the new ghosting contract; pre-
        # Phase-1 the consumed slots were deleted + reindexed.
        ghost_slots = [i for i, m in enumerate(ctx.messages) if m is None]
        assert len(ghost_slots) == 2, (
            f"expected exactly 2 ghosted slots (slash body + "
            f"launching-skill tool_result); got {len(ghost_slots)} at {ghost_slots}. "
            f"ctx.messages length = {len(ctx.messages)}"
        )

        # No surviving UserSlashCommandMessage (the slash body is the ghost).
        survivors = [m for m in ctx.messages if m is not None]
        from claude_code_log.models import UserSlashCommandMessage as _USM

        assert not any(isinstance(m.content, _USM) for m in survivors), (
            "the slash body should be ghosted, not surviving in ctx.messages"
        )

        # Pin that BOTH consumed slots are ghosted — not just the slash
        # body — so the "exactly 2 ghosts" count above can't be satisfied
        # vacuously by ghosting some unrelated slot. Assert by uuid-absence:
        # the launch tool_result lives in entry 'u-launch', the slash body
        # in 'u-meta-body'.
        survivor_uuids = {m.meta.uuid for m in survivors}
        assert "u-launch" not in survivor_uuids, (
            "the canonical 'Launching skill:' tool_result (uuid 'u-launch') "
            "should be ghosted, not surviving in ctx.messages"
        )
        assert "u-meta-body" not in survivor_uuids, (
            "the slash-command body (uuid 'u-meta-body') should be ghosted"
        )

        # === 3) The within-session branch header for the Skill-
        # bearing branch must resolve its parent_message_index to
        # the fork-anchor message (uuid 'c'), NOT to a phantom slot.
        # This is the PR #131 regression class under the ghosting
        # path: pre-Phase-1 a buggy reindex could shift the cached
        # parent_message_index; post-Phase-1 there's no reindex at
        # all, so the index set at register time stays valid as
        # long as the fork anchor itself wasn't ghosted (it wasn't —
        # the trunk-side anchor is never a Skill-fold target).
        branch_headers = [
            m
            for m in survivors
            if isinstance(m.content, SessionHeaderMessage) and m.content.is_branch
        ]
        # The Skill-bearing branch is the one rooted at 'a-skill'.
        # (Branch sids are ``{trunk}@{first_uuid12}`` per dag.py.)
        skill_branches = [
            m for m in branch_headers if m.content.session_id.endswith("@a-skill")
        ]
        assert len(skill_branches) == 1, (
            f"expected exactly one branch header rooted at 'a-skill'; got "
            f"{[m.content.session_id for m in branch_headers]}"
        )
        skill_branch = skill_branches[0]
        parent_idx = skill_branch.content.parent_message_index
        assert parent_idx is not None, (
            "skill-bearing branch header's parent_message_index is None — "
            "expected it to resolve to the fork anchor 'c'."
        )
        parent_msg = ctx.get(parent_idx)
        assert parent_msg is not None, (
            f"branch header parent_message_index={parent_idx} resolved to None — "
            "the fork anchor was either ghosted (unexpected) or the index was "
            "phantom-shifted."
        )
        assert parent_msg.meta.uuid == "c", (
            f"branch header parent_message_index={parent_idx} resolves to "
            f"uuid={parent_msg.meta.uuid!r}, expected 'c' (the fork anchor). "
            "This would surface as a wrong 'from #msg-d-N' backlink in the "
            "rendered branch header — exactly the PR #131 failure class."
        )


class TestSkillFoldGhostAnchorRepair:
    """``_pair_skill_tool_uses`` ghosts the slash body + launch tool_result
    *in place* (None slots). Anchor-target refs cached earlier — a branch
    header's ``parent_message_index`` and the ``session_first_message`` map,
    both populated in ``_render_messages`` before this pass — may point at a
    slot we just ghosted. The rendered ``#msg-d-{N}`` backlink is emitted
    from that raw index, so a ``ctx.get()`` that returns None for a ghost
    does NOT suppress the href; the ref itself must be nulled.

    This is the exposure CodeRabbit flagged on #193 (the broader
    ``_repair_stale_anchor_refs`` lives in Phase 2). Constructed via a
    manual context — the same unit style as ``TestReindexBranchBackrefs``
    — to land a fork point precisely on a soon-to-be-ghosted skill slot
    without fighting the DAG's fork-collapse heuristic.
    """

    def test_anchor_ref_into_ghosted_skill_slot_is_nulled(self) -> None:
        from claude_code_log.models import (
            MessageMeta,
            SessionHeaderMessage,
            ToolResultContent,
            ToolResultMessage,
            ToolUseContent,
            ToolUseMessage,
            UserSlashCommandMessage,
        )
        from claude_code_log.renderer import (
            RenderingContext,
            TemplateMessage,
            _pair_skill_tool_uses,
        )

        sid = "root"
        tid = "skill-tool-1"
        ctx = RenderingContext()

        def _reg(content: object) -> TemplateMessage:
            msg = TemplateMessage(content)  # type: ignore[arg-type]
            ctx.register(msg)
            return msg

        # idx 0: trunk user
        _reg(
            UserSlashCommandMessage(
                MessageMeta(session_id=sid, timestamp="", uuid="u-trunk"), text="hi"
            )
        )
        # idx 1: Skill tool_use
        _reg(
            ToolUseMessage(
                MessageMeta(session_id=sid, timestamp="", uuid="a-skill"),
                input=ToolUseContent(
                    type="tool_use",
                    id=tid,
                    name="Skill",
                    input={"skill": "forky:skill", "args": ""},
                ),
                tool_use_id=tid,
                tool_name="Skill",
            )
        )
        # idx 2: canonical launch tool_result — ghosted AND the fork anchor.
        launch = _reg(
            ToolResultMessage(
                MessageMeta(session_id=sid, timestamp="", uuid="u-launch"),
                tool_use_id=tid,
                output=ToolResultContent(
                    type="tool_result",
                    tool_use_id=tid,
                    content="Launching skill: forky:skill",
                    is_error=False,
                ),
            )
        )
        # idx 3: slash body — ghosted.
        slash = _reg(
            UserSlashCommandMessage(
                MessageMeta(
                    session_id=sid,
                    timestamp="",
                    uuid="u-meta-body",
                    is_meta=True,
                    source_tool_use_id=tid,
                ),
                text="# expanded skill body",
            )
        )
        # idx 4: a branch header whose fork anchor IS the launch tool_result.
        branch = _reg(
            SessionHeaderMessage(
                MessageMeta(session_id=f"{sid}@a-skill", timestamp="", uuid=""),
                title="Branch • test",
                session_id=f"{sid}@a-skill",
                parent_session_id=sid,
                parent_message_index=launch.message_index,
                attachment_uuid="u-launch",
                is_branch=True,
            )
        )

        assert launch.message_index is not None and slash.message_index is not None
        ctx.session_first_message[sid] = 0
        # A session whose first message resolves to a soon-ghosted slot.
        ctx.session_first_message["ghost-first"] = launch.message_index

        _pair_skill_tool_uses(ctx)

        # Both consumed slots ghosted in place.
        assert ctx.messages[launch.message_index] is None, (
            "launch tool_result slot should be ghosted (None)"
        )
        assert ctx.messages[slash.message_index] is None, (
            "slash-body slot should be ghosted (None)"
        )

        # THE GUARD: the branch backlink into the ghosted fork anchor is
        # nulled so the rendered '#msg-d-{N}' href doesn't dangle.
        assert isinstance(branch.content, SessionHeaderMessage)
        assert branch.content.parent_message_index is None, (
            "branch header parent_message_index still points at a ghosted "
            f"slot (={branch.content.parent_message_index}) — the dead-anchor "
            "guard in _pair_skill_tool_uses did not run."
        )

        # session_first_message entries resolving to a ghost are dropped;
        # the live one survives.
        assert "ghost-first" not in ctx.session_first_message, (
            "session_first_message kept an entry pointing at a ghosted slot"
        )
        assert ctx.session_first_message.get(sid) == 0, (
            "the live session_first_message entry was incorrectly dropped"
        )

    def test_session_nav_omits_anchor_for_ghosted_fork_point(self) -> None:
        """`prepare_session_navigation` must NOT retarget a ghosted fork
        point's nav anchor at the parent session header.

        The fork-point nav item resolves its anchor by scanning
        `_visible(ctx.messages)` for `attachment_uuid`. When that message
        was ghosted (e.g. a folded Skill slot), the scan can't find it, so
        `message_index` must stay None (anchor omitted) — NOT fall back to
        `session_first_message[parent_sid]`, which would silently undo
        `_drop_anchor_refs_into_ghosts` and point the fork link at the
        parent session header. Regression for the CodeRabbit finding on
        the #193 fix.
        """
        from claude_code_log.models import (
            MessageMeta,
            SessionHeaderMessage,
            UserSlashCommandMessage,
        )
        from claude_code_log.renderer import (
            RenderingContext,
            TemplateMessage,
            prepare_session_navigation,
        )

        ctx = RenderingContext()

        def _reg(content: object) -> TemplateMessage:
            msg = TemplateMessage(content)  # type: ignore[arg-type]
            ctx.register(msg)
            return msg

        # idx 0: parent session header (a live anchor we must NOT fall back to)
        _reg(
            SessionHeaderMessage(
                MessageMeta(session_id="root", timestamp="", uuid="root-hdr"),
                title="root",
                session_id="root",
            )
        )
        # idx 1: the fork point — ghosted below.
        fork_pt = _reg(
            UserSlashCommandMessage(
                MessageMeta(session_id="root", timestamp="", uuid="fork-pt"),
                text="x",
            )
        )
        # idx 2: branch header.
        _reg(
            SessionHeaderMessage(
                MessageMeta(session_id="root@b", timestamp="", uuid="branch-hdr"),
                title="Branch • b",
                session_id="root@b",
                parent_session_id="root",
                attachment_uuid="fork-pt",
                is_branch=True,
            )
        )

        ctx.session_first_message["root"] = 0
        ctx.session_first_message["root@b"] = 2
        # Ghost the fork point, mirroring what _pair_skill_tool_uses does.
        assert fork_pt.message_index is not None
        ctx.messages[fork_pt.message_index] = None

        sessions = {
            "root": {
                "first_user_message": "hello",
                "first_timestamp": "",
                "last_timestamp": "",
                "summary": None,
                "message_count": 1,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
            }
        }
        session_hierarchy = {
            "root": {"depth": 0},
            "root@b": {
                "is_branch": True,
                "attachment_uuid": "fork-pt",
                "parent_session_id": "root",
                "depth": 1,
            },
        }

        nav = prepare_session_navigation(sessions, ["root"], ctx, session_hierarchy)

        fork_items = [n for n in nav if n.get("is_fork_point")]
        assert len(fork_items) == 1, (
            f"expected exactly one fork-point nav item; got {len(fork_items)}"
        )
        fork = fork_items[0]
        assert fork["message_index"] is None, (
            f"fork-point nav anchor points at message_index={fork['message_index']} "
            "(the parent session header) instead of being omitted — the ghosted "
            "fork point was retargeted, undoing the anchor repair."
        )
        assert fork["parent_message_index"] is None, (
            "fork-point nav parent_message_index fell back to the parent header"
        )


# -- Renderer output ---------------------------------------------------------


class TestSkillPairingHtml:
    def test_skill_body_appears_in_tool_use_block(self, tmp_path: Path) -> None:
        body = "# Mail reader\n\nReads a mail by **id**."
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="clmail:read", skill_body=body
            )
        )
        html = HtmlRenderer().generate(messages, "Skill pairing HTML")

        # Body is rendered as markdown inside the skill-body container
        assert "skill-body" in html
        assert "<strong>id</strong>" in html  # ** → <strong>
        assert "Mail reader" in html
        # Standalone slash-command rendering is gone — no slash-command CSS class
        # on a top-level message (the class only appears inside the skill-body
        # container's rendered markdown, which doesn't use it).
        # The redundant "Launching skill" string is gone too.
        assert "Launching skill" not in html

    def test_skill_title_folds_skill_name(self, tmp_path: Path) -> None:
        """Title surfaces ``💡 Skill <name>`` and the params row is suppressed."""
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="clmail:read", skill_body="# body"
            )
        )
        html = HtmlRenderer().generate(messages, "Skill pairing HTML")

        # Title carries the skill-name as the tool-summary span next to "Skill"
        assert "💡 Skill" in html
        assert "clmail:read" in html
        # No params table row labelled "skill"
        assert ">skill</td>" not in html

    def test_skill_with_extra_args_field_still_typed(self, tmp_path: Path) -> None:
        """Real Skill invocations carry an ``args`` string alongside ``skill``;
        the typed model must accept that without falling back to ToolUseContent."""
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="my-worktree-actors", skill_body="x"
            )
        )
        html = HtmlRenderer().generate(messages, "Skill pairing HTML")
        # The fixture passes `{"skill": skill_name, "args": ""}` (see
        # `_skill_invocation_jsonl`); without `extra="allow"` on SkillInput,
        # validation would fail and the message would render with the generic
        # tool emoji 🛠️ instead of the skill-specific 💡.
        assert "💡 Skill" in html


class TestSkillPairingMarkdown:
    def test_skill_body_appears_under_tool_use(self, tmp_path: Path) -> None:
        body = "# Mail reader\n\nReads a mail by **id**."
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="clmail:read", skill_body=body
            )
        )
        md = MarkdownRenderer().generate(messages, "Skill pairing MD")

        # Markdown body passes through verbatim.
        assert "# Mail reader" in md
        assert "**id**" in md
        assert "Launching skill" not in md

    def test_skill_title_folds_skill_name(self, tmp_path: Path) -> None:
        messages = load_transcript(
            _skill_invocation_jsonl(
                tmp_path / "t.jsonl", skill_name="clmail:read", skill_body="# body"
            )
        )
        md = MarkdownRenderer().generate(messages, "Skill pairing MD")
        assert "💡 Skill `clmail:read`" in md
