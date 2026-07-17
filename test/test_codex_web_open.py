"""Expansion of open-only Codex web batches into WebFetch pairs."""

import json
from pathlib import Path
from typing import cast

import pytest

from claude_code_log.factories.tool_factory import create_tool_input, create_tool_output
from claude_code_log.models import (
    AssistantTranscriptEntry,
    ToolResultContent,
    ToolUseContent,
    UserTranscriptEntry,
    WebFetchInput,
    WebFetchOutput,
)
from claude_code_log.providers.codex import CodexProvider


SESSION_ID = "77777777-7777-4777-8777-777777777777"


def _record(timestamp: str, type_: str, payload: dict[str, object]) -> str:
    return json.dumps({"timestamp": timestamp, "type": type_, "payload": payload})


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexProvider:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source = (
        'const r = await tools.web__run({open: [{ref_id: "https://example.invalid/a"}, '
        '{ref_id: "https://example.invalid/b"}], response_length: "long"}); '
        "text(r);"
    )
    combined = (
        '# A\n\nSource: open({"ref_id":"https://example.invalid/a"})\n\nBody A'
        "\n--------------------------------------------------------------------------------\n"
        '# B\n\nSource: open({"ref_id":"https://example.invalid/b"})\n\nBody B'
    )
    records = [
        _record(
            "2026-01-01T00:00:00Z",
            "session_meta",
            {"id": SESSION_ID, "cwd": "/workspace/synthetic"},
        ),
        _record(
            "2026-01-01T00:00:01Z",
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": source,
                "call_id": "call-open",
            },
        ),
        _record(
            "2026-01-01T00:00:02Z",
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call-open",
                "output": [
                    {
                        "type": "input_text",
                        "text": "Script completed\nWall time 0.2 seconds\nOutput:\n",
                    },
                    {"type": "input_text", "text": combined},
                ],
            },
        ),
    ]
    (sessions / f"rollout-{SESSION_ID}.jsonl").write_text("\n".join(records) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return CodexProvider()


def test_open_batch_becomes_adjacent_webfetch_pairs(provider: CodexProvider) -> None:
    entries = list(provider.load_session(SESSION_ID))
    message_entries = [
        entry
        for entry in entries
        if isinstance(entry, (UserTranscriptEntry, AssistantTranscriptEntry))
    ]
    assert len(message_entries) == len(entries)
    content = [item for entry in message_entries for item in entry.message.content]

    assert [type(item) for item in content] == [
        ToolUseContent,
        ToolResultContent,
        ToolUseContent,
        ToolResultContent,
    ]
    uses = [item for item in content if isinstance(item, ToolUseContent)]
    results = [item for item in content if isinstance(item, ToolResultContent)]
    assert [item.name for item in uses] == ["WebFetch", "WebFetch"]
    assert [item.id for item in uses] == [
        "call-open:open:0",
        "call-open:open:1",
    ]
    assert [item.tool_use_id for item in results] == [item.id for item in uses]
    assert [item.parentUuid for item in message_entries] == [
        None,
        message_entries[0].uuid,
        message_entries[1].uuid,
        message_entries[2].uuid,
    ]

    typed_inputs = [create_tool_input(item.name, item.input) for item in uses]
    typed_outputs = [create_tool_output("WebFetch", item) for item in results]
    assert all(isinstance(item, WebFetchInput) for item in typed_inputs)
    assert all(isinstance(item, WebFetchOutput) for item in typed_outputs)
    web_outputs = cast(list[WebFetchOutput], typed_outputs)
    assert "Body A" in web_outputs[0].result
    assert "Body B" in web_outputs[1].result
