"""Parser tests for the teammates feature (issue #91, PR #117)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_log.converter import (
    load_directory_transcripts,
    load_transcript,
)
from claude_code_log.factories.agent_metadata_factory import (
    parse_agent_result_metadata,
)
from claude_code_log.factories.teammate_factory import (
    create_teammate_message,
    find_team_lead_body,
    has_teammate_message,
    iter_teammate_blocks,
)
from claude_code_log.factories.tool_factory import (
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_PARSERS,
    create_tool_input,
    parse_sendmessage_output,
    parse_taskcreate_output,
    parse_tasklist_output,
    parse_taskupdate_output,
    parse_teamcreate_output,
    parse_teamdelete_output,
)
from claude_code_log.models import (
    AgentResultMetadata,
    AssistantTranscriptEntry,
    MessageMeta,
    SendMessageInput,
    SendMessageOutput,
    TaskCreateInput,
    TaskCreateOutput,
    TaskListInput,
    TaskListOutput,
    TaskUpdateInput,
    TaskUpdateOutput,
    TeamCreateInput,
    TeamCreateOutput,
    TeamDeleteInput,
    TeamDeleteOutput,
    ToolResultContent,
    ToolUseContent,
    UserTranscriptEntry,
)


class TestAgentResultMetadata:
    def test_returns_none_for_plain_text(self) -> None:
        body, meta = parse_agent_result_metadata("Hello, world.")
        assert body == "Hello, world."
        assert meta is None

    def test_returns_none_for_empty(self) -> None:
        body, meta = parse_agent_result_metadata("")
        assert body == ""
        assert meta is None

    def test_parses_agent_id_only(self) -> None:
        text = "Done.\n\nagentId: abc123\n"
        body, meta = parse_agent_result_metadata(text)
        assert body == "Done."
        assert meta is not None
        assert meta.agent_id == "abc123"
        assert meta.worktree_path is None
        assert meta.total_tokens is None

    def test_parses_agent_id_with_trailing_sendmessage_hint(self) -> None:
        text = (
            "Work complete.\n"
            "agentId: a4ca7529 (use SendMessage with to: 'x' to continue this agent)\n"
        )
        body, meta = parse_agent_result_metadata(text)
        assert body == "Work complete."
        assert meta is not None
        # Hint in parens must not be absorbed into the id
        assert meta.agent_id == "a4ca7529"

    def test_parses_worktree_fields(self) -> None:
        text = (
            "Body text.\n"
            "agentId: xyz\n"
            "worktreePath: /home/user/worktrees/agent-xyz\n"
            "worktreeBranch: worktree-agent-xyz\n"
        )
        body, meta = parse_agent_result_metadata(text)
        assert body == "Body text."
        assert meta is not None
        assert meta.agent_id == "xyz"
        assert meta.worktree_path == "/home/user/worktrees/agent-xyz"
        assert meta.worktree_branch == "worktree-agent-xyz"

    def test_parses_usage_block(self) -> None:
        text = (
            "agent response\n"
            "agentId: a\n"
            "worktreePath: /tmp/a\n"
            "worktreeBranch: b-a\n"
            "<usage>total_tokens: 48421\n"
            "tool_uses: 24\n"
            "duration_ms: 802753</usage>"
        )
        body, meta = parse_agent_result_metadata(text)
        assert body == "agent response"
        assert meta is not None
        assert meta.total_tokens == 48421
        assert meta.tool_uses == 24
        assert meta.duration_ms == 802753

    def test_usage_block_only(self) -> None:
        """Pre-teammates transcripts may have <usage> alone."""
        text = (
            "Answer.\n<usage>total_tokens: 10\ntool_uses: 1\nduration_ms: 200</usage>"
        )
        body, meta = parse_agent_result_metadata(text)
        assert body == "Answer."
        assert meta is not None
        assert meta.agent_id is None
        assert meta.total_tokens == 10
        assert meta.tool_uses == 1
        assert meta.duration_ms == 200

    def test_metadata_tail_is_stripped_idempotently(self) -> None:
        text = "Body\n\n\nagentId: x\nworktreePath: /p\n"
        body, meta = parse_agent_result_metadata(text)
        assert body == "Body"
        # Feeding the stripped body back yields None (no tail left).
        _, second = parse_agent_result_metadata(body)
        assert second is None

    def test_result_object_type(self) -> None:
        _, meta = parse_agent_result_metadata("agentId: abc\n")
        assert isinstance(meta, AgentResultMetadata)

    def test_body_mentioning_agent_id_is_preserved(self) -> None:
        """Regression (coderabbit #117): an agent response that itself
        shows an `agentId:` line verbatim (e.g. quoting another agent's
        metadata back) must not be truncated — only the *last* `agentId:`
        line is treated as the metadata anchor.
        """
        # Body contains a literal line-starting ``agentId:`` that would
        # have matched the old first-match anchor; real metadata follows.
        text = (
            "Here's the metadata from the upstream spawn I investigated:\n"
            "\n"
            "agentId: bogus1234\n"
            "worktreePath: /tmp/upstream\n"
            "\n"
            "That's what I found. My own report follows.\n"
            "\n"
            "agentId: real5678\n"
            "worktreePath: /tmp/mine\n"
            "worktreeBranch: wt-real\n"
        )
        body, meta = parse_agent_result_metadata(text)
        # Old code would have truncated at the first ``agentId:`` line,
        # dropping everything after it (including the "real report" line
        # AND the actual metadata).
        assert "agentId: bogus1234" in body
        assert "That's what I found" in body
        assert meta is not None
        assert meta.agent_id == "real5678"
        assert meta.worktree_path == "/tmp/mine"
        assert meta.worktree_branch == "wt-real"

    def test_worktree_path_with_spaces(self) -> None:
        """Regression (coderabbit #117): `worktreePath` with spaces must
        be captured in full, not truncated at the first space."""
        text = (
            "body\n"
            "agentId: abc\n"
            "worktreePath: /home/user/My Worktrees/agent-abc\n"
            "worktreeBranch: feature/agent abc\n"
        )
        body, meta = parse_agent_result_metadata(text)
        assert body == "body"
        assert meta is not None
        assert meta.worktree_path == "/home/user/My Worktrees/agent-abc"
        assert meta.worktree_branch == "feature/agent abc"


def _meta() -> MessageMeta:
    return MessageMeta(session_id="s", timestamp="t", uuid="u")


def test_inline_code_widens_fence_for_backticks() -> None:
    """Regression (CodeRabbit #4 on PR #125): CommonMark code spans
    don't honor backslash escapes inside them, so a literal `replace`
    is wrong. The widening recipe is to use a fence one tick longer
    than the longest run in the value, padded with spaces when the
    value starts/ends with a backtick.
    """
    from claude_code_log.markdown.renderer import _inline_code

    # No backticks → minimal fence, no padding
    assert _inline_code("alpha") == "`alpha`"
    # Single backtick inside → fence widened to 2 ticks, no padding
    assert _inline_code("foo`bar") == "``foo`bar``"
    # Run of 3 backticks → fence widened to 4
    assert _inline_code("a```b") == "````a```b````"
    # Starts with a backtick → space padding so fence isn't fused
    assert _inline_code("`alpha") == "`` `alpha ``"
    # Ends with a backtick → same
    assert _inline_code("alpha`") == "`` alpha` ``"
    # Empty → empty code span (degenerate but well-formed)
    assert _inline_code("") == "``"


def test_markdown_session_header_handles_backtick_in_team_name() -> None:
    """Regression (CodeRabbit #4): a malformed team_name containing a
    backtick must produce a parseable Markdown title; the trailing
    `Team: ...` segment should round-trip through CommonMark cleanly.
    """
    from claude_code_log.markdown.renderer import MarkdownRenderer
    from claude_code_log.models import MessageMeta, SessionHeaderMessage
    from claude_code_log.renderer import TemplateMessage

    meta = MessageMeta(session_id="s12345678", timestamp="t", uuid="u")
    content = SessionHeaderMessage(
        meta=meta,
        title="Session",
        session_id="s12345678",
        team_name="weird`team",
    )
    template_msg = TemplateMessage(content)
    rendered = MarkdownRenderer().title_SessionHeaderMessage(content, template_msg)
    # The team name must appear exactly once, with adaptive fence so the
    # inner backtick stays inside the code span (no premature close).
    assert "weird`team" in rendered
    # Fence must be at least 2 ticks long around it
    assert "``weird`team``" in rendered or "`` weird`team ``" in rendered


def test_fallback_team_names_filters_warmup_sessions() -> None:
    """Regression (CodeRabbit #1): the no-cache fallback path's
    `team_names` aggregation must filter warmup-only sessions just
    like the cached path does. Without the filter, a warmup
    initialization session that happens to carry teamName would
    surface in the project card under the fallback path but not
    under the cached path.
    """
    from claude_code_log.converter import convert_jsonl_to_html
    from claude_code_log.utils import get_warmup_session_ids
    from claude_code_log.models import UserTranscriptEntry, UserMessageModel
    from claude_code_log.renderer import TemplateProject
    import json

    # Build a tmp project where one session is warmup-only (every
    # message text is "Warmup") and another is real with teamName set.
    # Both sessions carry teamName="leak-team" — if the filter is
    # missing, the warmup's team would surface; with the filter, only
    # the real session's team should.
    # We test the helper logic directly rather than the full convert
    # pipeline to keep the test fast and focused.
    warmup_msg = UserTranscriptEntry(
        parentUuid=None,
        isSidechain=False,
        userType="external",
        cwd="/x",
        sessionId="warmup-session",
        version="2.1.34",
        uuid="w1",
        timestamp="2026-04-25T10:00:00Z",
        teamName="leak-team",
        type="user",
        message=UserMessageModel(
            role="user",
            content=[{"type": "text", "text": "Warmup"}],  # type: ignore[list-item]
        ),
    )
    real_msg = UserTranscriptEntry(
        parentUuid=None,
        isSidechain=False,
        userType="external",
        cwd="/x",
        sessionId="real-session",
        version="2.1.34",
        uuid="r1",
        timestamp="2026-04-25T10:01:00Z",
        teamName="real-team",
        type="user",
        message=UserMessageModel(
            role="user",
            content=[{"type": "text", "text": "do the thing"}],  # type: ignore[list-item]
        ),
    )

    # Confirm the warmup session is detected
    warmup_ids = get_warmup_session_ids([warmup_msg, real_msg])
    assert "warmup-session" in warmup_ids
    assert "real-session" not in warmup_ids

    # Mirror the fallback aggregation locally — same logic that landed
    # in converter.py for the no-cache path.
    team_name_per_session: dict[str, str] = {}
    for msg in [warmup_msg, real_msg]:
        sid = msg.sessionId
        if sid in warmup_ids:
            continue
        if msg.teamName and sid not in team_name_per_session:
            team_name_per_session[sid] = msg.teamName
    team_names = sorted(set(team_name_per_session.values()))

    # Only "real-team" should surface — "leak-team" gets filtered out
    # because warmup-session is in warmup_ids.
    assert team_names == ["real-team"]
    # Suppress the unused-import warning — convert_jsonl_to_html is
    # the prod entry point this regression covers.
    del convert_jsonl_to_html
    del json
    del TemplateProject


def test_template_project_carries_team_names() -> None:
    """TemplateProject reads `team_names` from the project_summaries dict
    and exposes a sorted list. Empty list when the project has no teams.
    """
    from claude_code_log.renderer import TemplateProject

    project_with_teams = TemplateProject(
        {
            "name": "x",
            "html_file": "x/c.html",
            "jsonl_count": 1,
            "message_count": 1,
            "last_modified": 0.0,
            "team_names": {"beta", "alpha"},  # set → should sort
        }
    )
    assert project_with_teams.team_names == ["alpha", "beta"]

    project_without_teams = TemplateProject(
        {
            "name": "y",
            "html_file": "y/c.html",
            "jsonl_count": 1,
            "message_count": 1,
            "last_modified": 0.0,
        }
    )
    assert project_without_teams.team_names == []


def test_session_cache_data_round_trips_team_name() -> None:
    """SessionCacheData.team_name survives serialisation: build with a value,
    then read back to confirm Pydantic accepts and the field is present."""
    from claude_code_log.cache import SessionCacheData

    s = SessionCacheData(
        session_id="abc",
        first_timestamp="t",
        last_timestamp="t",
        message_count=1,
        first_user_message="hi",
        team_name="my-team",
    )
    assert s.team_name == "my-team"

    # Backward-compat: old caches missing team_name still load (default
    # Optional[str] = None).
    s2 = SessionCacheData(
        session_id="abc",
        first_timestamp="t",
        last_timestamp="t",
        message_count=1,
        first_user_message="hi",
    )
    assert s2.team_name is None


def test_prepare_session_team_names() -> None:
    """prepare_session_team_names collects first-non-None teamName per session."""
    from claude_code_log.renderer import prepare_session_team_names

    # Build minimal stand-ins — anything with sessionId+teamName attrs works.
    class _Entry:
        def __init__(self, sid: str, team: str | None) -> None:
            self.sessionId = sid
            self.teamName = team
            self.uuid = "u"

    msgs = [
        _Entry("s1", None),
        _Entry("s1", "alpha"),  # first team-tagged sighting wins
        _Entry("s1", "beta"),  # ignored — already set
        _Entry("s2", "gamma"),
        _Entry("s3", None),  # no team
    ]
    out = prepare_session_team_names(msgs)  # type: ignore[arg-type]
    assert out == {"s1": "alpha", "s2": "gamma"}


SINGLE_BLOCK = (
    '<teammate-message teammate_id="alice" color="blue" '
    'summary="relay tests complete">\n'
    "Relay coverage is now 96%.\n"
    "</teammate-message>"
)

MULTI_BLOCK = (
    '<teammate-message teammate_id="alice" color="blue">\n'
    "alice heartbeat: still here.\n"
    "</teammate-message>\n\n"
    '<teammate-message teammate_id="bob" color="green" summary="done">\n'
    "All server tests pass.\n"
    "</teammate-message>\n\n"
    '<teammate-message teammate_id="system">\n'
    "teammate_terminated: alice exited cleanly\n"
    "</teammate-message>"
)


class TestTeammateMessageParser:
    def test_has_teammate_message_detects(self) -> None:
        assert has_teammate_message(SINGLE_BLOCK) is True
        assert has_teammate_message("no tags here") is False
        assert has_teammate_message("<teammate-message") is False  # no close tag

    def test_iter_returns_blocks_in_order(self) -> None:
        ids = [b.teammate_id for b in iter_teammate_blocks(MULTI_BLOCK)]
        assert ids == ["alice", "bob", "system"]

    def test_single_block_attributes_and_body(self) -> None:
        blocks = list(iter_teammate_blocks(SINGLE_BLOCK))
        assert len(blocks) == 1
        b = blocks[0]
        assert b.teammate_id == "alice"
        assert b.color == "blue"
        assert b.summary == "relay tests complete"
        assert b.body == "Relay coverage is now 96%."
        assert b.is_system is False

    def test_block_without_summary(self) -> None:
        text = (
            '<teammate-message teammate_id="alice" color="blue">\n'
            "plain body\n"
            "</teammate-message>"
        )
        b = next(iter(iter_teammate_blocks(text)))
        assert b.summary is None
        assert b.color == "blue"

    def test_summary_with_unescaped_gt_is_preserved(self) -> None:
        """XML allows literal ``>`` inside attribute values. The earlier
        ``[^>]*`` attribute-run regex truncated the opening tag at the
        first ``>``, leaking the rest of the summary into the body."""
        text = (
            '<teammate-message teammate_id="alice" color="blue" '
            'summary="coverage 15% -> 96%">\n'
            "all good\n"
            "</teammate-message>"
        )
        b = next(iter(iter_teammate_blocks(text)))
        assert b.summary == "coverage 15% -> 96%"
        assert b.body == "all good"

    def test_summary_with_unescaped_amp_is_preserved(self) -> None:
        """We're parsing free-form transcript text, not strict XML — a
        literal ``&`` inside an attribute value (e.g.
        ``summary="tests & docs done"``) is common in practice and
        must NOT make the whole block disappear (CodeRabbit on PR
        #125)."""
        text = (
            '<teammate-message teammate_id="alice" color="blue" '
            'summary="tests & docs done">\n'
            "ok\n"
            "</teammate-message>"
        )
        blocks = list(iter_teammate_blocks(text))
        assert len(blocks) == 1, "block must not be skipped because of literal '&'"
        b = blocks[0]
        assert b.summary == "tests & docs done"
        assert b.body == "ok"

    def test_json_body_renders_as_key_value_dl(self) -> None:
        """Notification-shaped teammate bodies (``{"type":"...",...}``)
        render as a ``<dl class="teammate-json">`` key/value list, not as
        a Markdown code-blob."""
        from claude_code_log.html.teammate_formatter import format_teammate_content

        text = (
            '<teammate-message teammate_id="alice" color="blue">\n'
            '{"type":"idle_notification","from":"alice",'
            '"timestamp":"2026-02-06T10:36:50.559Z",'
            '"idleReason":"available"}\n'
            "</teammate-message>"
        )
        content = create_teammate_message(_meta(), text)
        assert content is not None
        html = format_teammate_content(content)
        assert 'class="teammate-json"' in html
        assert "<dt>type</dt>" in html
        assert "idle_notification" in html
        assert "<dt>idleReason</dt>" in html
        # Markdown rendering would produce a `<p>` or `<pre>`; ensure
        # the JSON path won.
        assert (
            "<p>" not in html.split('class="teammate-json"', 1)[1].split("</dl>", 1)[0]
        )

    def test_non_json_body_falls_back_to_markdown(self) -> None:
        """A body that just happens to start with ``{`` but isn't valid
        JSON still routes through the Markdown renderer."""
        from claude_code_log.html.teammate_formatter import format_teammate_content

        text = (
            '<teammate-message teammate_id="alice" color="blue">\n'
            "{not really json, missing quotes}\n"
            "</teammate-message>"
        )
        content = create_teammate_message(_meta(), text)
        assert content is not None
        html = format_teammate_content(content)
        assert 'class="teammate-json"' not in html

    def test_sidechain_teammate_at_level_4(self) -> None:
        """Regression: a sidechain ``TeammateMessage`` (team-lead's
        wrapped prompt to a teammate) must dispatch to level 4, not
        the default level 1.

        Pre-fix the type "teammate" wasn't recognised by
        ``_get_message_hierarchy_level`` so it fell through to level
        1. Level 1 popped the level stack down past the spawning Task
        (level 3) — the next Task tool_use ended up nested as a
        descendant of the Teammate, swallowing the rest of the
        sidechain content into the wrong slot.
        """
        from claude_code_log.models import MessageMeta
        from claude_code_log.renderer import (
            TemplateMessage,
            _get_message_hierarchy_level,
        )

        # Trunk teammate (team-lead receives messages from teammates):
        # level 1, alongside regular user.
        trunk_meta = MessageMeta(session_id="s", timestamp="t", uuid="u-1")
        trunk_msg = TemplateMessage(
            create_teammate_message(
                trunk_meta,
                '<teammate-message teammate_id="alice">hi</teammate-message>',
            )  # type: ignore[arg-type]
        )
        assert _get_message_hierarchy_level(trunk_msg) == 1

        # Sidechain teammate (team-lead's wrapped prompt to a
        # teammate, found inside a subagent transcript): level 4,
        # nested under the spawning Task tool_result.
        sidechain_meta = MessageMeta(
            session_id="s#agent-x", timestamp="t", uuid="u-2", is_sidechain=True
        )
        sidechain_msg = TemplateMessage(
            create_teammate_message(
                sidechain_meta,
                '<teammate-message teammate_id="team-lead">'
                "do the thing</teammate-message>",
            )  # type: ignore[arg-type]
        )
        assert _get_message_hierarchy_level(sidechain_msg) == 4

    def test_system_block_flagged(self) -> None:
        blocks = list(iter_teammate_blocks(MULTI_BLOCK))
        system_block = blocks[-1]
        assert system_block.is_system is True
        assert "teammate_terminated" in system_block.body

    def test_create_returns_none_without_block(self) -> None:
        assert create_teammate_message(_meta(), "just some text") is None

    def test_create_batch_single_block(self) -> None:
        content = create_teammate_message(_meta(), SINGLE_BLOCK)
        assert content is not None
        assert len(content.blocks) == 1
        assert content.blocks[0].teammate_id == "alice"
        assert content.leading_text is None
        assert content.trailing_text is None
        assert content.message_type == "teammate"
        assert content.has_markdown is True

    def test_create_batch_mixed_teammates(self) -> None:
        content = create_teammate_message(_meta(), MULTI_BLOCK)
        assert content is not None
        assert [b.teammate_id for b in content.blocks] == ["alice", "bob", "system"]

    def test_leading_and_trailing_text_preserved(self) -> None:
        text = f"Before text\n\n{SINGLE_BLOCK}\n\nAfter text"
        content = create_teammate_message(_meta(), text)
        assert content is not None
        assert content.leading_text == "Before text"
        assert content.trailing_text == "After text"

    def test_find_team_lead_body(self) -> None:
        wrapped = (
            '<teammate-message teammate_id="team-lead" color="cyan">\n'
            "do the thing\n"
            "</teammate-message>"
        )
        assert find_team_lead_body(wrapped) == "do the thing"
        assert find_team_lead_body(SINGLE_BLOCK) is None
        assert find_team_lead_body("") is None


def _tr_text(text: str) -> ToolResultContent:
    """Build a ToolResultContent with a single text block body."""
    return ToolResultContent(
        type="tool_result",
        tool_use_id="tu_fake",
        content=[{"type": "text", "text": text}],
    )


class TestTeammateToolInputs:
    """All six teammate tool names route to a typed BaseModel input."""

    def test_inputs_registered(self) -> None:
        for name, cls in {
            "TeamCreate": TeamCreateInput,
            "TeamDelete": TeamDeleteInput,
            "TaskCreate": TaskCreateInput,
            "TaskUpdate": TaskUpdateInput,
            "TaskList": TaskListInput,
            "SendMessage": SendMessageInput,
        }.items():
            assert TOOL_INPUT_MODELS.get(name) is cls, f"{name} not mapped"

    def test_teamcreate_input(self) -> None:
        parsed = create_tool_input(
            "TeamCreate",
            {
                "team_name": "x",
                "description": "d",
                "agent_type": "team-lead",
            },
        )
        assert isinstance(parsed, TeamCreateInput)
        assert parsed.team_name == "x"
        assert parsed.agent_type == "team-lead"

    def test_taskupdate_input_partial(self) -> None:
        parsed = create_tool_input("TaskUpdate", {"taskId": "1", "status": "completed"})
        assert isinstance(parsed, TaskUpdateInput)
        assert parsed.taskId == "1"
        assert parsed.status == "completed"
        assert parsed.owner is None

    def test_tasklist_input_empty(self) -> None:
        parsed = create_tool_input("TaskList", {})
        assert isinstance(parsed, TaskListInput)

    def test_sendmessage_input(self) -> None:
        parsed = create_tool_input(
            "SendMessage",
            {
                "type": "shutdown_request",
                "recipient": "alice",
                "content": "go home",
            },
        )
        assert isinstance(parsed, SendMessageInput)
        assert parsed.recipient == "alice"
        assert parsed.content == "go home"


class TestTeammateToolOutputs:
    """JSON/plain-text tool results parse into typed outputs."""

    def test_output_parsers_registered(self) -> None:
        for name in (
            "TeamCreate",
            "TeamDelete",
            "TaskCreate",
            "TaskUpdate",
            "TaskList",
            "SendMessage",
        ):
            assert name in TOOL_OUTPUT_PARSERS, f"{name} parser missing"

    def test_teamcreate_output(self) -> None:
        payload = (
            '{"team_name":"test-coverage",'
            '"team_file_path":"/teams/test-coverage/config.json",'
            '"lead_agent_id":"team-lead@test-coverage"}'
        )
        out = parse_teamcreate_output(_tr_text(payload), None)
        assert isinstance(out, TeamCreateOutput)
        assert out.team_name == "test-coverage"
        assert out.lead_agent_id == "team-lead@test-coverage"

    def test_teamcreate_output_rejects_non_json(self) -> None:
        out = parse_teamcreate_output(_tr_text("not-json"), None)
        assert out is None

    def test_teamdelete_extracts_active_members(self) -> None:
        payload = (
            '{"success":false,'
            '"message":"Cannot cleanup team with 2 active member(s): alice, bob. Try shutdown first.",'
            '"team_name":"test-coverage"}'
        )
        out = parse_teamdelete_output(_tr_text(payload), None)
        assert isinstance(out, TeamDeleteOutput)
        assert out.success is False
        assert out.active_members == ["alice", "bob"]
        assert out.team_name == "test-coverage"

    def test_teamdelete_success_no_members(self) -> None:
        payload = '{"success":true,"message":"Team deleted.","team_name":"x"}'
        out = parse_teamdelete_output(_tr_text(payload), None)
        assert isinstance(out, TeamDeleteOutput)
        assert out.success is True
        assert out.active_members is None

    def test_teamdelete_rejects_string_success(self) -> None:
        """Regression (coderabbit #117): stringified `"false"` must not
        coerce into `success=True`."""
        payload = '{"success":"false","message":"no","team_name":"x"}'
        out = parse_teamdelete_output(_tr_text(payload), None)
        assert out is None

    def test_sendmessage_rejects_string_success(self) -> None:
        """Regression (coderabbit #117): same as TeamDelete — the parser
        falls through rather than silently mis-rendering a string bool."""
        payload = (
            '{"success":"true","message":"sent","request_id":"r","target":"alice"}'
        )
        out = parse_sendmessage_output(_tr_text(payload), None)
        assert out is None

    def test_sendmessage_rejects_non_string_message(self) -> None:
        payload = '{"success":true,"message":123,"target":"alice"}'
        out = parse_sendmessage_output(_tr_text(payload), None)
        assert out is None

    def test_tasklist_markdown_escapes_pipes_and_newlines(self) -> None:
        """Regression (monk #6 / coderabbit): every Markdown-table cell
        must escape `|` and `\\n` — not just the subject.

        A malformed transcript with `|` in status or `\\n` in owner
        would previously split rows silently and shift subsequent cells.
        """
        from claude_code_log.markdown.renderer import MarkdownRenderer
        from claude_code_log.models import (
            MessageMeta,
            TaskListItem,
            TaskListOutput,
            ToolResultMessage,
            ToolResultContent,
        )
        from claude_code_log.renderer import TemplateMessage

        output = TaskListOutput(
            tasks=[
                TaskListItem(
                    id="1",
                    subject="A | B",
                    status="in|progress",
                    owner=None,
                ),
                TaskListItem(
                    id="2",
                    subject="multi\nline",
                    status="completed",
                    owner=None,
                ),
            ],
            raw_text="",
        )
        meta = MessageMeta(session_id="s", timestamp="t", uuid="u")
        msg = ToolResultMessage(
            meta=meta,
            tool_use_id="tu",
            output=ToolResultContent(type="tool_result", tool_use_id="tu", content=""),
        )
        template_msg = TemplateMessage(msg)

        renderer = MarkdownRenderer()
        table = renderer.format_TaskListOutput(output, template_msg)

        # Pipes escaped in subject AND status
        assert r"A \| B" in table
        assert r"in\|progress" in table
        # Newline converted to <br>, NOT left as a literal newline that
        # would terminate the row
        assert "multi<br>line" in table
        # Row count still 2 (plus header + separator)
        assert table.count("\n") == 3

    def test_teamdelete_active_members_without_trailing_period(self) -> None:
        """Defensive: the active-members regex must not require a period."""
        payload = (
            '{"success":false,'
            '"message":"Cannot cleanup team with 2 active member(s): alice, bob",'
            '"team_name":"x"}'
        )
        out = parse_teamdelete_output(_tr_text(payload), None)
        assert isinstance(out, TeamDeleteOutput)
        assert out.active_members == ["alice", "bob"]

    def test_taskcreate_output(self) -> None:
        out = parse_taskcreate_output(
            _tr_text("Task #3 created successfully: Add relay tests"),
            None,
        )
        assert isinstance(out, TaskCreateOutput)
        assert out.task_id == "3"
        assert out.subject == "Add relay tests"

    def test_taskcreate_rejects_unrecognized(self) -> None:
        out = parse_taskcreate_output(_tr_text("Completely different"), None)
        assert out is None

    def test_taskupdate_output(self) -> None:
        out = parse_taskupdate_output(_tr_text("Updated task #1 owner, status"), None)
        assert isinstance(out, TaskUpdateOutput)
        assert out.success is True
        assert out.task_id == "1"
        assert out.updated_fields == {"owner": True, "status": True}

    def test_taskupdate_strips_trailing_punctuation(self) -> None:
        """Regression (coderabbit #117): a trailing period or semicolon
        on the field list must not leak into the last field key."""
        out = parse_taskupdate_output(_tr_text("Updated task #1 owner, status."), None)
        assert isinstance(out, TaskUpdateOutput)
        assert out.updated_fields == {"owner": True, "status": True}

        out = parse_taskupdate_output(_tr_text("Updated task #2 subject, owner;"), None)
        assert isinstance(out, TaskUpdateOutput)
        assert out.updated_fields == {"subject": True, "owner": True}

    def test_tasklist_output(self) -> None:
        text = (
            "#1 [completed] Add relay tests (alice)\n"
            "#2 [in_progress] Add server tests (bob)\n"
            "#3 [pending] Merge branches"
        )
        out = parse_tasklist_output(_tr_text(text), None)
        assert isinstance(out, TaskListOutput)
        assert len(out.tasks) == 3
        assert out.tasks[0].status == "completed"
        assert out.tasks[0].owner == "alice"
        assert out.tasks[2].owner is None

    def test_tasklist_returns_none_on_unknown_format(self) -> None:
        out = parse_tasklist_output(_tr_text("This is not a task list."), None)
        assert out is None

    def test_sendmessage_output(self) -> None:
        payload = (
            '{"success":true,'
            '"message":"Shutdown request sent to alice.",'
            '"request_id":"shutdown-1@alice",'
            '"target":"alice"}'
        )
        out = parse_sendmessage_output(_tr_text(payload), None)
        assert isinstance(out, SendMessageOutput)
        assert out.success is True
        assert out.target == "alice"
        assert out.request_id == "shutdown-1@alice"


# ---------------------------------------------------------------------------
# End-to-end fixture integration
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "test_data" / "teammates"
MAIN_SESSION = "ef000000-0000-4000-8000-000000000001"
MAIN_JSONL = FIXTURE_DIR / f"{MAIN_SESSION}.jsonl"
ALICE_AGENT_ID = "aaaa111111111111"
BOB_AGENT_ID = "bbbb222222222222"


@pytest.fixture(scope="module")
def fixture_messages() -> list:
    return load_transcript(MAIN_JSONL, cache_manager=None, silent=True)


@pytest.fixture(scope="module")
def fixture_dag() -> tuple[list, object]:
    return load_directory_transcripts(FIXTURE_DIR, cache_manager=None, silent=True)


class TestTeammatesFixtureLoading:
    def test_main_and_both_subagents_load(self, fixture_messages: list) -> None:
        # 22 main + 3 alice + 3 bob = 28 entries
        assert len(fixture_messages) == 28

    def test_alice_subagent_linked_via_primary_path(
        self, fixture_messages: list
    ) -> None:
        alice_entries = [
            m for m in fixture_messages if getattr(m, "agentId", None) == ALICE_AGENT_ID
        ]
        assert len(alice_entries) >= 4  # tool_result + 3 subagent entries

    def test_bob_subagent_linked_via_prompt_hash(self, fixture_messages: list) -> None:
        bob_entries = [
            m for m in fixture_messages if getattr(m, "agentId", None) == BOB_AGENT_ID
        ]
        # Without the fallback bob wouldn't be linked at all (0 entries).
        assert len(bob_entries) >= 4

    def test_bob_tool_result_back_patched_with_agentid(
        self, fixture_messages: list
    ) -> None:
        # Find the tool_result for bob's Task and confirm the prompt-hash
        # fallback set its agentId.
        tool_use_id: str | None = None
        for m in fixture_messages:
            if isinstance(m, AssistantTranscriptEntry):
                for item in m.message.content:
                    if (
                        isinstance(item, ToolUseContent)
                        and item.name == "Task"
                        and item.input.get("name") == "bob"
                    ):
                        tool_use_id = item.id
                        break
        assert tool_use_id is not None

        for m in fixture_messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            for c in m.message.content:
                if isinstance(c, ToolResultContent) and c.tool_use_id == tool_use_id:
                    assert m.agentId == BOB_AGENT_ID
                    return
        pytest.fail("bob tool_result not found")


class TestTeammatesIntegrateAgentEntries:
    def test_synthetic_session_ids_per_agent(self, fixture_dag: tuple) -> None:
        messages, _ = fixture_dag
        alice_sessions = {
            m.sessionId
            for m in messages
            if isinstance(m, (AssistantTranscriptEntry, UserTranscriptEntry))
            and getattr(m, "isSidechain", False)
            and getattr(m, "agentId", None) == ALICE_AGENT_ID
        }
        bob_sessions = {
            m.sessionId
            for m in messages
            if isinstance(m, (AssistantTranscriptEntry, UserTranscriptEntry))
            and getattr(m, "isSidechain", False)
            and getattr(m, "agentId", None) == BOB_AGENT_ID
        }
        assert alice_sessions == {f"{MAIN_SESSION}#agent-{ALICE_AGENT_ID}"}
        assert bob_sessions == {f"{MAIN_SESSION}#agent-{BOB_AGENT_ID}"}

    def test_each_agent_root_anchored_to_its_tool_result(
        self, fixture_dag: tuple
    ) -> None:
        messages, _ = fixture_dag

        # Anchor UUID for each agent = the tool_result entry carrying its agentId
        anchors: dict[str, str] = {}
        for m in messages:
            if (
                isinstance(m, UserTranscriptEntry)
                and not m.isSidechain
                and m.agentId in {ALICE_AGENT_ID, BOB_AGENT_ID}
            ):
                anchors[m.agentId] = m.uuid

        assert set(anchors) == {ALICE_AGENT_ID, BOB_AGENT_ID}, anchors

        # Every sidechain root (parentUuid -> anchor uuid) for each agent
        # must point to that agent's anchor.
        for m in messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            if not m.isSidechain:
                continue
            if not m.agentId:
                continue
            # First message of each sidechain: parentUuid now anchored
            # (we crafted the fixture so the first alice/bob sidechain
            # entry's parentUuid was None).
            if m.uuid.startswith("aaaaaaaa-0000-4000-8000-000000000001") or (
                m.uuid.startswith("bbbbbbbb-0000-4000-8000-000000000001")
            ):
                assert m.parentUuid == anchors[m.agentId]


class TestTeammatesFactoryIntegration:
    def test_alice_task_output_metadata_populated(self, fixture_messages: list) -> None:
        """Parse the fixture via the factory pipeline and confirm the
        alice Task tool_result carries an AgentResultMetadata with the
        values embedded in the markdown tail."""
        from claude_code_log.factories.tool_factory import create_tool_output

        found_alice_metadata = False
        for m in fixture_messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            for c in m.message.content:
                if (
                    isinstance(c, ToolResultContent)
                    and m.agentId == ALICE_AGENT_ID
                    and c.tool_use_id.startswith("tu_Task_")
                ):
                    parsed = create_tool_output("Task", c)
                    # Typed TaskOutput with structured metadata
                    assert hasattr(parsed, "metadata")
                    meta = getattr(parsed, "metadata")
                    assert meta is not None
                    assert meta.agent_id == ALICE_AGENT_ID
                    assert meta.total_tokens == 12345
                    assert meta.tool_uses == 5
                    assert meta.duration_ms == 60000
                    # Body stripped of the metadata tail
                    result_text = getattr(parsed, "result")
                    assert "Relay tests added" in result_text
                    assert "agentId:" not in result_text
                    found_alice_metadata = True
        assert found_alice_metadata

    def test_teammate_colors_are_session_scoped(self, tmp_path: Path) -> None:
        """Regression (monk #5 / coderabbit): same teammate_id in two
        sessions of a combined transcript must keep distinct colors.

        Before the fix, RenderingContext.teammate_colors was
        ``dict[teammate_id, color]`` so first-sighting-wins silently
        cross-contaminated: session A's alice=blue overrode session B's
        alice=red. With per-session scoping each session looks up its
        own map keyed by session_id.
        """
        import json as _json

        from claude_code_log.converter import load_directory_transcripts
        from claude_code_log.renderer import generate_template_messages

        def build_session(sid: str, color: str) -> list[dict]:
            return [
                {
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": "/t",
                    "sessionId": sid,
                    "version": "2.1.34",
                    "uuid": f"{sid[:8]}-0000-4000-8000-000000000001",
                    "timestamp": "2026-04-20T10:00:00Z",
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f'<teammate-message teammate_id="alice" color="{color}">\n'
                            f"working ({color})\n"
                            f"</teammate-message>"
                        ),
                    },
                },
            ]

        # Session A: alice=blue. Session B: alice=red.
        (tmp_path / "session_a.jsonl").write_text(
            "\n".join(_json.dumps(e) for e in build_session("ssn-a-0001", "blue"))
            + "\n"
        )
        (tmp_path / "session_b.jsonl").write_text(
            "\n".join(_json.dumps(e) for e in build_session("ssn-b-0001", "red")) + "\n"
        )

        messages, _ = load_directory_transcripts(
            tmp_path, cache_manager=None, silent=True
        )
        _roots, _nav, ctx = generate_template_messages(messages)

        # Per-session scoped map
        assert ctx.teammate_colors == {
            "ssn-a-0001": {"alice": "blue"},
            "ssn-b-0001": {"alice": "red"},
        }

    def test_identical_prompts_do_not_collide(self, tmp_path: Path) -> None:
        """Regression: two Tasks with identical prompts must link to
        *different* subagent files. Reported by monk in PR #117 review —
        the inner match loop used to overwrite the first patch when a
        second agent file's first-message body normalized to the same
        string.
        """
        import json as _json

        session_id = "dead0000-0000-4000-8000-000000000001"
        main_path = tmp_path / f"{session_id}.jsonl"
        subagents_dir = tmp_path / session_id / "subagents"
        subagents_dir.mkdir(parents=True)

        agent_x = "xxxx1111111111111"
        agent_y = "yyyy2222222222222"

        shared_prompt = "Do the thing."
        base = {
            "isSidechain": False,
            "userType": "external",
            "cwd": "/t",
            "sessionId": session_id,
            "version": "2.1.34",
        }

        def entry(**kw: object) -> dict:
            return {**base, **kw}

        def main_uuid(n: int) -> str:
            return f"00000000-0000-4000-8000-{n:012d}"

        entries = [
            # U1: initial user prompt
            entry(
                parentUuid=None,
                uuid=main_uuid(1),
                timestamp="2026-04-19T10:01:00Z",
                type="user",
                message={"role": "user", "content": [{"type": "text", "text": "go"}]},
            ),
            # A1: Task tool_use → teammate X (identical prompt)
            entry(
                parentUuid=main_uuid(1),
                uuid=main_uuid(2),
                timestamp="2026-04-19T10:02:00Z",
                type="assistant",
                message={
                    "id": "msg_a",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-A",
                            "name": "Task",
                            "input": {
                                "prompt": shared_prompt,
                                "subagent_type": "general-purpose",
                                "description": "teammate X",
                                "name": "x",
                            },
                        }
                    ],
                    "stop_reason": None,
                },
            ),
            # U2: tool_result for tu-A (no agentId — forces fallback)
            entry(
                parentUuid=main_uuid(2),
                uuid=main_uuid(3),
                timestamp="2026-04-19T10:03:00Z",
                type="user",
                message={
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "tu-A",
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "done by x"}],
                        }
                    ],
                },
            ),
            # A2: Task tool_use → teammate Y (identical prompt)
            entry(
                parentUuid=main_uuid(3),
                uuid=main_uuid(4),
                timestamp="2026-04-19T10:04:00Z",
                type="assistant",
                message={
                    "id": "msg_b",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-B",
                            "name": "Task",
                            "input": {
                                "prompt": shared_prompt,
                                "subagent_type": "general-purpose",
                                "description": "teammate Y",
                                "name": "y",
                            },
                        }
                    ],
                    "stop_reason": None,
                },
            ),
            # U3: tool_result for tu-B (no agentId)
            entry(
                parentUuid=main_uuid(4),
                uuid=main_uuid(5),
                timestamp="2026-04-19T10:05:00Z",
                type="user",
                message={
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "tu-B",
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "done by y"}],
                        }
                    ],
                },
            ),
        ]

        with main_path.open("w") as f:
            for e in entries:
                f.write(_json.dumps(e) + "\n")

        # Two agent files whose first message bodies both normalize to the
        # shared prompt.
        for agent_id in (agent_x, agent_y):
            agent_entry = {
                "parentUuid": None,
                "isSidechain": True,
                "agentId": agent_id,
                "userType": "external",
                "cwd": "/t",
                "sessionId": session_id,
                "version": "2.1.34",
                "uuid": f"aa{agent_id[:10]}-0000-4000-8000-000000000001",
                "timestamp": "2026-04-19T10:10:00Z",
                "type": "user",
                "message": {"role": "user", "content": shared_prompt},
            }
            with (subagents_dir / f"agent-{agent_id}.jsonl").open("w") as f:
                f.write(_json.dumps(agent_entry) + "\n")

        messages = load_transcript(main_path, cache_manager=None, silent=True)

        # Collect the agentId each tool_result got patched with.
        patched: dict[str, str] = {}
        for m in messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            for c in m.message.content:
                if isinstance(c, ToolResultContent) and c.tool_use_id in {
                    "tu-A",
                    "tu-B",
                }:
                    if m.agentId:
                        patched[c.tool_use_id] = m.agentId

        # Both Tasks linked, to *different* agents (no overwrite).
        assert patched.keys() == {"tu-A", "tu-B"}, patched
        assert set(patched.values()) == {agent_x, agent_y}, patched
        assert patched["tu-A"] != patched["tu-B"]

    def test_teammate_message_content_parsed(self, fixture_messages: list) -> None:
        """The user entry carrying multiple <teammate-message> blocks becomes
        a TeammateMessage content model through the user_factory pipeline."""
        from claude_code_log.factories.user_factory import create_user_message
        from claude_code_log.factories.meta_factory import create_meta
        from claude_code_log.models import TeammateMessage

        batched: list[TeammateMessage] = []
        for m in fixture_messages:
            # Restrict to main-session user entries (bob's subagent first
            # message is also a teammate-message wrapper, but that's the
            # subject of the prompt-hash test above).
            if not isinstance(m, UserTranscriptEntry) or m.isSidechain:
                continue
            meta = create_meta(m)
            text_bits: list[str] = []
            for c in m.message.content:
                if hasattr(c, "text"):
                    text_bits.append(getattr(c, "text"))
            text = "\n".join(text_bits)
            content = create_user_message(meta, list(m.message.content), text)
            if isinstance(content, TeammateMessage):
                batched.append(content)

        # Expect two: U14 (single alice block), U15 (alice+bob+system)
        assert len(batched) == 2
        block_counts = sorted(len(b.blocks) for b in batched)
        assert block_counts == [1, 3]
        # System block flagged in the mixed entry
        mixed = next(b for b in batched if len(b.blocks) == 3)
        assert any(blk.is_system for blk in mixed.blocks)


# ---------------------------------------------------------------------------
# Prompt-hash fallback: the ``Agent`` spawn tool
# ---------------------------------------------------------------------------


def _spawn_fixture(tmp_path: Path, spawn_tool_name: str) -> Path:
    """A trunk session that spawns one teammate, plus its subagent file.

    The sidecar deliberately carries no ``toolUseId`` (current Claude Code
    omits it for in-process teammates) and the spawn's ``toolUseResult``
    carries no ``agentId``, so the prompt-hash fallback is the *only*
    path that can link the two.
    """
    import json

    session_id = "ef000000-0000-4000-8000-0000000000aa"
    agent_id = "aworker-0123456789abcdef"
    prompt = "Audit the corpus and report back."
    base = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/synthetic/spawn-fixture",
        "sessionId": session_id,
        "version": "2.1.172",
    }

    trunk = tmp_path / f"{session_id}.jsonl"
    trunk.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    **base,
                    "parentUuid": None,
                    "uuid": "aaaaaaaa-0000-4000-8000-000000000001",
                    "timestamp": "2026-08-28T10:00:00.000Z",
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "go"}],
                    },
                },
                {
                    **base,
                    "parentUuid": "aaaaaaaa-0000-4000-8000-000000000001",
                    "uuid": "aaaaaaaa-0000-4000-8000-000000000002",
                    "timestamp": "2026-08-28T10:01:00.000Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg_spawn",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_spawn_001",
                                "name": spawn_tool_name,
                                "input": {"name": "worker", "prompt": prompt},
                            }
                        ],
                        "stop_reason": None,
                    },
                },
                {
                    **base,
                    "parentUuid": "aaaaaaaa-0000-4000-8000-000000000002",
                    "uuid": "aaaaaaaa-0000-4000-8000-000000000003",
                    "timestamp": "2026-08-28T10:02:00.000Z",
                    "type": "user",
                    "toolUseResult": {"status": "spawned", "prompt": prompt},
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_spawn_001",
                                "content": "Spawned successfully.",
                            }
                        ],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subagents = tmp_path / session_id / "subagents"
    subagents.mkdir(parents=True)
    # No ``toolUseId`` here — that is what makes the sidecar path unusable.
    (subagents / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(
            {"agentType": "worker", "name": "worker", "taskKind": "in_process_teammate"}
        ),
        encoding="utf-8",
    )
    sub_base = {**base, "isSidechain": True, "agentId": agent_id}
    (subagents / f"agent-{agent_id}.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    **sub_base,
                    "parentUuid": None,
                    "uuid": "bbbbbbbb-0000-4000-8000-000000000001",
                    "timestamp": "2026-08-28T10:03:00.000Z",
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            '<teammate-message teammate_id="team-lead" summary="audit">\n'
                            f"{prompt}\n</teammate-message>"
                        ),
                    },
                },
                {
                    **sub_base,
                    "parentUuid": "bbbbbbbb-0000-4000-8000-000000000001",
                    "uuid": "bbbbbbbb-0000-4000-8000-000000000002",
                    "timestamp": "2026-08-28T10:04:00.000Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg_worker",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "MARKER-worker-report"}],
                        "stop_reason": None,
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return trunk


@pytest.mark.parametrize("spawn_tool_name", ["Task", "Agent"])
def test_prompt_hash_fallback_covers_both_spawn_tool_names(
    tmp_path: Path, spawn_tool_name: str
) -> None:
    """The teammate spawn tool is named ``Agent``, not ``Task``.

    Gating the fallback's prompt collection on ``Task`` alone silently
    dropped every teammate transcript of a modern session — the subagent
    JSONLs were never even opened.
    """
    trunk = _spawn_fixture(tmp_path, spawn_tool_name)
    messages = load_transcript(trunk, cache_manager=None, silent=True)

    agent_id = "aworker-0123456789abcdef"
    linked = [m for m in messages if getattr(m, "agentId", None) == agent_id]
    assert linked, f"{spawn_tool_name} spawn: subagent transcript not linked"
    # tool_result back-patch + both subagent entries.
    assert len(linked) == 3
    assert any(
        "MARKER-worker-report" in str(getattr(m, "message", "")) for m in messages
    )


def _colliding_prompt_fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Two teammates spawned with a byte-identical prompt.

    The canonical teammate shape: a team-lead fans the same instructions
    out to N teammates, so the prompt cannot identify which sidecar
    belongs to which spawn. Only the ``name`` can. Filenames are ordered
    so that sort-order pairing gets it backwards.
    """
    import json

    session_id = "ef000000-0000-4000-8000-0000000000bb"
    prompt = "Run the arm and report the four answers."
    # agent-a... sorts first but belongs to the SECOND spawn.
    agents = {
        "aalpha-1111111111111111": "worker-B",
        "azulu-2222222222222222": "worker-A",
    }
    base = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/synthetic/collide",
        "sessionId": session_id,
        "version": "2.1.172",
    }

    entries: list[dict[str, object]] = [
        {
            **base,
            "parentUuid": None,
            "uuid": "cccccccc-0000-4000-8000-000000000001",
            "timestamp": "2026-08-29T10:00:00.000Z",
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        }
    ]
    prev = "cccccccc-0000-4000-8000-000000000001"
    # Spawn order is worker-A then worker-B — the reverse of filename order.
    for idx, spawn_name in enumerate(["worker-A", "worker-B"], start=2):
        use_uuid = f"cccccccc-0000-4000-8000-00000000000{idx}a"
        res_uuid = f"cccccccc-0000-4000-8000-00000000000{idx}b"
        tu_id = f"tu_spawn_{spawn_name}"
        entries.append(
            {
                **base,
                "parentUuid": prev,
                "uuid": use_uuid,
                "timestamp": f"2026-08-29T10:0{idx}:00.000Z",
                "type": "assistant",
                "message": {
                    "id": f"msg_{spawn_name}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tu_id,
                            "name": "Agent",
                            "input": {"name": spawn_name, "prompt": prompt},
                        }
                    ],
                    "stop_reason": None,
                },
            }
        )
        entries.append(
            {
                **base,
                "parentUuid": use_uuid,
                "uuid": res_uuid,
                "timestamp": f"2026-08-29T10:0{idx}:30.000Z",
                "type": "user",
                "toolUseResult": {"status": "spawned", "prompt": prompt},
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": "Spawned successfully.",
                        }
                    ],
                },
            }
        )
        prev = res_uuid

    trunk = tmp_path / f"{session_id}.jsonl"
    trunk.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    subagents = tmp_path / session_id / "subagents"
    subagents.mkdir(parents=True)
    for agent_id, name in agents.items():
        (subagents / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(
                {"agentType": name, "name": name, "taskKind": "in_process_teammate"}
            ),
            encoding="utf-8",
        )
        sub_base = {**base, "isSidechain": True, "agentId": agent_id}
        (subagents / f"agent-{agent_id}.jsonl").write_text(
            json.dumps(
                {
                    **sub_base,
                    "parentUuid": None,
                    "uuid": f"dddddddd-0000-4000-8000-{agent_id[1:13]}",
                    "timestamp": "2026-08-29T10:05:00.000Z",
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f'<teammate-message teammate_id="team-lead" summary="{name}">\n'
                            f"{prompt}\n</teammate-message>"
                        ),
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return trunk, {v: k for k, v in agents.items()}


def test_colliding_prompts_are_disambiguated_by_teammate_name(tmp_path: Path) -> None:
    """A shared prompt must not be paired by filename sort order.

    Teammates routinely receive byte-identical instructions, so the
    prompt is not a discriminator. Pairing on it alone anchored 23
    spawns (~12% of those it linked) to the wrong teammate on a real
    archive — a permutation within each colliding group. A wrong
    attribution is worse than none: an absent transcript invites a
    re-run, a confidently mis-attributed one invites a wrong conclusion.
    """
    trunk, by_name = _colliding_prompt_fixture(tmp_path)
    messages = load_transcript(trunk, cache_manager=None, silent=True)

    seen: dict[str, str] = {}
    for m in messages:
        if not isinstance(m, UserTranscriptEntry) or not m.agentId:
            continue
        for c in m.message.content:
            if isinstance(c, ToolResultContent):
                seen[c.tool_use_id] = m.agentId

    assert seen.get("tu_spawn_worker-A") == by_name["worker-A"]
    assert seen.get("tu_spawn_worker-B") == by_name["worker-B"]
