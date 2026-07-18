"""Adversarial correlation and privacy cases for the Codex provider."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_code_log.models import (
    AssistantTranscriptEntry,
    ToolResultContent,
    ToolUseContent,
    UserTranscriptEntry,
)
from claude_code_log.providers.codex import (
    CodexProvider,
    CodexSessionIdentity,
    _DecodedRecord,
)
from claude_code_log.providers.codex_tools import adapt_codex_tool_call


THREAD_ID = "77777777-7777-4777-8777-777777777777"


def _record(line_no: int, kind: str, payload: dict[str, object]) -> _DecodedRecord:
    return _DecodedRecord(line_no, f"2026-07-14T00:00:{line_no:02d}Z", kind, payload)


def _message(line_no: int, role: str, text: str) -> _DecodedRecord:
    return _record(
        line_no,
        "response_item",
        {"type": "message", "role": role, "content": [{"type": "text", "text": text}]},
    )


def _event(line_no: int, role: str, text: str) -> _DecodedRecord:
    event_type = "user_message" if role == "user" else "agent_message"
    return _record(line_no, "event_msg", {"type": event_type, "message": text})


def _call(line_no: int, call_id: str, name: str, arguments: str) -> _DecodedRecord:
    return _record(
        line_no,
        "response_item",
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        },
    )


def _output(line_no: int, call_id: str, output: object) -> _DecodedRecord:
    return _record(
        line_no,
        "response_item",
        {"type": "function_call_output", "call_id": call_id, "output": output},
    )


def _command_envelope(**values: object) -> list[dict[str, str]]:
    return [{"type": "input_text", "text": json.dumps(values)}]


def _forwarded_result_envelope(
    payload: object, *, is_error: bool = False
) -> list[dict[str, str]]:
    return [
        {
            "type": "input_text",
            "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
        },
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "structuredContent": payload,
                    "isError": is_error,
                }
            ),
        },
    ]


def _normalized(
    records: list[_DecodedRecord],
) -> list[UserTranscriptEntry | AssistantTranscriptEntry]:
    provider = CodexProvider()
    identity = CodexSessionIdentity(thread_id=THREAD_ID, path=Path("synthetic.jsonl"))
    return list(provider._normalize_records(identity, records, None))


def _visible_text(records: list[_DecodedRecord]) -> list[str]:
    return [
        text
        for entry in _normalized(records)
        for item in entry.message.content
        if isinstance((text := getattr(item, "text", None)), str)
    ]


@pytest.mark.parametrize(
    "source",
    [
        (
            'const a = await tools.spawn_agent({task_name: "one", message: "TOKEN"}); '
            'const b = await tools.exec_command({cmd: "true"}); text(a); text(b);'
        ),
        ("const args = getArgs(); const a = await tools.spawn_agent(args); text(a);"),
        (
            'const a = await tools.spawn_agent({task_name: "one", message: "TOKEN"}); '
            'text("prefix"); text(a);'
        ),
        (
            'const a = await tools.spawn_agent({task_name: "one", message: "TOKEN",}); '
            "text(a); trailing malformed source"
        ),
    ],
)
def test_workflow_fallback_scrubs_opaque_agent_payload(source: str) -> None:
    token = "gAAAAA" + "A" * 100
    source = source.replace("TOKEN", token)

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Workflow"
    assert token not in call.input["script"]
    assert "spawn_agent" in call.input["script"]


def test_static_object_argument_is_adapted_and_scrubs_opaque_agent_payload() -> None:
    token = "gAAAAA" + "A" * 100
    source = (
        f'const args = {{task_name: "one", message: "{token}"}}; '
        "const result = await tools.spawn_agent(args); text(result);"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Task"
    assert call.input["prompt"] == ""
    assert token not in repr(call.input)


def test_workflow_fallback_preserves_ordinary_long_prompt() -> None:
    prompt = "Inspect the synthetic fixtures carefully. " * 20
    source = (
        f'const a = await tools.spawn_agent({{task_name: "one", message: "{prompt}"}}); '
        'text("prefix"); text(a);'
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Workflow"
    assert prompt in call.input["script"]


def test_visible_message_pairing_is_local_across_repeated_turns() -> None:
    records = [
        _event(1, "user", "Repeat"),
        _message(2, "user", "Repeat"),
        _event(3, "assistant", "First answer"),
        _message(4, "assistant", "First answer"),
        _message(5, "user", "Repeat"),
        _event(6, "assistant", "Interruption"),
        _event(7, "user", "Repeat"),
    ]

    assert _visible_text(records) == [
        "Repeat",
        "First answer",
        "Repeat",
        "Interruption",
        "Repeat",
    ]


@pytest.mark.parametrize(
    "records",
    [
        [_event(1, "user", "Adjacent"), _message(2, "user", "Adjacent")],
        [_message(1, "assistant", "Adjacent"), _event(2, "assistant", "Adjacent")],
    ],
)
def test_visible_message_pairing_handles_both_record_orders(
    records: list[_DecodedRecord],
) -> None:
    assert _visible_text(records) == ["Adjacent"]


def _image_message(line_no: int, text: str) -> _DecodedRecord:
    return _record(
        line_no,
        "response_item",
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": '<image name="screenshot.png">'},
                {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                {"type": "input_text", "text": "</image>"},
                {"type": "input_text", "text": text},
            ],
        },
    )


def _image_event(line_no: int, text: str) -> _DecodedRecord:
    return _record(
        line_no,
        "event_msg",
        {
            "type": "user_message",
            "message": text,
            "images": [],
            "local_images": ["/tmp/screenshot.png"],
        },
    )


@pytest.mark.parametrize("response_first", [True, False])
def test_image_message_pairing_keeps_richer_response_copy(
    response_first: bool,
) -> None:
    text = "Please inspect [Image #1]."
    response = _image_message(1, text)
    event = _image_event(2, text)
    records = [response, event] if response_first else [event, response]

    entries = _normalized(records)

    assert len(entries) == 1
    assert _visible_text(records) == [text]


def test_image_message_pairing_preserves_distinct_adjacent_prompt() -> None:
    records = [
        _image_message(1, "Inspect this image."),
        _image_event(2, "Inspect this other image."),
    ]

    assert len(_normalized(records)) == 2


def test_async_bash_does_not_fold_across_visible_activity() -> None:
    records = [
        _call(1, "exec", "exec_command", '{"cmd":"pytest"}'),
        _output(2, "exec", "Script running with cell ID cell-1"),
        _event(3, "assistant", "Still working."),
        _call(4, "wait", "wait", '{"cell_id":"cell-1"}'),
        _output(
            5,
            "wait",
            _command_envelope(output="passed\n", exit_code=0, wall_time_seconds=1.0),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    assert [item.id for item in uses] == ["exec", "wait"]
    assert [item.tool_use_id for item in results] == ["exec", "wait"]


def test_async_bash_retains_nonterminal_live_handle() -> None:
    records = [
        _call(1, "exec", "exec_command", '{"cmd":"pytest"}'),
        _output(2, "exec", "Script running with cell ID cell-1"),
        _call(3, "wait", "wait", '{"cell_id":"cell-1"}'),
        _output(
            4,
            "wait",
            _command_envelope(
                output="still running\n", session_id=42, wall_time_seconds=1.0
            ),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    assert [item.id for item in content if isinstance(item, ToolUseContent)] == [
        "exec",
        "wait",
    ]


def test_async_bash_fold_preserves_terminal_failure_status() -> None:
    records = [
        _call(1, "exec", "exec_command", '{"cmd":"pytest"}'),
        _output(2, "exec", "Script running with cell ID cell-1"),
        _call(3, "wait", "wait", '{"cell_id":"cell-1"}'),
        _output(
            4,
            "wait",
            _command_envelope(output="failed\n", exit_code=7, wall_time_seconds=1.0),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    assert len(results) == 1
    assert results[0].tool_use_id == "exec"
    assert results[0].content == "failed\n"
    assert results[0].is_error is True


@pytest.mark.parametrize("nested_tool", ["mcp__clmail__communicate", "future_tool"])
def test_direct_nested_tool_result_matches_native_tool_result_shape(
    nested_tool: str,
) -> None:
    payload = {"sent": True, "message_ids": [4730]}
    source = (
        f'const result = await tools.{nested_tool}({{action: "send"}}); '
        "text(JSON.stringify(result));"
    )
    records = [
        _call(1, "exec", "exec", source),
        _output(2, "exec", _forwarded_result_envelope(payload)),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == [nested_tool]
    assert len(results) == 1
    assert results[0].content == json.dumps(payload)
    assert results[0].is_error is False


def test_direct_nested_tool_result_propagates_mcp_error() -> None:
    source = (
        'const result = await tools.mcp__clmail__communicate({action: "send"}); '
        "text(JSON.stringify(result));"
    )
    records = [
        _call(1, "exec", "exec", source),
        _output(
            2,
            "exec",
            _forwarded_result_envelope({"error": "recipient missing"}, is_error=True),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    result = next(item for item in content if isinstance(item, ToolResultContent))
    assert result.content == json.dumps({"error": "recipient missing"})
    assert result.is_error is True


def test_workflow_result_retains_nested_tool_transport() -> None:
    payload = {"sent": True, "message_ids": [4730]}
    output = _forwarded_result_envelope(payload)
    source = (
        'const result = await tools.mcp__clmail__communicate({action: "send"}); '
        'text("prefix"); text(JSON.stringify(result));'
    )
    records = [_call(1, "exec", "exec", source), _output(2, "exec", output)]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    result = next(item for item in content if isinstance(item, ToolResultContent))
    assert [item.name for item in uses] == ["Workflow"]
    assert result.content == output


def test_inherited_prefix_requires_strong_parent_suffix_evidence() -> None:
    provider = CodexProvider()
    shared = _event(1, "assistant", "Done.")
    other = _event(2, "assistant", "Other")

    assert provider._contiguous_prefix_length([shared], [other, shared, other]) == 0
    assert (
        provider._contiguous_prefix_length([shared, other], [shared, other, shared])
        == 0
    )
    assert (
        provider._contiguous_prefix_length([shared, other], [other, shared, other]) == 2
    )


def test_assignment_and_emission_text_in_comments_or_strings_is_not_structural() -> (
    None
):
    source = (
        "// const result = await tools.exec_command(\n"
        'await tools.exec_command({cmd: "git status"}); text("result");'
    )

    assert adapt_codex_tool_call("exec", {"raw": source}, raw_input=source).name == (
        "Workflow"
    )


def test_object_key_rewriting_never_mutates_command_strings() -> None:
    source = (
        'const result = await tools.exec_command({cmd: "echo {foo: bar}"}); '
        "text(result.output);"
    )

    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)

    assert call.name == "Bash"
    assert call.input["command"] == "echo {foo: bar}"


def test_promise_all_batch_becomes_ordered_tool_result_pairs() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), '
        'tools.exec_command({cmd: "two"})]); '
        "results.forEach((r,i)=>{text(`RESULT_${i+1}`);text(r.output)});"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "RESULT_1"},
                {"type": "input_text", "text": "first output\n"},
                {"type": "input_text", "text": "RESULT_2"},
                {"type": "input_text", "text": "second output\n"},
            ],
        ),
    ]

    entries = _normalized(records)
    content = [item for entry in entries for item in entry.message.content]

    assert [item.name for item in content if isinstance(item, ToolUseContent)] == [
        "Bash",
        "Bash",
    ]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    assert [item.tool_use_id for item in results] == [
        "batch:batch:0",
        "batch:batch:1",
    ]
    assert [item.content for item in results] == ["first output\n", "second output\n"]


def test_destructured_mixed_promise_batch_preserves_call_and_result_order() -> None:
    source = """
        const [pr, auth, refs] = await Promise.all([
          tools.mcp__codex_apps__github_get_pr_info({
            repository_full_name: "daaain/claude-code-log", pr_number: 243
          }),
          tools.exec_command({cmd: "gh auth status"}),
          tools.exec_command({cmd: "git log --oneline"})
        ]);
        text(JSON.stringify(pr));
        text(auth.output);
        text(refs.output);
    """
    pr = {"number": 243, "title": "AGY provider"}
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                _forwarded_result_envelope(pr)[1],
                {"type": "input_text", "text": "github.com authenticated"},
                {"type": "input_text", "text": "ca6da78 latest commit"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == [
        "mcp__codex_apps__github_get_pr_info",
        "Bash",
        "Bash",
    ]
    assert [item.content for item in results] == [
        json.dumps(pr),
        "github.com authenticated",
        "ca6da78 latest commit",
    ]


def test_promise_batch_result_count_mismatch_stays_workflow() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), '
        'tools.exec_command({cmd: "two"})]); '
        "results.forEach((r,i)=>{text(`RESULT_${i+1}`);text(r.output)});"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "RESULT_1"},
                {"type": "input_text", "text": "only one output"},
            ],
        ),
    ]

    uses = [
        item
        for entry in _normalized(records)
        for item in entry.message.content
        if isinstance(item, ToolUseContent)
    ]

    assert [item.name for item in uses] == ["Workflow"]


def test_ordered_batch_becomes_tool_result_pairs() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), '
        'tools.exec_command({cmd: "two"})]); '
        "for (const r of results) text(r.output);"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "first output"},
                {"type": "input_text", "text": "second output"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]

    assert [item.name for item in content if isinstance(item, ToolUseContent)] == [
        "Bash",
        "Bash",
    ]
    assert [
        item.content for item in content if isinstance(item, ToolResultContent)
    ] == ["first output", "second output"]


def test_ordered_batch_result_count_mismatch_stays_workflow() -> None:
    source = (
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), '
        'tools.exec_command({cmd: "two"})]); '
        "for (const r of results) text(r.output);"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "only one output"},
            ],
        ),
    ]

    uses = [
        item
        for entry in _normalized(records)
        for item in entry.message.content
        if isinstance(item, ToolUseContent)
    ]

    assert [item.name for item in uses] == ["Workflow"]


def test_sequential_batch_pairs_outputs_by_result_variable() -> None:
    source = (
        'const a = await tools.exec_command({cmd: "one"});'
        'const b = await tools.exec_command({cmd: "two"});'
        "text(b.output); text(a.output);"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "two output"},
                {"type": "input_text", "text": "one output"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]

    assert [
        item.content for item in content if isinstance(item, ToolResultContent)
    ] == ["one output", "two output"]


def test_static_for_of_becomes_distinct_tool_result_pairs() -> None:
    source = """
        for (const id of [4745, 4746, 4756]) {
          const r = await tools.mcp__clmail__communicate({
            action: "read", actor: "/workspace/codex", params: {id}
          });
          text(JSON.stringify(r));
        }
    """
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "message 4745"},
                {"type": "input_text", "text": "message 4746"},
                {"type": "input_text", "text": "message 4756"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == [
        "mcp__clmail__communicate",
        "mcp__clmail__communicate",
        "mcp__clmail__communicate",
    ]
    assert [item.input["params"] for item in uses] == [
        {"id": 4745},
        {"id": 4746},
        {"id": 4756},
    ]
    assert [item.content for item in results] == [
        "message 4745",
        "message 4746",
        "message 4756",
    ]


def test_static_for_of_unwraps_each_nested_mcp_result_like_a_direct_call() -> None:
    source = """
        for (const id of [4745, 4746, 4756]) {
          const r = await tools.mcp__clmail__communicate({
            action: "read", actor: "/workspace/codex", params: {id}
          });
          text(JSON.stringify(r));
        }
    """
    payloads = [
        {"id": 4745, "subject": "ACK"},
        {"id": 4746, "subject": "Handoff"},
        {"id": 4756, "subject": "Delivery note"},
    ]
    emitted = [_forwarded_result_envelope(payload)[1] for payload in payloads]
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "batch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "batch",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                *emitted,
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.content for item in results] == [
        json.dumps(payload) for payload in payloads
    ]
    assert [item.is_error for item in results] == [False, False, False]


@pytest.mark.parametrize(
    ("source", "expected_name"),
    [
        (
            '/* tools.exec_command({cmd: "fake"}) */ '
            'const result = await tools.exec_command({cmd: "real"}); '
            "text(result.output);",
            "Bash",
        ),
        (
            'const result = await tools.exec_command({cmd: "echo \\"quoted\\"", '
            'env: {MODE: "test",},}); text(result);',
            "Bash",
        ),
        (
            "const result = await tools.mcp__clmail__communicate("
            '{action: "send", params: {to: "main"}}); '
            "text(JSON.stringify(result));",
            "mcp__clmail__communicate",
        ),
        (
            "const result = await tools.exec_command({cmd: `echo ${value}`}); "
            "text(result);",
            "Workflow",
        ),
        (
            'const result = await tools.exec_command({cmd: "unterminated}); '
            "text(result);",
            "Workflow",
        ),
        (
            'const result = await tools.exec_command({cmd: "real"}); text(`result`);',
            "Workflow",
        ),
        (
            'const result = await tools.exec_command({cmd: "real"}); '
            "text({nested: [result.output, {ok: true}]} );",
            "Workflow",
        ),
    ],
    ids=[
        "commented-tool",
        "escapes-nesting-trailing-commas",
        "stringified-direct-result",
        "template-expression",
        "unterminated-string",
        "result-name-in-template",
        "nested-emission-expression",
    ],
)
def test_exec_wrapper_lexical_matrix(source: str, expected_name: str) -> None:
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == expected_name


@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
def test_nested_json_helpers_tolerate_parser_failures(
    error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_json_loads(_value: str) -> object:
        raise error_type("synthetic parser limit")

    monkeypatch.setattr(json, "loads", fail_json_loads)
    source = 'const result = await tools.exec_command({cmd: "real"}); text(result);'
    assert adapt_codex_tool_call("exec", {"raw": source}, raw_input=source).name == (
        "Workflow"
    )

    provider = CodexProvider()
    value = '{"output":"safe"}'
    assert provider._tool_input(value) == {"raw": value}
    assert provider._command_result([{"type": "input_text", "text": value}]) is None


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"event_msg","payload":{"type":"user_message","message":"SECRET","n":'
        + "9" * 5000
        + "}}",
        '{"type":"event_msg","payload":{"type":"user_message","message":"SECRET","n":'
        + "[" * 10000
        + "0"
        + "]" * 10000
        + "}}",
    ],
    ids=["oversized-integer", "excessive-nesting"],
)
def test_tolerant_decoder_skips_all_json_parser_failures_without_payloads(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    line: str,
) -> None:
    path = tmp_path / "rollout-synthetic.jsonl"
    path.write_text(line + "\n")
    caplog.set_level(logging.WARNING)

    assert list(CodexProvider()._decode_records(path)) == []
    warning = "\n".join(record.getMessage() for record in caplog.records)
    assert str(path) in warning
    assert "line 1" in warning
    assert "SECRET" not in warning
