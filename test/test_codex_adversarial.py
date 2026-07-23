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

    assert call.name == "ToolExecution"
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

    assert call.name == "ToolExecution"
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


def test_async_bash_folds_wait_and_write_stdin_across_ignored_records() -> None:
    write_source = (
        'const r = await tools.write_stdin({session_id: 73978, chars: ""}); '
        "text(JSON.stringify(r));"
    )
    records = [
        _call(1, "exec", "exec_command", '{"cmd":"pytest"}'),
        _output(2, "exec", "Script running with cell ID 14"),
        _record(3, "event_msg", {"type": "token_count"}),
        _record(
            4,
            "response_item",
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Prefix approved"}],
            },
        ),
        _record(5, "response_item", {"type": "reasoning", "summary": []}),
        _record(6, "inter_agent_communication_metadata", {"trigger_turn": False}),
        _record(
            7,
            "response_item",
            {
                "type": "agent_message",
                "author": "/root/researcher",
                "recipient": "/root",
                "content": [{"type": "input_text", "text": "Internal result"}],
            },
        ),
        _call(8, "wait", "wait", '{"cell_id":"14"}'),
        _output(
            9,
            "wait",
            _command_envelope(
                output="bringing up nodes... [98%]\n",
                session_id=73978,
                wall_time_seconds=30.0,
            ),
        ),
        _record(10, "event_msg", {"type": "token_count"}),
        _record(11, "response_item", {"type": "reasoning", "summary": []}),
        _record(
            12,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "write",
                "name": "exec",
                "input": write_source,
            },
        ),
        _output(
            13,
            "write",
            _command_envelope(
                output="2335 passed, 7 skipped\n",
                exit_code=0,
                wall_time_seconds=7.9,
            ),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.id for item in uses] == ["exec"]
    assert [item.tool_use_id for item in results] == ["exec"]
    assert results[0].content == (
        "bringing up nodes... [98%]\n2335 passed, 7 skipped\n"
    )
    assert results[0].is_error is False


def test_async_bash_folds_json_session_and_direct_write_stdin_poll() -> None:
    origin_source = (
        'const r = await tools.exec_command({cmd:"uv run pytest -m tui -q", '
        "yield_time_ms:1000,max_output_tokens:12000,tty:true}); "
        "text(JSON.stringify(r));"
    )
    poll_source = (
        'const r = await tools.write_stdin({session_id:41447,chars:"",'
        "yield_time_ms:10000,max_output_tokens:12000}); text(JSON.stringify(r));"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "origin",
                "name": "exec",
                "input": origin_source,
            },
        ),
        _output(
            2,
            "origin",
            _command_envelope(
                chunk_id="af326b",
                wall_time_seconds=1.0,
                session_id=41447,
                output="bringing up nodes...\n",
            ),
        ),
        _record(
            3,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "poll",
                "name": "exec",
                "input": poll_source,
            },
        ),
        _output(
            4,
            "poll",
            _command_envelope(
                chunk_id="7a379c",
                wall_time_seconds=0.0,
                exit_code=0,
                output="68 passed in 8.34s\n",
            ),
        ),
    ]

    entries = _normalized(records)
    content = [item for entry in entries for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    result_entry = next(
        entry
        for entry in entries
        if any(isinstance(item, ToolResultContent) for item in entry.message.content)
    )

    assert [(item.id, item.name) for item in uses] == [("origin", "Bash")]
    assert [item.tool_use_id for item in results] == ["origin"]
    assert results[0].content == "bringing up nodes...\n68 passed in 8.34s\n"
    assert results[0].is_error is False
    assert result_entry.timestamp == "2026-07-14T00:00:04Z"


def test_async_bash_folds_write_stdin_outer_cell_wait_and_final_poll() -> None:
    origin_source = (
        'const r = await tools.exec_command({cmd:"uv run pytest -m integration -q", '
        "yield_time_ms:1000,max_output_tokens:12000,tty:true}); "
        "text(JSON.stringify(r));"
    )
    poll_source = (
        'const r = await tools.write_stdin({session_id:82784,chars:"",'
        "yield_time_ms:10000,max_output_tokens:12000}); text(JSON.stringify(r));"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "origin",
                "name": "exec",
                "input": origin_source,
            },
        ),
        _output(
            2,
            "origin",
            _command_envelope(
                chunk_id="first",
                wall_time_seconds=1.0,
                session_id=82784,
                output="bringing up nodes...\n",
            ),
        ),
        _record(
            3,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "slow-poll",
                "name": "exec",
                "input": poll_source,
            },
        ),
        _output(
            4,
            "slow-poll",
            "Script running with cell ID 45\nWall time 10.2 seconds\nOutput:\n",
        ),
        _call(5, "wait", "wait", '{"cell_id":"45"}'),
        _output(
            6,
            "wait",
            _command_envelope(
                chunk_id="middle",
                wall_time_seconds=10.0,
                session_id=82784,
                output="integration tests running...\n",
            ),
        ),
        _record(
            7,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "final-poll",
                "name": "exec",
                "input": poll_source,
            },
        ),
        _output(
            8,
            "final-poll",
            _command_envelope(
                chunk_id="final",
                wall_time_seconds=6.9,
                exit_code=0,
                output="78 passed, 7 skipped\n",
            ),
        ),
    ]

    entries = _normalized(records)
    content = [item for entry in entries for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    result_entry = next(
        entry
        for entry in entries
        if any(isinstance(item, ToolResultContent) for item in entry.message.content)
    )

    assert [(item.id, item.name) for item in uses] == [("origin", "Bash")]
    assert [item.tool_use_id for item in results] == ["origin"]
    assert results[0].content == (
        "bringing up nodes...\nintegration tests running...\n78 passed, 7 skipped\n"
    )
    assert results[0].is_error is False
    assert result_entry.timestamp == "2026-07-14T00:00:08Z"


def test_async_bash_does_not_fold_across_task_boundary() -> None:
    records = [
        _call(1, "exec", "exec_command", '{"cmd":"pytest"}'),
        _output(2, "exec", "Script running with cell ID 14"),
        _record(3, "event_msg", {"type": "task_complete"}),
        _call(4, "wait", "wait", '{"cell_id":"14"}'),
        _output(
            5,
            "wait",
            _command_envelope(output="passed\n", exit_code=0),
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    assert [item.id for item in content if isinstance(item, ToolUseContent)] == [
        "exec",
        "wait",
    ]


def test_ordered_session_marker_poll_folds_after_outer_cell_wait() -> None:
    origin_source = """
        const r = await tools.exec_command({cmd: "uv run pytest -q -n 0 test/test_tui.py"});
        text(r.output);
        if (r.session_id) text(`SESSION_ID=${r.session_id}`);
    """
    poll_source = """
        const r = await tools.write_stdin({session_id: 54193, chars: ""});
        text(r.output);
        if (r.session_id) text(`SESSION_ID=${r.session_id}`);
    """
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "origin",
                "name": "exec",
                "input": origin_source,
            },
        ),
        _output(2, "origin", "Script running with cell ID 42\nOutput:\n"),
        _call(3, "wait", "wait", '{"cell_id":"42"}'),
        _output(
            4,
            "wait",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 18.6 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "validation errors ...\n"},
                {"type": "input_text", "text": "SESSION_ID=54193"},
            ],
        ),
        _record(
            5,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "poll",
                "name": "exec",
                "input": poll_source,
            },
        ),
        _output(
            6,
            "poll",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.0 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "63 passed in 29.76s\n"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash"]
    assert [item.input["command"] for item in uses] == [
        "uv run pytest -q -n 0 test/test_tui.py"
    ]
    assert [item.content for item in results] == [
        "validation errors ...\n63 passed in 29.76s\n"
    ]


def test_parallel_marker_sessions_fold_into_their_originating_bash_calls() -> None:
    origin_source = """
        const results = await Promise.all([
          tools.exec_command({cmd: "pytest"}),
          tools.exec_command({cmd: "pyright"}),
          tools.exec_command({cmd: "git diff --check"})
        ]);
        results.forEach((r, i) => {
          text(`RESULT_${i+1}`); text(r.output);
          if (r.session_id) text(`SESSION_ID=${r.session_id}`)
        });
    """
    parallel_poll = """
        const results = await Promise.all([
          tools.write_stdin({session_id: 52391, chars: ""}),
          tools.write_stdin({session_id: 92535, chars: ""})
        ]);
        results.forEach((r, i) => {
          text(`RESULT_${i+1}`); text(r.output);
          if (r.session_id) text(`SESSION_ID=${r.session_id}`)
        });
    """
    final_poll = (
        'const r = await tools.write_stdin({session_id: 52391, chars: ""}); '
        "text(r.output); if (r.session_id) text(`SESSION_ID=${r.session_id}`);"
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "origin",
                "name": "exec",
                "input": origin_source,
            },
        ),
        _output(
            2,
            "origin",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 1.0 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "RESULT_1"},
                {"type": "input_text", "text": "pytest start\n"},
                {"type": "input_text", "text": "SESSION_ID=52391"},
                {"type": "input_text", "text": "RESULT_2"},
                {"type": "input_text", "text": "pyright start\n"},
                {"type": "input_text", "text": "SESSION_ID=92535"},
                {"type": "input_text", "text": "RESULT_3"},
                {"type": "input_text", "text": "diff complete\n"},
            ],
        ),
        _record(3, "event_msg", {"type": "token_count"}),
        _record(
            4,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "parallel-poll",
                "name": "exec",
                "input": parallel_poll,
            },
        ),
        _output(5, "parallel-poll", "Script running with cell ID 39"),
        _record(6, "response_item", {"type": "reasoning", "summary": []}),
        _call(7, "wait", "wait", '{"cell_id":"39"}'),
        _output(
            8,
            "wait",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 23.0 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "RESULT_1"},
                {"type": "input_text", "text": "pytest middle\n"},
                {"type": "input_text", "text": "SESSION_ID=52391"},
                {"type": "input_text", "text": "RESULT_2"},
                {"type": "input_text", "text": "pyright complete\n"},
            ],
        ),
        _record(9, "event_msg", {"type": "token_count"}),
        _record(
            10,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "final-poll",
                "name": "exec",
                "input": final_poll,
            },
        ),
        _output(
            11,
            "final-poll",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 1.6 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "pytest complete\n"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash", "Bash", "Bash"]
    assert [item.input["command"] for item in uses] == [
        "pytest",
        "pyright",
        "git diff --check",
    ]
    assert [item.content for item in results] == [
        "pytest start\npytest middle\npytest complete\n",
        "pyright start\npyright complete\n",
        "diff complete\n",
    ]


def test_incomplete_parallel_marker_sessions_remain_workflow() -> None:
    source = r"""
        const results = await Promise.all([
          tools.exec_command({cmd: "pytest"}),
          tools.exec_command({cmd: "pyright"})
        ]);
        results.forEach((r, i) => {
          text(`RESULT_${i+1}`); text(r.output);
          if (r.session_id) text(`SESSION_ID=${r.session_id}`)
        });
    """
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "origin",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "origin",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 1.0 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": "RESULT_1"},
                {"type": "input_text", "text": ""},
                {"type": "input_text", "text": "SESSION_ID=52391"},
                {"type": "input_text", "text": "RESULT_2"},
                {"type": "input_text", "text": "pyright complete\n"},
            ],
        ),
    ]

    uses = [
        item
        for entry in _normalized(records)
        for item in entry.message.content
        if isinstance(item, ToolUseContent)
    ]

    assert [item.name for item in uses] == ["ToolExecution"]


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


def test_static_join_body_keeps_nested_tool_plugin_rendering() -> None:
    payload = {"sent": True, "message_ids": [4813]}
    source = r"""
        const r = await tools.mcp__clmail__communicate({
          action: "send",
          actor: "/workspace/codex",
          params: {
            to: "/workspace/clmail/main",
            subject: "Lifecycle verified",
            body: ["Automatic delivery confirmed.", "", "Plugin viable."].join("\n")
          }
        });
        text(JSON.stringify(r));
    """
    records = [
        _call(1, "exec", "exec", source),
        _output(2, "exec", _forwarded_result_envelope(payload)),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    use = next(item for item in content if isinstance(item, ToolUseContent))
    result = next(item for item in content if isinstance(item, ToolResultContent))

    assert use.name == "mcp__clmail__communicate"
    assert use.input["params"]["body"] == (
        "Automatic delivery confirmed.\n\nPlugin viable."
    )
    assert result.content == json.dumps(payload)


def test_static_delay_before_nested_tool_becomes_wait_then_tool() -> None:
    payload = {"messages": [{"id": 4812}]}
    source = """
        await new Promise(resolve => setTimeout(resolve, 5000));
        const r = await tools.mcp__clmail__communicate({
          action: "list", actor: "/workspace/codex", params: {status: "unread"}
        });
        text(JSON.stringify(r));
    """
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "exec",
                "name": "exec",
                "input": source,
            },
        ),
        _output(2, "exec", _forwarded_result_envelope(payload)),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["wait", "mcp__clmail__communicate"]
    assert uses[0].input == {"delay_ms": 5000}
    assert uses[1].input == {
        "action": "list",
        "actor": "/workspace/codex",
        "params": {"status": "unread"},
    }
    assert [item.content for item in results] == [
        "Waited 5000 ms",
        json.dumps(payload),
    ]


@pytest.mark.parametrize("mcp_event_before_wait", [True, False])
def test_static_delay_nested_tool_coalesces_outer_cell_wait(
    mcp_event_before_wait: bool,
) -> None:
    payload = {"thread_id": 4743, "messages": [{"id": 4743}], "count": 1}
    source = """
        await new Promise(resolve => setTimeout(resolve, 20000));
        const r = await tools.mcp__clmail__communicate({
          action: "thread", actor: "/workspace/codex", params: {id: 4743}
        });
        text(JSON.stringify(r));
    """
    start = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "exec",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "exec",
            "Script running with cell ID 13\nWall time 10.0 seconds\nOutput:\n",
        ),
        _record(3, "event_msg", {"type": "token_count"}),
    ]
    mcp_event = _record(
        4 if mcp_event_before_wait else 5,
        "event_msg",
        {
            "type": "mcp_tool_call_end",
            "call_id": "exec-inner-mcp",
            "result": {"Ok": {"structuredContent": payload}},
        },
    )
    wait = _call(
        5 if mcp_event_before_wait else 4,
        "poll",
        "wait",
        '{"cell_id":"13","yield_time_ms":15000}',
    )
    records = [
        *start,
        *([mcp_event, wait] if mcp_event_before_wait else [wait, mcp_event]),
        _output(6, "poll", _forwarded_result_envelope(payload)),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["wait", "mcp__clmail__communicate"]
    assert uses[0].input == {"delay_ms": 20000}
    assert uses[1].input == {
        "action": "thread",
        "actor": "/workspace/codex",
        "params": {"id": 4743},
    }
    assert [item.content for item in results] == [
        "Waited 20000 ms",
        json.dumps(payload),
    ]


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
    assert [item.name for item in uses] == ["ToolExecution"]
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
        "ToolExecution"
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


def test_mixed_apply_patch_becomes_write_and_edit_result_pairs() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: created.txt\n+created\n"
        "*** Update File: existing.txt\n@@\n-old\n+new\n"
        "*** End Patch"
    )
    source = f"const patch = {json.dumps(patch)}; text(await tools.apply_patch(patch));"
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "patch",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "patch",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "{}"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Write", "Edit"]
    assert [item.id for item in uses] == ["patch:batch:0", "patch:batch:1"]
    assert uses[0].input == {"file_path": "created.txt", "content": "created\n"}
    assert uses[1].input == {
        "file_path": "existing.txt",
        "old_string": "old\n",
        "new_string": "new\n",
    }
    assert [item.tool_use_id for item in results] == [
        "patch:batch:0",
        "patch:batch:1",
    ]
    assert [item.content for item in results] == [
        "Script completed\nOutput:\n",
        "Script completed\nOutput:\n",
    ]


def test_delete_add_relocation_becomes_ordered_delete_write_pairs() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: /tmp/plugin/hooks.json\n"
        '*** Add File: /tmp/plugin/hooks/hooks.json\n+{"hooks": {}}\n'
        "*** End Patch"
    )
    source = f"const patch = {json.dumps(patch)}; text(await tools.apply_patch(patch));"
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "relocate",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "relocate",
            [
                {"type": "input_text", "text": "Script completed\nOutput:\n"},
                {"type": "input_text", "text": "{}"},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Delete", "Write"]
    assert [item.input for item in uses] == [
        {"file_path": "/tmp/plugin/hooks.json"},
        {
            "file_path": "/tmp/plugin/hooks/hooks.json",
            "content": '{"hooks": {}}\n',
        },
    ]
    assert [item.tool_use_id for item in results] == [
        "relocate:batch:0",
        "relocate:batch:1",
    ]
    assert [item.content for item in results] == [
        "Script completed\nOutput:\n",
        "Script completed\nOutput:\n",
    ]


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


def test_static_array_map_batch_extracts_spread_command_outputs() -> None:
    source = """
        const cmds = [
          ["pyright", "uv run pyright"],
          ["unit", "uv run pytest -q"]
        ];
        const results = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({cmd, workdir: "/workspace"});
          return {name, ...r};
        }));
        for (const r of results) text(JSON.stringify(r));
    """
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "mapped",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "mapped",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                },
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {"name": "pyright", "exit_code": 2, "output": "type error\n"}
                    ),
                },
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {"name": "unit", "exit_code": 0, "output": "63 passed\n"}
                    ),
                },
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash", "Bash"]
    assert [item.input["command"] for item in uses] == [
        "uv run pyright",
        "uv run pytest -q",
    ]
    assert [item.content for item in results] == ["type error\n", "63 passed\n"]


def test_static_array_map_batch_extracts_explicit_command_outputs() -> None:
    source = """
        const cmds = [
          ["guide", "rg --files -g 'AGENTS.md'"],
          ["status", "git status --short --branch"],
          ["head", "git log -1 --oneline"]
        ];
        const out = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({cmd, workdir: "/workspace"});
          return {name, exit_code: r.exit_code, output: r.output};
        }));
        out.forEach(text);
    """
    emitted = [
        {"name": "guide", "exit_code": 1, "output": ""},
        {"name": "status", "exit_code": 0, "output": "## feature\n"},
        {"name": "head", "exit_code": 0, "output": "abc1234 Subject\n"},
    ]
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "mapped-explicit",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "mapped-explicit",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                },
                *[{"type": "input_text", "text": json.dumps(item)} for item in emitted],
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash"] * 3
    assert [item.input["command"] for item in uses] == [
        "rg --files -g 'AGENTS.md'",
        "git status --short --branch",
        "git log -1 --oneline",
    ]
    assert [item.content for item in results] == [
        "",
        "## feature\n",
        "abc1234 Subject\n",
    ]


def test_static_array_map_batch_splits_consolidated_truncated_objects() -> None:
    source = """
        const qs = [
          ["one", "printf one"],
          ["two", "printf two"],
          ["three", "printf three"]
        ];
        const out = await Promise.all(qs.map(async ([name, cmd]) => {
          const r = await tools.exec_command({cmd, workdir: "/workspace"});
          return {name, output: r.output};
        }));
        out.forEach(text);
    """
    emitted = (
        "Warning: truncated output (original token count: 30000)\n"
        "Total output lines: 3\n\n"
        + json.dumps({"name": "one", "output": "first\n"}, separators=(",", ":"))
        + '{"name":"two","output":"cut…99 tokens truncated…'
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "mapped-consolidated",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "mapped-consolidated",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": emitted},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash"] * 3
    assert [item.content for item in results] == [
        "first\n",
        "[Output omitted by Codex truncation]",
        "[Output omitted by Codex truncation]",
    ]


def test_sequential_object_batch_recovers_intact_tail_after_truncation() -> None:
    source = """
        const actor = "/workspace/codex";
        const p = await tools.mcp__clmail__actors({
          action: "presence", actor, params: {status: "all"}
        });
        const m = await tools.mcp__clmail__communicate({
          action: "list", actor, params: {status: "unread"}
        });
        text(JSON.stringify({presence: p, mail: m}));
    """
    emitted = (
        "Warning: truncated output (original token count: 10000)\n"
        "Total output lines: 1\n\n"
        '{"presence":{"content":"cut…99 tokens truncated…:broken},'
        '"mail":{"messages":[],"count":0}}'
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "clmail-object",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "clmail-object",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": emitted},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == [
        "mcp__clmail__actors",
        "mcp__clmail__communicate",
    ]
    assert [item.content for item in results] == [
        "[Output omitted by Codex truncation]",
        '{"messages": [], "count": 0}',
    ]


def test_openai_docs_object_batch_becomes_three_doc_pairs() -> None:
    source = """
        const [hooks, plugins, marketplace] = await Promise.all([
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({
            url: "https://learn.chatgpt.com/docs/hooks", anchor: "#plugin-bundled-hooks"
          }),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({
            url: "https://learn.chatgpt.com/docs/build-plugins", anchor: "#bundled-mcp-servers-and-lifecycle-hooks"
          }),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({
            url: "https://learn.chatgpt.com/docs/build-plugins", anchor: "#add-a-marketplace-from-the-cli"
          })
        ]);
        text(JSON.stringify({hooks,plugins,marketplace}));
    """
    documents = {
        "hooks": {"content": [{"type": "text", "text": "# Hooks\nHook body"}]},
        "plugins": {"content": [{"type": "text", "text": "# Plugins\nPlugin body"}]},
        "marketplace": {
            "content": [{"type": "text", "text": "# Marketplace\nMarket body"}]
        },
    }
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "docs",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "docs",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": json.dumps(documents)},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["CodexDoc"] * 3
    assert [item.id for item in uses] == [
        "docs:batch:0",
        "docs:batch:1",
        "docs:batch:2",
    ]
    assert [item.content for item in results] == [
        "# Hooks\nHook body",
        "# Plugins\nPlugin body",
        "# Marketplace\nMarket body",
    ]


def test_openai_docs_object_batches_survive_transport_truncation_preamble() -> None:
    source = """
        const [a, m] = await Promise.all([
          tools.mcp__openaiDeveloperDocs__search_openai_docs({query:"approval", limit:5}),
          tools.mcp__openaiDeveloperDocs__search_openai_docs({query:"MCP", limit:5})
        ]);
        text(JSON.stringify({approval:a,mcp:m}));
    """
    search_result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "hits": [
                            {
                                "url": "https://learn.chatgpt.com/docs/hooks",
                                "hierarchy": {"lvl1": "Hooks", "lvl2": None},
                            }
                        ]
                    }
                ),
            }
        ]
    }
    emitted = (
        "Warning: truncated output (original token count: 39941)\n"
        "Total output lines: 1\n\n" + json.dumps({"approval": search_result})
    )
    records = [
        _record(
            1,
            "response_item",
            {
                "type": "custom_tool_call",
                "call_id": "docs-search",
                "name": "exec",
                "input": source,
            },
        ),
        _output(
            2,
            "docs-search",
            [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.7 seconds\nOutput:\n",
                },
                {"type": "input_text", "text": emitted},
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["CodexDocSearch"] * 2
    assert [item.content for item in results] == [
        json.dumps(json.loads(search_result["content"][0]["text"]), ensure_ascii=False),
        "[Output omitted by Codex truncation]",
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

    assert [item.name for item in uses] == ["ToolExecution"]


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

    assert [item.name for item in uses] == ["ToolExecution"]


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


def test_static_path_loop_becomes_template_bash_result_pairs() -> None:
    source = r"""
        const paths = [
          "/tmp/github/SKILL.md",
          "/tmp/openai-docs/SKILL.md"
        ];
        for (const path of paths) {
          const r = await tools.exec_command({
            cmd: `sed -n '1,260p' '${path}'`,
            workdir: "/workspace"
          });
          text(`FILE ${path}\n${r.output}`);
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
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.1 seconds\nOutput:\n",
                },
                {
                    "type": "input_text",
                    "text": "FILE /tmp/github/SKILL.md\nGitHub skill",
                },
                {
                    "type": "input_text",
                    "text": "FILE /tmp/openai-docs/SKILL.md\nOpenAI docs skill",
                },
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash", "Bash"]
    assert [item.input["command"] for item in uses] == [
        "sed -n '1,260p' '/tmp/github/SKILL.md'",
        "sed -n '1,260p' '/tmp/openai-docs/SKILL.md'",
    ]
    assert [item.content for item in results] == [
        "FILE /tmp/github/SKILL.md\nGitHub skill",
        "FILE /tmp/openai-docs/SKILL.md\nOpenAI docs skill",
    ]


def test_static_destructured_range_loop_becomes_bash_result_pairs() -> None:
    source = r"""
        const ranges = [
          ["claude_code_log/cli.py", 570, 850],
          ["claude_code_log/models.py", 1, 240]
        ];
        for (const [f, a, b] of ranges) {
          const r = await tools.exec_command({
            cmd: `sed -n '${a},${b}p' '${f}'`,
            workdir: "/workspace"
          });
          text(`FILE ${f}:${a}\n${r.output}`);
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
                {
                    "type": "input_text",
                    "text": "FILE claude_code_log/cli.py:570\nCLI source",
                },
                {
                    "type": "input_text",
                    "text": "FILE claude_code_log/models.py:1\nModels source",
                },
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.name for item in uses] == ["Bash", "Bash"]
    assert [item.input["command"] for item in uses] == [
        "sed -n '570,850p' 'claude_code_log/cli.py'",
        "sed -n '1,240p' 'claude_code_log/models.py'",
    ]
    assert [item.content for item in results] == [
        "FILE claude_code_log/cli.py:570\nCLI source",
        "FILE claude_code_log/models.py:1\nModels source",
    ]


def test_truncated_consolidated_range_loop_preserves_known_boundaries() -> None:
    source = r"""
        const ranges = [
          ["cli.py", 570, 850],
          ["workflow.py", 1, 280],
          ["models.py", 1, 240]
        ];
        for (const [f, a, b] of ranges) {
          const r = await tools.exec_command({cmd: `sed -n '${a},${b}p' '${f}'`});
          text(`FILE ${f}:${a}\n${r.output}`);
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
                {
                    "type": "input_text",
                    "text": (
                        "Warning: truncated output (original token count: 10000)\n"
                        "FILE cli.py:570\nCLI source\n"
                        "… middle omitted …\n"
                        "FILE models.py:1\nModels source"
                    ),
                },
            ],
        ),
    ]

    content = [item for entry in _normalized(records) for item in entry.message.content]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]

    assert [item.input["command"] for item in uses] == [
        "sed -n '570,850p' 'cli.py'",
        "sed -n '1,280p' 'workflow.py'",
        "sed -n '1,240p' 'models.py'",
    ]
    assert [item.content for item in results] == [
        "Warning: truncated output (original token count: 10000)\n"
        "FILE cli.py:570\nCLI source\n… middle omitted …",
        "[Output omitted by Codex truncation]",
        "FILE models.py:1\nModels source",
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
            "ToolExecution",
        ),
        (
            'const result = await tools.exec_command({cmd: "unterminated}); '
            "text(result);",
            "ToolExecution",
        ),
        (
            'const result = await tools.exec_command({cmd: "real"}); text(`result`);',
            "ToolExecution",
        ),
        (
            'const result = await tools.exec_command({cmd: "real"}); '
            "text({nested: [result.output, {ok: true}]} );",
            "ToolExecution",
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
        "ToolExecution"
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
