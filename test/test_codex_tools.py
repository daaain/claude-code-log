"""Codex-to-shared tool adapter contract."""

from claude_code_log.factories.tool_factory import create_tool_input
from claude_code_log.models import BashInput, SendMessageInput, TaskInput
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
