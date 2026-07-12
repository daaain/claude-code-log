"""Codex list_agents to TaskList result adaptation."""

from claude_code_log.factories.tool_factory import create_tool_input, create_tool_output
from claude_code_log.models import (
    TaskListInput,
    TaskListOutput,
    ToolResultContent,
)
from claude_code_log.providers.codex_tools import adapt_codex_tool_call


def test_list_agents_selects_tasklist_input_model() -> None:
    call = adapt_codex_tool_call("list_agents", {})
    assert isinstance(create_tool_input(call.name, call.input), TaskListInput)


def test_agent_rows_become_typed_task_list() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-list",
        content=(
            '{"agents":['
            '{"agent_name":"/root","agent_status":"running",'
            '"last_task_message":"Coordinate work"},'
            '{"agent_name":"/root/research","agent_status":'
            '{"completed":"opaque completion body"},"last_task_message":null}'
            "]}"
        ),
    )

    output = create_tool_output("TaskList", raw)

    assert isinstance(output, TaskListOutput)
    assert [(item.id, item.status, item.subject) for item in output.tasks] == [
        ("root", "running", "Coordinate work"),
        ("research", "completed", "research"),
    ]
    assert "opaque completion body" not in repr(output)


def test_malformed_agent_rows_keep_generic_result() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-list",
        content='{"agents":[{"agent_status":"running"}]}',
    )
    assert create_tool_output("TaskList", raw) is raw
