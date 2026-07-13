"""Adversarial correlation and privacy cases for the Codex provider."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_code_log.models import ToolResultContent, ToolUseContent
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


def _normalized(records: list[_DecodedRecord]) -> list[object]:
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
        (
            'const args = {task_name: "one", message: "TOKEN"}; '
            "const a = await tools.spawn_agent(args); text(a);"
        ),
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
            "Bash",
        ),
    ],
    ids=[
        "commented-tool",
        "escapes-nesting-trailing-commas",
        "template-expression",
        "unterminated-string",
        "result-name-in-template",
        "nested-emission-expression",
    ],
)
def test_exec_wrapper_lexical_matrix(source: str, expected_name: str) -> None:
    call = adapt_codex_tool_call("exec", {"raw": source}, raw_input=source)
    assert call.name == expected_name


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
