"""Codex-to-shared tool adapter contract."""

import pytest

from claude_code_log.factories.tool_factory import create_tool_input, create_tool_output
from claude_code_log.html.tool_formatters import format_tool_execution_output
from claude_code_log.models import (
    BashInput,
    DeleteInput,
    DeleteOutput,
    EditInput,
    SendMessageInput,
    TaskInput,
    ToolResultContent,
    ToolExecutionInput,
    ToolExecutionOutput,
    WriteInput,
)
from claude_code_log.providers.codex_tools import (
    adapt_codex_tool_batch,
    adapt_codex_tool_call,
)
from claude_code_log.providers.codex import CodexProvider


def test_exec_command_reuses_bash_renderer() -> None:
    call = adapt_codex_tool_call(
        "exec",
        {"raw": "unused"},
        raw_input='const r = await tools.exec_command({cmd: "git status"}); text(r.output);',
    )
    assert call.name == "Bash"
    assert call.input == {"command": "git status"}


def test_apply_patch_exec_reuses_edit_renderer() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: example.py\n"
        "@@\n"
        "-old value\n"
        "+new value\n"
        "*** End Patch"
    )
    source = (
        f"const patch = {__import__('json').dumps(patch)};\n"
        "text(await tools.apply_patch(patch));"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Edit"
    assert call.input == {
        "file_path": "example.py",
        "old_string": "old value\n",
        "new_string": "new value\n",
    }
    assert isinstance(create_tool_input(call.name, call.input), EditInput)


def test_direct_single_add_patch_reuses_write_renderer() -> None:
    patch = "*** Begin Patch\n*** Add File: added.txt\n+hello\n*** End Patch"

    call = adapt_codex_tool_call("apply_patch", {"raw": patch}, raw_input=patch)

    assert call.name == "Write"
    assert call.input == {
        "file_path": "added.txt",
        "content": "hello\n",
    }
    assert isinstance(create_tool_input(call.name, call.input), WriteInput)


def test_delete_patch_without_body_reuses_delete_renderer() -> None:
    patch = "*** Begin Patch\n*** Delete File: obsolete.txt\n*** End Patch"
    source = (
        f"const patch = {__import__('json').dumps(patch)}; "
        "text(await tools.apply_patch(patch));"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Delete"
    assert call.input == {"file_path": "obsolete.txt"}
    assert isinstance(create_tool_input(call.name, call.input), DeleteInput)


def test_delete_result_keeps_first_transport_status_line() -> None:
    result = ToolResultContent(
        type="tool_result",
        tool_use_id="delete",
        content="Script completed\nWall time: 0.6 seconds\nOutput:\n",
    )

    output = create_tool_output("Delete", result, "/tmp/obsolete.txt")

    assert output == DeleteOutput(
        file_path="/tmp/obsolete.txt",
        success=True,
        message="Script completed\nWall time: 0.6 seconds",
    )


def test_multi_file_patch_reuses_multiedit_renderer() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: one.txt\n+one\n"
        "*** Add File: two.txt\n+two\n"
        "*** End Patch"
    )
    source = (
        f"const patch = {__import__('json').dumps(patch)}; "
        "text(await tools.apply_patch(patch));"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "MultiEdit"
    assert call.input == {
        "file_path": "2 files",
        "edits": [
            {"file_path": "one.txt", "old_string": "", "new_string": "one\n"},
            {"file_path": "two.txt", "old_string": "", "new_string": "two\n"},
        ],
    }


def test_mixed_patch_batch_splits_file_operations_without_reordering() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: first.txt\n@@\n-old\n+new\n"
        "*** Delete File: obsolete.txt\n"
        "*** Add File: created.txt\n+created\n"
        "*** Update File: last.txt\n@@\n-before\n+after\n"
        "*** End Patch"
    )
    source = (
        f"const patch = {__import__('json').dumps(patch)};\n"
        "text(await tools.apply_patch(patch));"
    )

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.result_indexes == [0, 0, 0, 0]
    assert [call.name for call in batch.calls] == [
        "Edit",
        "Delete",
        "Write",
        "Edit",
    ]
    assert [call.input["file_path"] for call in batch.calls] == [
        "first.txt",
        "obsolete.txt",
        "created.txt",
        "last.txt",
    ]
    assert batch.calls[0].input == {
        "file_path": "first.txt",
        "old_string": "old\n",
        "new_string": "new\n",
    }
    assert batch.calls[1].input == {"file_path": "obsolete.txt"}
    assert batch.calls[2].input["content"] == "created\n"


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


def test_codex_openai_docs_mcp_uses_internal_doc_tool() -> None:
    source = (
        "const r = await tools.mcp__openaiDeveloperDocs__fetch_openai_doc({"
        'url: "https://learn.chatgpt.com/docs/hooks", anchor: "#config-shape"});'
        "text(JSON.stringify(r));"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "CodexDoc"
    assert call.input == {
        "url": "https://learn.chatgpt.com/docs/hooks",
        "anchor": "#config-shape",
    }


def test_codex_openai_docs_object_batch_preserves_result_projection() -> None:
    source = """
        const [hooks, plugins, marketplace] = await Promise.all([
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "https://learn.chatgpt.com/docs/hooks"}),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "https://learn.chatgpt.com/docs/build-plugins"}),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "https://learn.chatgpt.com/docs/build-plugins", anchor: "#marketplace"})
        ]);
        text(JSON.stringify({hooks, plugins, marketplace}));
    """

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert [call.name for call in batch.calls] == ["CodexDoc"] * 3
    assert batch.result_indexes == [0, 0, 0]
    assert batch.result_object_keys == ("hooks", "plugins", "marketplace")


def test_codex_openai_docs_search_batch_preserves_aliased_results() -> None:
    source = """
        const [a, m] = await Promise.all([
          tools.mcp__openaiDeveloperDocs__search_openai_docs({query: "Codex approval policy", limit: 5}),
          tools.mcp__openaiDeveloperDocs__search_openai_docs({query: "Codex MCP streamable HTTP", limit: 5})
        ]);
        text(JSON.stringify({approval: a, mcp: m}));
    """

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert [call.name for call in batch.calls] == ["CodexDocSearch"] * 2
    assert [call.input["query"] for call in batch.calls] == [
        "Codex approval policy",
        "Codex MCP streamable HTTP",
    ]
    assert batch.result_indexes == [0, 0]
    assert batch.result_object_keys == ("approval", "mcp")


def test_single_mcp_call_resolves_constant_shorthand_input() -> None:
    source = """
        const actor = "/workspace/codex";
        const to = "/workspace/clmail/alice";
        const r = await tools.mcp__clmail__communicate({
          action: "send", actor, params: {to, subject: "Ready"}
        });
        text(r);
    """

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "mcp__clmail__communicate"
    assert call.input == {
        "action": "send",
        "actor": "/workspace/codex",
        "params": {"to": "/workspace/clmail/alice", "subject": "Ready"},
    }


def test_multi_call_exec_remains_visible_as_workflow() -> None:
    source = (
        'const a = await tools.exec_command({cmd: "one"}); '
        'const b = await tools.exec_command({cmd: "two"}); text(a); text(b);'
    )
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "ToolExecution"
    assert call.input == {"script": source}


def test_static_promise_all_batch_recovers_heterogeneous_tools() -> None:
    source = (
        "const results = await Promise.all([\n"
        ' tools.exec_command({cmd: "pytest -q"}),\n'
        ' tools.web__run({search_query: [{q: "synthetic"}]})\n'
        "]); results.forEach((r,i)=>{text(`RESULT_${i+1}`);text(r.output)});"
    )

    calls = adapt_codex_tool_batch(source)

    assert calls is not None
    assert calls.output_mode == "markers"
    assert calls.result_indexes == [0, 1]
    assert [(call.name, call.input) for call in calls.calls] == [
        ("Bash", {"command": "pytest -q"}),
        ("WebSearch", {"query": "synthetic"}),
    ]


def test_promise_batch_with_uncorrelated_emission_is_rejected() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), tools.exec_command({cmd: "two"})]);'
        "text(results[0].output);"
    )

    assert adapt_codex_tool_batch(source) is None


def test_promise_for_of_batch_uses_ordered_outputs() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), tools.exec_command({cmd: "two"})]);'
        "for (const r of results) text(r.output);"
    )

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.output_mode == "ordered"
    assert [call.input["command"] for call in batch.calls] == ["one", "two"]


def test_sequential_calls_use_ordered_outputs() -> None:
    source = (
        'const first = await tools.exec_command({cmd: "one"}); text(first.output);'
        'const second = await tools.exec_command({cmd: "two"}); text(second.output);'
    )

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.output_mode == "ordered"
    assert batch.result_indexes == [0, 1]
    assert [call.input["command"] for call in batch.calls] == ["one", "two"]


def test_sequential_calls_can_emit_after_all_invocations() -> None:
    source = (
        "const a = await tools.exec_command({"
        'cmd:"codex plugin list --json",'
        'workdir:"/workspace/project",'
        'sandbox_permissions:"require_escalated",'
        'justification:"Allow reading the synthetic plugin registry?",'
        'prefix_rule:["codex","plugin","list"],'
        "yield_time_ms:30000,max_output_tokens:5000});\n"
        "const b = await tools.exec_command({"
        'cmd:"find /home/user/.codex/plugins/cache/example-plugin/1.2.3 '
        '-maxdepth 4 -type f -print",'
        'workdir:"/workspace/project",'
        "yield_time_ms:10000,max_output_tokens:3000});\n"
        "text(a.output); text(b.output);"
    )

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.result_indexes == [0, 1]
    assert [call.input["command"] for call in batch.calls] == [
        "codex plugin list --json",
        "find /home/user/.codex/plugins/cache/example-plugin/1.2.3 "
        "-maxdepth 4 -type f -print",
    ]


def test_sequential_calls_correlate_outputs_by_result_variable() -> None:
    source = (
        'const a = await tools.exec_command({cmd: "one"});'
        'const b = await tools.exec_command({cmd: "two"});'
        "text(b.output); text(a.output);"
    )

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.result_indexes == [1, 0]


def test_static_for_of_expands_distinct_tool_calls() -> None:
    source = """
        for (const id of [4745, 4746, 4756]) {
          const r = await tools.mcp__clmail__communicate({
            action: "read",
            actor: "/workspace/codex",
            params: {id}
          });
          text(JSON.stringify(r));
        }
    """

    batch = adapt_codex_tool_batch(source)

    assert batch is not None
    assert batch.output_mode == "ordered"
    assert batch.result_indexes == [0, 1, 2]
    assert [call.name for call in batch.calls] == [
        "mcp__clmail__communicate",
        "mcp__clmail__communicate",
        "mcp__clmail__communicate",
    ]
    assert [call.input["params"] for call in batch.calls] == [
        {"id": 4745},
        {"id": 4746},
        {"id": 4756},
    ]


def test_all_tools_plus_one_command_is_compound_workflow() -> None:
    source = (
        "const matches = ALL_TOOLS.filter(x => x.name.includes('git')); "
        'const git = await tools.exec_command({cmd: "git status"}); '
        "text(JSON.stringify(matches, null, 2)); text(git.output);"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "ToolExecution"
    assert call.input == {"script": source}


def test_one_tool_with_multiple_result_emissions_reuses_renderer() -> None:
    source = (
        'const result = await tools.exec_command({cmd: "git status"}); '
        "text(result.output); text(`exit_code=${result.exit_code}`);"
    )

    assert (
        adapt_codex_tool_call("exec", {"raw": source}, raw_input=source).name == "Bash"
    )


def test_one_tool_with_unrelated_output_emission_is_workflow() -> None:
    source = (
        'const result = await tools.exec_command({cmd: "git status"}); '
        'text("prefix"); text(result.output);'
    )

    assert adapt_codex_tool_call("exec", {"raw": source}, raw_input=source).name == (
        "ToolExecution"
    )


def test_dynamic_exec_falls_back_to_workflow() -> None:
    source = (
        "const args = getArgs(); const r = await tools.exec_command(args); text(r);"
    )
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "ToolExecution"


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
    assert isinstance(create_tool_input(message.name, message.input), SendMessageInput)


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


def test_fernet_shaped_agent_message_payload_is_not_rendered() -> None:
    token = "gAAAAA" + "A" * 100

    message = adapt_codex_tool_call(
        "send_message", {"target": "/root/research", "message": token}
    )
    followup = adapt_codex_tool_call(
        "followup_task", {"target": "/root/research", "message": token}
    )

    assert message.name == followup.name == "SendMessage"
    assert message.input["content"] == ""
    assert followup.input["content"] == ""


def test_ordinary_agent_message_is_preserved() -> None:
    message = "Report the synthetic findings. " * 20
    call = adapt_codex_tool_call(
        "send_message", {"target": "/root/research", "message": message}
    )

    assert call.input["content"] == message


@pytest.mark.parametrize(
    ("name", "input_data"),
    [
        ("spawn_agent", {"task_name": 7}),
        ("send_message", {"target": 7}),
        ("followup_task", {"target": None}),
    ],
)
def test_malformed_collaboration_calls_scrub_opaque_messages(
    name: str, input_data: dict[str, object]
) -> None:
    token = "gAAAAA" + "A" * 100
    call = adapt_codex_tool_call(name, {**input_data, "message": token})

    assert token not in repr(call.input)
    assert call.input["message"] == "[opaque payload redacted]"


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


def test_find_only_web_run_reuses_webfetch_renderer() -> None:
    find = adapt_codex_tool_call(
        "web__run",
        {
            "find": [
                {"ref_id": "turn12view0", "pattern": "collabToolCall"},
                {"ref_id": "turn12view0", "pattern": "parentThreadId"},
            ],
            "response_length": "long",
        },
    )

    assert find.name == "WebFetch"
    assert find.input == {
        "url": "turn12view0",
        "prompt": "Find: collabToolCall • parentThreadId",
    }


def test_list_agents_reuses_task_list_renderer() -> None:
    call = adapt_codex_tool_call("list_agents", {"path_prefix": "/root"})

    assert call.name == "TaskList"
    assert call.input == {"path_prefix": "/root"}


def test_unknown_tool_stays_generic() -> None:
    call = adapt_codex_tool_call("future_tool", {"value": 1})
    assert call.name == "future_tool"
    assert call.input == {"value": 1}


def test_tool_like_text_inside_string_is_not_unwrapped() -> None:
    source = 'text("example: tools.exec_command({cmd: \\"unsafe\\"})");'
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == "ToolExecution"


def test_opaque_exec_uses_typed_tool_execution_pair() -> None:
    source = 'text("opaque");'
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "ToolExecution"
    assert create_tool_input(call.name, call.input) == ToolExecutionInput(script=source)

    result = ToolResultContent(
        type="tool_result",
        tool_use_id="opaque",
        content=[
            {
                "type": "input_text",
                "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
            },
            {"type": "input_text", "text": '{"ok": true}'},
            {"type": "future_result", "value": 42},
        ],
    )
    output = create_tool_output("ToolExecution", result)

    assert output == ToolExecutionOutput(
        status="Script completed\nWall time 0.2 seconds",
        items=[
            {"type": "input_text", "text": '{"ok": true}'},
            {"type": "future_result", "value": 42},
        ],
    )
    assert isinstance(output, ToolExecutionOutput)
    rendered = format_tool_execution_output(output)
    assert "Script completed<br>Wall time 0.2 seconds" in rendered
    assert "<table class='tool-execution-results'>" in rendered
    assert (
        "<th scope='row' class='tool-execution-result-label'>Result 1</th>" in rendered
    )
    assert (
        "<th scope='row' class='tool-execution-result-label'>Result 2</th>" in rendered
    )
    assert rendered.count("class='tool-execution-result-value'") == 2
    assert "tool-result-json" in rendered


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

    normalized, _ = CodexProvider()._adapt_tool_result(
        raw.content, tool_name="TodoWrite", is_error=False
    )
    adapted = raw.model_copy(update={"content": normalized})
    output = create_tool_output("TodoWrite", adapted)

    assert isinstance(output, ToolResultContent)
    assert output.content == "Todo list updated."


@pytest.mark.parametrize("tool_name", ["Write", "Delete", "Edit", "MultiEdit"])
def test_codex_file_success_result_keeps_exec_status(tool_name: str) -> None:
    content = [
        {
            "type": "input_text",
            "text": "Script completed\nWall time: 0.0 seconds\nOutput:\n",
        },
        {"type": "input_text", "text": "{}"},
    ]

    normalized, _ = CodexProvider()._adapt_tool_result(
        content, tool_name=tool_name, is_error=False
    )

    assert normalized == "Script completed\nWall time: 0.0 seconds\nOutput:\n"


@pytest.mark.parametrize("tool_name", ["Write", "Delete", "Edit", "MultiEdit"])
def test_codex_bare_empty_patch_result_is_not_rewritten(tool_name: str) -> None:
    normalized, _ = CodexProvider()._adapt_tool_result(
        "{}", tool_name=tool_name, is_error=False
    )

    assert normalized == "{}"


def test_unfamiliar_todo_result_stays_generic() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-plan",
        content=[{"type": "text", "text": "A future result shape"}],
    )
    assert create_tool_output("TodoWrite", raw) is raw


def test_codex_todo_error_transport_is_not_collapsed() -> None:
    content = [
        {
            "type": "input_text",
            "text": "Script completed\nWall time: 0.0 seconds\nOutput:\n",
        },
        {"type": "input_text", "text": "{}"},
    ]
    normalized, _ = CodexProvider()._adapt_tool_result(
        content, tool_name="TodoWrite", is_error=True
    )
    assert normalized == content


def test_codex_write_error_result_is_not_collapsed() -> None:
    normalized, _ = CodexProvider()._adapt_tool_result(
        "{}", tool_name="Write", is_error=True
    )

    assert normalized == "{}"
