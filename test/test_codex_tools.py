"""Codex-to-shared tool adapter contract."""

from claude_code_log.factories.tool_factory import create_tool_input, create_tool_output
from claude_code_log.models import (
    BashInput,
    SendMessageInput,
    TaskInput,
    ToolResultContent,
)
from claude_code_log.providers.codex_tools import adapt_codex_tool_call


def test_exec_command_reuses_bash_renderer() -> None:
    call = adapt_codex_tool_call(
        "exec",
        {"raw": "unused"},
        raw_input='const r = await tools.exec_command({cmd: "git status"}); text(r.output);',
    )
    assert call.name == "Bash"
    assert call.input == {"command": "git status"}


def test_single_mcp_call_keeps_name_for_plugin_transformers() -> None:
    call = adapt_codex_tool_call(
        "exec",
        {"raw": "unused"},
        raw_input=(
            'const r = await tools.mcp__clmail__communicate({action: "list", '
            'actor: "/workspace/synthetic", params: {status: "unread"}}); text(r);'
        ),
    )
    assert call.name == "mcp__clmail__communicate"
    assert call.input["action"] == "list"


def test_multi_call_exec_remains_visible_as_workflow() -> None:
    source = (
        'const a = await tools.exec_command({cmd: "one"}); '
        'const b = await tools.exec_command({cmd: "two"}); text(a); text(b);'
    )
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "Workflow"
    assert call.input == {"script": source}


def test_dynamic_exec_falls_back_to_workflow() -> None:
    source = "const args = getArgs(); const r = await tools.exec_command(args); text(r);"
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "Workflow"


def test_collaboration_calls_reuse_task_and_message_renderers() -> None:
    spawn = adapt_codex_tool_call(
        "spawn_agent", {"task_name": "research", "message": "Inspect it."}
    )
    message = adapt_codex_tool_call(
        "send_message", {"target": "research", "message": "Report back."}
    )
    assert spawn.name == "Task"
    assert spawn.input["prompt"] == "Inspect it."
    assert message.name == "SendMessage"
    assert message.input["recipient"] == "research"
    assert isinstance(create_tool_input(spawn.name, spawn.input), TaskInput)
    assert isinstance(
        create_tool_input(message.name, message.input), SendMessageInput
    )


def test_fernet_shaped_spawn_payload_is_not_rendered_as_task_prompt() -> None:
    token = "gAAAAA" + "A" * 100
    spawn = adapt_codex_tool_call(
        "spawn_agent", {"task_name": "research", "message": token}
    )

    assert spawn.name == "Task"
    assert spawn.input["prompt"] == ""


def test_ordinary_long_spawn_prompt_is_preserved() -> None:
    prompt = "Inspect the synthetic fixtures. " * 20
    spawn = adapt_codex_tool_call(
        "spawn_agent", {"task_name": "research", "message": prompt}
    )

    assert spawn.input["prompt"] == prompt


def test_plan_and_search_reuse_specialized_renderers() -> None:
    plan = adapt_codex_tool_call(
        "update_plan",
        {"plan": [{"step": "Inspect", "status": "in_progress"}]},
    )
    search = adapt_codex_tool_call(
        "web__run",
        {"search_query": [{"q": "synthetic query"}], "response_length": "short"},
    )
    assert plan.name == "TodoWrite"
    assert plan.input["todos"][0]["content"] == "Inspect"
    assert search.name == "WebSearch"
    assert search.input == {"query": "synthetic query"}


def test_list_agents_reuses_task_list_renderer() -> None:
    call = adapt_codex_tool_call("list_agents", {"path_prefix": "/root"})

    assert call.name == "TaskList"
    assert call.input == {"path_prefix": "/root"}


def test_unknown_tool_stays_generic() -> None:
    call = adapt_codex_tool_call("future_tool", {"value": 1})
    assert call.name == "future_tool"
    assert call.input == {"value": 1}


def test_tool_like_text_inside_string_is_not_unwrapped() -> None:
    source = 'text("example: tools.exec_command({cmd: \\\"unsafe\\\"})");'
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "Workflow"


def test_adapted_exec_selects_existing_bash_model() -> None:
    call = adapt_codex_tool_call("exec_command", {"cmd": "git status"})
    assert isinstance(create_tool_input(call.name, call.input), BashInput)


def test_codex_todo_success_result_hides_exec_transport() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-plan",
        content=[
            {
                "type": "input_text",
                "text": "Script completed\nWall time: 0.0 seconds\nOutput:\n",
            },
            {"type": "input_text", "text": "{}"},
        ],
    )

    output = create_tool_output("TodoWrite", raw)

    assert isinstance(output, ToolResultContent)
    assert output.content == "Todo list updated."


def test_unfamiliar_todo_result_stays_generic() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-plan",
        content=[{"type": "text", "text": "A future result shape"}],
    )
    assert create_tool_output("TodoWrite", raw) is raw
