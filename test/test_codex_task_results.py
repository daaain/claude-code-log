"""Codex Task acknowledgement cleanup."""

from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.models import TaskOutput, ToolResultContent


def _result(content: str) -> ToolResultContent:
    return ToolResultContent(
        type="tool_result",
        tool_use_id="call-task",
        content=content,
    )


def test_redundant_task_name_only_acknowledgement_has_no_body() -> None:
    output = create_tool_output("Task", _result('{"task_name":"/root/research"}'))

    assert isinstance(output, TaskOutput)
    assert output.result == ""


def test_additional_task_acknowledgement_fields_remain_literal_json() -> None:
    output = create_tool_output(
        "Task",
        _result('{"task_name":"/root/research","status":"started"}'),
    )

    assert isinstance(output, TaskOutput)
    assert "task_name" not in output.result
    assert output.result == '```json\n{\n  "status": "started"\n}\n```'


def test_normal_task_markdown_is_unchanged() -> None:
    output = create_tool_output("Task", _result("## Research complete\n\nAll done."))

    assert isinstance(output, TaskOutput)
    assert output.result == "## Research complete\n\nAll done."
