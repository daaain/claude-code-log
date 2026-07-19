"""Parser + integration tests for the async-agents feature (issue #90).

Covers:

- ``has_task_notification`` detection.
- ``create_task_notification_message`` parsing (positive cases + edge cases).
- ``parse_taskoutput_output`` parsing of the polling tool result body.
- End-to-end fixture loading: Phase 3 fold (notification ``result_text`` ->
  spawning ``Task`` ``tool_result`` ``async_final_answer``), notification
  flagged as duplicate, sub-assistant duplicate dropped from the tree.
- Detail-level invariants: the fold is present at LOW (regression
  guard), and the notification body is the surviving copy at
  MINIMAL/USER_ONLY where the spawning Task tool_result is filtered
  out by the post-render pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.factories.task_notification_factory import (
    create_task_notification_message,
    has_task_notification,
)
from claude_code_log.factories.tool_factory import (
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_PARSERS,
    create_tool_input,
    parse_taskoutput_output,
)
from claude_code_log.models import (
    AssistantTextMessage,
    AssistantTranscriptEntry,
    RenderingDepth,
    MessageMeta,
    TaskNotificationMessage,
    TaskNotificationUsage,
    TaskOutput,
    TaskOutputInput,
    TaskOutputResult,
    TextContent,
    ToolResultContent,
    ToolResultMessage,
    ToolUseContent,
    UserTranscriptEntry,
)
from claude_code_log.renderer import generate_template_messages


def _assistant_text(content: AssistantTextMessage) -> str:
    return "\n".join(
        item.text for item in content.items if isinstance(item, TextContent)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta() -> MessageMeta:
    return MessageMeta(session_id="s", timestamp="t", uuid="u")


def _tool_result(text: str, tool_use_id: str = "tu") -> ToolResultContent:
    return ToolResultContent(
        type="tool_result",
        tool_use_id=tool_use_id,
        content=text,
    )


# ---------------------------------------------------------------------------
# has_task_notification
# ---------------------------------------------------------------------------


class TestHasTaskNotification:
    def test_plain_text_is_not_a_notification(self) -> None:
        assert has_task_notification("Just a regular message.") is False

    def test_unrelated_xml_is_not_a_notification(self) -> None:
        assert (
            has_task_notification("<teammate-message>...</teammate-message>") is False
        )

    def test_minimal_notification_is_detected(self) -> None:
        text = "<task-notification><task-id>x</task-id></task-notification>"
        assert has_task_notification(text) is True

    def test_open_tag_without_close_is_rejected(self) -> None:
        # Substring is present but the regex won't match without a closing tag.
        text = "<task-notification>broken"
        assert has_task_notification(text) is False


# ---------------------------------------------------------------------------
# create_task_notification_message
# ---------------------------------------------------------------------------


class TestCreateTaskNotificationMessage:
    def test_returns_none_for_non_notification(self) -> None:
        assert create_task_notification_message(_meta(), "no tags here") is None

    def test_parses_full_payload(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>a8b740b</task-id>\n"
            "<status>completed</status>\n"
            '<summary>Agent "Coverage analysis" completed</summary>\n'
            "<result>Coverage analysis complete.\n\nAll done.</result>\n"
            "<usage>total_tokens: 23099\n"
            "tool_uses: 2\n"
            "duration_ms: 15506</usage>\n"
            "</task-notification>\n"
            "Full transcript available at: /tmp/foo/tasks/a8b740b.output"
        )
        msg = create_task_notification_message(_meta(), text)
        assert msg is not None
        assert msg.task_id == "a8b740b"
        assert msg.status == "completed"
        assert msg.summary == 'Agent "Coverage analysis" completed'
        assert "Coverage analysis complete." in msg.result_text
        assert msg.transcript_path == "/tmp/foo/tasks/a8b740b.output"
        assert msg.usage is not None
        assert msg.usage.total_tokens == 23099
        assert msg.usage.tool_uses == 2
        assert msg.usage.duration_ms == 15506
        # raw_text preserved verbatim for fallback rendering
        assert msg.raw_text == text
        # Phase 3 dedup markers default to false / unset
        assert msg.result_is_duplicate is False
        assert msg.spawning_task_message_index is None

    def test_message_type_is_task_notification(self) -> None:
        text = "<task-notification><task-id>x</task-id></task-notification>"
        msg = create_task_notification_message(_meta(), text)
        assert msg is not None
        assert msg.message_type == "task_notification"

    def test_missing_usage_block_is_tolerated(self) -> None:
        text = (
            "<task-notification>\n"
            "<task-id>x</task-id>\n"
            "<status>failed</status>\n"
            "<summary>Agent crashed</summary>\n"
            "<result>Error: out of memory</result>\n"
            "</task-notification>"
        )
        msg = create_task_notification_message(_meta(), text)
        assert msg is not None
        assert msg.task_id == "x"
        assert msg.status == "failed"
        assert msg.usage is None
        assert msg.transcript_path is None

    def test_empty_payload_returns_none(self) -> None:
        # ``<task-notification></task-notification>`` with nothing
        # recognisable inside should not claim the body.
        assert (
            create_task_notification_message(
                _meta(), "<task-notification></task-notification>"
            )
            is None
        )

    def test_result_body_with_xmlish_text_does_not_clobber_metadata(self) -> None:
        """Regression (CodeRabbit on PR #132): the ``<result>`` body
        is agent-authored markdown and may contain literal XML-shaped
        snippets (for instance, an agent quoting a `<summary>` HTML
        tag verbatim). The header-field scan must run on the body
        *minus* ``<result>`` and ``<usage>`` so a ``<summary>`` (or
        ``<status>`` / ``<task-id>``) inside the result text can't
        overwrite the real notification metadata.
        """
        text = (
            "<task-notification>\n"
            "<task-id>real123</task-id>\n"
            "<status>completed</status>\n"
            "<summary>Real summary</summary>\n"
            "<result>The agent's body shows: "
            "<task-id>fake999</task-id> "
            "<status>failed</status> "
            "<summary>Bogus summary</summary> "
            "and continues here.</result>\n"
            "</task-notification>"
        )
        msg = create_task_notification_message(_meta(), text)
        assert msg is not None
        # Real header metadata wins; the inline copies inside <result>
        # don't bleed through.
        assert msg.task_id == "real123"
        assert msg.status == "completed"
        assert msg.summary == "Real summary"
        # The inline tags are preserved verbatim in result_text — they
        # were the agent's content, not metadata.
        assert "fake999" in msg.result_text
        assert "Bogus summary" in msg.result_text

    def test_partial_usage_block_keeps_known_fields(self) -> None:
        text = (
            "<task-notification>"
            "<task-id>x</task-id>"
            "<usage>total_tokens: 100</usage>"
            "</task-notification>"
        )
        msg = create_task_notification_message(_meta(), text)
        assert msg is not None
        assert msg.usage == TaskNotificationUsage(
            total_tokens=100, tool_uses=None, duration_ms=None
        )


# ---------------------------------------------------------------------------
# parse_taskoutput_output
# ---------------------------------------------------------------------------


class TestParseTaskOutputOutput:
    def test_parses_full_payload(self) -> None:
        text = (
            "<retrieval_status>success</retrieval_status>\n\n"
            "<task_id>a5de609</task_id>\n\n"
            "<task_type>local_agent</task_type>\n\n"
            "<status>completed</status>\n\n"
            "<output>\n"
            "[Truncated. Full output: /tmp/claude/tasks/a5de609.output]\n"
            "</output>"
        )
        out = parse_taskoutput_output(_tool_result(text), file_path=None)
        assert isinstance(out, TaskOutputResult)
        assert out.retrieval_status == "success"
        assert out.task_id == "a5de609"
        assert out.task_type == "local_agent"
        assert out.status == "completed"
        assert out.output_truncated is True
        assert out.output_file == "/tmp/claude/tasks/a5de609.output"

    def test_handles_in_progress_status(self) -> None:
        text = (
            "<retrieval_status>success</retrieval_status>\n"
            "<task_id>x</task_id>\n"
            "<task_type>local_agent</task_type>\n"
            "<status>in_progress</status>"
        )
        out = parse_taskoutput_output(_tool_result(text), file_path=None)
        assert out is not None
        assert out.status == "in_progress"
        # No <output> block — truncation flag stays False
        assert out.output_truncated is False
        assert out.output_file is None

    def test_returns_none_for_non_taskoutput_text(self) -> None:
        # No recognisable XML fields → not a TaskOutput shape
        assert (
            parse_taskoutput_output(_tool_result("Just text."), file_path=None) is None
        )

    def test_returns_none_for_empty_tool_result(self) -> None:
        # Empty content can't be a TaskOutput
        assert parse_taskoutput_output(_tool_result(""), file_path=None) is None

    def test_registered_in_input_and_output_dispatch(self) -> None:
        # Defensive: protects against accidental dispatch table churn.
        assert TOOL_INPUT_MODELS["TaskOutput"] is TaskOutputInput
        assert TOOL_OUTPUT_PARSERS["TaskOutput"] is parse_taskoutput_output

    def test_input_model_via_create_tool_input(self) -> None:
        parsed = create_tool_input(
            "TaskOutput",
            {"task_id": "abc123", "block": True, "timeout": 60000},
        )
        assert isinstance(parsed, TaskOutputInput)
        assert parsed.task_id == "abc123"
        assert parsed.block is True
        assert parsed.timeout == 60000


# ---------------------------------------------------------------------------
# End-to-end fixture integration
# ---------------------------------------------------------------------------


FIXTURE_DIR = Path(__file__).parent / "test_data" / "async_agents"
MAIN_SESSION = "eb000000-0000-4000-8000-000000000001"
MAIN_JSONL = FIXTURE_DIR / f"{MAIN_SESSION}.jsonl"
ASYNC_AGENT_ID = "cccc333"
ASYNC_TASK_TOOL_USE_ID = "tu_Task_async_001"
ASYNC_TASKOUTPUT_TOOL_USE_ID = "tu_TaskOutput_001"
FINAL_ANSWER_NEEDLE = "Coverage analysis complete."


@pytest.fixture(scope="module")
def fixture_messages() -> list:
    return load_transcript(MAIN_JSONL, cache_manager=None, silent=True)


class TestAsyncAgentsFixtureLoading:
    def test_main_and_subagent_load(self, fixture_messages: list) -> None:
        # 7 main + 3 sub = 10 entries
        assert len(fixture_messages) == 10

    def test_async_task_tool_use_recognised_as_async(
        self, fixture_messages: list
    ) -> None:
        for m in fixture_messages:
            if not isinstance(m, AssistantTranscriptEntry):
                continue
            for c in m.message.content:
                if (
                    isinstance(c, ToolUseContent)
                    and c.name == "Task"
                    and c.id == ASYNC_TASK_TOOL_USE_ID
                ):
                    assert c.input.get("run_in_background") is True
                    return
        pytest.fail("async Task tool_use not found in fixture")

    def test_subagent_entries_linked_via_agent_id(self, fixture_messages: list) -> None:
        sub_entries = [
            m for m in fixture_messages if getattr(m, "agentId", None) == ASYNC_AGENT_ID
        ]
        # 3 subagent entries + the trunk tool_result that the converter
        # back-patches with the agentId for sidechain anchoring.
        assert len(sub_entries) >= 3


class TestAsyncAgentsFactoryDispatch:
    """The two new parsers are reachable via the standard dispatch."""

    def test_async_task_tool_result_parses_metadata(
        self, fixture_messages: list
    ) -> None:
        from claude_code_log.factories.tool_factory import create_tool_output

        for m in fixture_messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            for c in m.message.content:
                if (
                    isinstance(c, ToolResultContent)
                    and c.tool_use_id == ASYNC_TASK_TOOL_USE_ID
                ):
                    parsed = create_tool_output("Task", c)
                    assert isinstance(parsed, TaskOutput)
                    # The shared agent-metadata factory picks up the
                    # ``agentId: cccc333 (...)`` line embedded in the
                    # async-launched body.
                    assert parsed.metadata is not None
                    assert parsed.metadata.agent_id == ASYNC_AGENT_ID
                    return
        pytest.fail("async Task tool_result not found in fixture")

    def test_taskoutput_tool_result_parses_via_dispatch(
        self, fixture_messages: list
    ) -> None:
        from claude_code_log.factories.tool_factory import create_tool_output

        for m in fixture_messages:
            if not isinstance(m, UserTranscriptEntry):
                continue
            for c in m.message.content:
                if (
                    isinstance(c, ToolResultContent)
                    and c.tool_use_id == ASYNC_TASKOUTPUT_TOOL_USE_ID
                ):
                    parsed = create_tool_output("TaskOutput", c)
                    assert isinstance(parsed, TaskOutputResult)
                    assert parsed.task_id == ASYNC_AGENT_ID
                    assert parsed.task_type == "local_agent"
                    assert parsed.status == "in_progress"
                    assert parsed.output_truncated is True
                    assert (
                        parsed.output_file
                        == f"/tmp/claude-1000/synthetic/tasks/{ASYNC_AGENT_ID}.output"
                    )
                    return
        pytest.fail("TaskOutput tool_result not found in fixture")


class TestAsyncAgentsRenderingPipeline:
    """Phase 3 fold + dedup pass behaves end-to-end on the fixture."""

    @pytest.fixture(scope="class")
    def render(self, request: pytest.FixtureRequest) -> tuple:
        messages = load_transcript(MAIN_JSONL, cache_manager=None, silent=True)
        roots, _nav, ctx = generate_template_messages(messages)
        return roots, ctx

    def test_notification_card_flagged_as_duplicate(self, render: tuple) -> None:
        _, ctx = render
        notifications = [
            tm.content
            for tm in ctx.messages
            if isinstance(tm.content, TaskNotificationMessage)
        ]
        assert len(notifications) == 1
        notif = notifications[0]
        assert notif.task_id == ASYNC_AGENT_ID
        assert notif.result_is_duplicate is True, (
            "notification result should be flagged as duplicate "
            "(matches last sub-assistant)"
        )
        # Backlink anchor wired to the spawning Task tool_use.
        assert notif.spawning_task_message_index is not None

    def test_async_final_answer_folded_into_spawn(self, render: tuple) -> None:
        _, ctx = render
        for tm in ctx.messages:
            content = tm.content
            if not isinstance(content, ToolResultMessage):
                continue
            if content.tool_name != "Task":
                continue
            output = content.output
            if not isinstance(output, TaskOutput):
                continue
            if output.metadata is None or output.metadata.agent_id != ASYNC_AGENT_ID:
                continue
            assert output.async_final_answer is not None, (
                "spawning Task tool_result should carry the folded final answer"
            )
            assert FINAL_ANSWER_NEEDLE in output.async_final_answer
            return
        pytest.fail("async Task tool_result not found in rendering context")

    def test_duplicate_sub_assistant_dropped_from_tree(self, render: tuple) -> None:
        roots, _ctx = render

        # Walk the rendered tree and count sub-assistants that carry the
        # final-answer text. Phase 3 should have removed the duplicate.
        def walk(msg) -> int:
            count = 0
            content = msg.content
            if isinstance(content, AssistantTextMessage) and msg.is_sidechain:
                if FINAL_ANSWER_NEEDLE in _assistant_text(content):
                    count += 1
            for child in msg.children:
                count += walk(child)
            return count

        sidechain_hits = sum(walk(r) for r in roots)
        assert sidechain_hits == 0, (
            "the last sub-assistant carrying the final answer should have "
            "been dropped after folding into the spawning Task tool_result"
        )

    def test_final_answer_appears_exactly_once_in_tree(self, render: tuple) -> None:
        """Across the entire tree, the final-answer text should appear in
        exactly one place: folded into the spawning Task tool_result.
        Both the duplicate sub-assistant and the notification body should
        be suppressed."""
        roots, _ctx = render

        def walk(msg) -> int:
            count = 0
            content = msg.content
            # Folded answer on the spawning Task tool_result.
            if isinstance(content, ToolResultMessage) and isinstance(
                content.output, TaskOutput
            ):
                if (
                    content.output.async_final_answer
                    and FINAL_ANSWER_NEEDLE in content.output.async_final_answer
                ):
                    count += 1
            # Free-standing sidechain assistant carrying the same text
            # (should be zero — Phase 3 removes it).
            if isinstance(content, AssistantTextMessage) and msg.is_sidechain:
                if FINAL_ANSWER_NEEDLE in _assistant_text(content):
                    count += 1
            # Notification result_text — even when present in the model,
            # the renderer collapses it to a backlink stub when
            # ``result_is_duplicate`` is set, so we don't count it here.
            for child in msg.children:
                count += walk(child)
            return count

        assert sum(walk(r) for r in roots) == 1


class TestAsyncAgentsRenderingDepths:
    """Detail-level invariants for the async-agent fold (issue #90).

    Plan A (mail #2620 → #2622): the spawn-fold sources from the
    notification's ``result_text`` (not from the sidechain assistant),
    so it survives the LOW depth level where ``_filter_by_depth``
    has stripped sidechain entries before the renderer ever runs.
    At MINIMAL / USER_ONLY the spawning Task tool_result itself is
    filtered out post-render — there's nothing to fold onto, so the
    notification card retains its body as the surviving copy.
    """

    @staticmethod
    def _spawning_task_output(ctx) -> TaskOutput | None:
        for tm in ctx.messages:
            if tm is None:
                continue
            content = tm.content
            if (
                isinstance(content, ToolResultMessage)
                and content.tool_name == "Task"
                and isinstance(content.output, TaskOutput)
                and content.output.metadata is not None
                and content.output.metadata.agent_id == ASYNC_AGENT_ID
            ):
                return content.output
        return None

    @staticmethod
    def _notification(ctx) -> TaskNotificationMessage | None:
        for tm in ctx.messages:
            if tm is None:
                continue
            if (
                isinstance(tm.content, TaskNotificationMessage)
                and tm.content.task_id == ASYNC_AGENT_ID
            ):
                return tm.content
        return None

    def _render_at(self, depth: RenderingDepth) -> tuple:
        messages = load_transcript(MAIN_JSONL, cache_manager=None, silent=True)
        return generate_template_messages(messages, depth=depth)

    @pytest.mark.parametrize(
        "depth",
        [RenderingDepth.HOOK, RenderingDepth.TOOL, RenderingDepth.AGENT],
    )
    def test_fold_present_when_spawn_target_kept(self, depth: RenderingDepth) -> None:
        """At FULL/HIGH/LOW the spawning Task tool_result survives the
        depth filters, so the notification's ``result_text`` is folded
        onto its ``async_final_answer``.

        Regression guard for the "fold lost at --detail low" report:
        before Plan A, the fold relied on the sidechain assistant
        being present; LOW strips sidechain entries, so the fold went
        missing despite the spawn surviving.
        """
        _roots, _nav, ctx = self._render_at(depth)

        spawn_output = self._spawning_task_output(ctx)
        assert spawn_output is not None, (
            f"spawning Task tool_result missing at depth={depth.value}"
        )
        assert spawn_output.async_final_answer is not None, (
            f"async_final_answer not folded at depth={depth.value}"
        )
        assert FINAL_ANSWER_NEEDLE in spawn_output.async_final_answer

    def test_notification_flagged_duplicate_at_full_and_high(self) -> None:
        """At FULL/HIGH the notification card stays in ctx, flagged
        ``result_is_duplicate`` and wired with a spawn backlink. The
        formatter still emits the full metadata card — keeping it for
        transcript fidelity is the documented FULL/HIGH behavior."""
        for depth in (RenderingDepth.HOOK, RenderingDepth.TOOL):
            _roots, _nav, ctx = self._render_at(depth)
            notif = self._notification(ctx)
            assert notif is not None, f"notification missing at {depth.value}"
            assert notif.result_is_duplicate is True
            assert notif.spawning_task_message_index is not None

    def test_duplicate_notification_ghosted_at_low(self) -> None:
        """At LOW, the duplicate-flagged notification is "ghosted" —
        it stays in ``ctx.messages`` (so `message_index`, ancestry
        classes, backlink fields, and session nav anchors all remain
        valid) but its format/title return ``""``, so the rendering
        loop's "skip empty messages" elision drops the card from
        the visible output.

        This avoids the index-remap cascade that deleting the
        message would have triggered (CodeRabbit review on PR #132).
        """
        from claude_code_log.html.renderer import HtmlRenderer
        from claude_code_log.markdown.renderer import MarkdownRenderer

        _roots, _nav, ctx = self._render_at(RenderingDepth.AGENT)
        notif = self._notification(ctx)
        assert notif is not None, (
            "notification should remain in ctx.messages even when ghosted"
        )
        assert notif.result_is_duplicate is True

        # Find the TemplateMessage wrapping the notification so we
        # can drive the formatters at LOW directly.
        tm_notif = next(
            tm
            for tm in ctx.messages
            if tm is not None
            and isinstance(tm.content, TaskNotificationMessage)
            and tm.content.task_id == ASYNC_AGENT_ID
        )

        for renderer in (HtmlRenderer(), MarkdownRenderer()):
            renderer.depth = RenderingDepth.AGENT
            assert renderer.format_TaskNotificationMessage(notif, tm_notif) == ""
            assert renderer.title_TaskNotificationMessage(notif, tm_notif) == ""

    @pytest.mark.parametrize(
        "depth",
        [RenderingDepth.ASSISTANT, RenderingDepth.USER],
    )
    def test_fold_skipped_when_spawn_target_filtered(
        self, depth: RenderingDepth
    ) -> None:
        """At MINIMAL/USER_ONLY the post-render filter drops every
        ``ToolResultMessage`` (only user/assistant text survives). The
        spawn fold is skipped so the notification body remains the
        only surviving copy of the agent's answer.
        """
        _roots, _nav, ctx = self._render_at(depth)

        # Spawning Task tool_result was filtered out post-render.
        spawn_output = self._spawning_task_output(ctx)
        assert spawn_output is None, (
            f"Task tool_result should be filtered at depth={depth.value}"
        )

        # Notification card still in ctx; body is NOT marked duplicate
        # so the result_text renders as the visible answer.
        notif = self._notification(ctx)
        assert notif is not None
        assert notif.result_is_duplicate is False, (
            f"notification body must remain visible at depth={depth.value}"
        )
        assert FINAL_ANSWER_NEEDLE in notif.result_text
