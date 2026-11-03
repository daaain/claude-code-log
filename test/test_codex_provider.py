"""Frozen behavioral contract for the Codex rollout provider.

The checked-in rollout files are deliberately synthetic.  They model observed
record families without containing data copied from a real Codex session.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_code_log.models import (
    AssistantTranscriptEntry,
    BashOutput,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    TranscriptEntry,
    UserTranscriptEntry,
)
from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.providers.codex import CodexProvider


FIXTURES = Path(__file__).parent / "test_data" / "codex"
ROOT_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"
CHILD_ID = "33333333-3333-4333-8333-333333333333"
LEGACY_ID = "44444444-4444-4444-8444-444444444444"
ARCHIVED_ID = "55555555-5555-4555-8555-555555555555"
ENCRYPTED_SENTINEL = "SYNTHETIC_ENCRYPTED_CONTENT_MUST_NOT_RENDER"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> CodexProvider:
    monkeypatch.setenv("CODEX_HOME", str(FIXTURES))
    return CodexProvider()


def _content(entries: Sequence[TranscriptEntry]) -> list[object]:
    return [
        item
        for entry in entries
        if isinstance(entry, (UserTranscriptEntry, AssistantTranscriptEntry))
        for item in entry.message.content
    ]


def _visible_text(entries: Sequence[TranscriptEntry]) -> list[str]:
    return [
        text
        for item in _content(entries)
        if (text := getattr(item, "text", None)) is not None
    ]


def test_data_dir_honors_codex_home(provider: CodexProvider) -> None:
    assert provider.get_data_dir() == FIXTURES
    assert provider.get_provider_name() == "codex"
    assert provider.get_session_format() == "jsonl"


def test_entry_uuid_exposes_record_position_before_thread_id(
    provider: CodexProvider,
) -> None:
    assert provider._entry_uuid(ROOT_ID, 123, 4) == f"c123-4-{ROOT_ID}"


def test_discovery_is_recursive_deterministic_and_active_only(
    provider: CodexProvider,
) -> None:
    first = list(provider.discover_sessions())
    second = list(provider.discover_sessions())
    first_ids = [session.session_id for session in first]
    assert first_ids == [session.session_id for session in second]
    assert set(first_ids) == {ROOT_ID, PARENT_ID, CHILD_ID, LEGACY_ID}
    assert ARCHIVED_ID not in first_ids
    assert all(session.provider == "codex" for session in first)


def test_rollout_discovery_rejects_symlinks_outside_sessions(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    outside = tmp_path / "rollout-external.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    link = sessions / "rollout-external.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert CodexProvider()._rollout_paths(home) == []


def test_load_deduplicates_visible_messages_and_preserves_order(
    provider: CodexProvider,
) -> None:
    entries = list(provider.load_session(ROOT_ID))
    base_entries = [
        entry
        for entry in entries
        if isinstance(entry, (UserTranscriptEntry, AssistantTranscriptEntry))
    ]
    assert len(base_entries) == len(entries)
    visible = _visible_text(entries)
    assert visible.count("List the synthetic files.") == 1
    assert visible.count("The synthetic files are alpha.txt and beta.txt.") == 1
    assert visible.index("List the synthetic files.") < visible.index(
        "The synthetic files are alpha.txt and beta.txt."
    )
    assert [entry.parentUuid for entry in base_entries] == [
        None,
        *[entry.uuid for entry in base_entries[:-1]],
    ]


def test_reasoning_summary_is_visible_but_encrypted_reasoning_is_not(
    provider: CodexProvider,
) -> None:
    entries = list(provider.load_session(ROOT_ID))
    content = _content(entries)
    assert any(
        isinstance(item, ThinkingContent)
        and item.thinking == "I will inspect the synthetic directory."
        for item in content
    )
    assert ENCRYPTED_SENTINEL not in repr(entries)


def test_structured_custom_and_open_ended_tools_are_correlated(
    provider: CodexProvider,
) -> None:
    content = _content(list(provider.load_session(ROOT_ID)))
    uses = {item.id: item for item in content if isinstance(item, ToolUseContent)}
    results = {
        item.tool_use_id: item
        for item in content
        if isinstance(item, ToolResultContent)
    }
    assert (
        uses.keys()
        == results.keys()
        == {
            "call-structured-001",
            "call-custom-001",
            "call-mcp-001",
            "call-async-001",
        }
    )
    assert uses["call-structured-001"].name == "Bash"
    assert uses["call-custom-001"].name == "Write"
    assert uses["call-custom-001"].input == {
        "file_path": "gamma.txt",
        "content": "synthetic\n",
    }
    assert uses["call-mcp-001"].name == "mcp__synthetic__lookup"
    assert results["call-structured-001"].content == "./alpha.txt\n./beta.txt"
    bash_output = create_tool_output(
        "Bash", results["call-structured-001"], file_path=None
    )
    assert isinstance(bash_output, BashOutput)
    assert bash_output.content == "./alpha.txt\n./beta.txt"
    assert uses["call-async-001"].name == "Bash"
    assert results["call-async-001"].content == "tests running...\n2 passed\n"
    assert "call-wait-001" not in uses
    assert "call-write-001" not in uses
    assert "Done!" in str(results["call-custom-001"].content)


def test_max_messages_counts_normalized_entries(provider: CodexProvider) -> None:
    all_entries = list(provider.load_session(ROOT_ID))
    limited = list(provider.load_session(ROOT_ID, max_messages=4))
    assert len(all_entries) > 4
    assert limited == all_entries[:4]


def test_session_lookup_is_exact_and_rejects_invalid_ids(
    provider: CodexProvider,
) -> None:
    with pytest.raises((FileNotFoundError, ValueError)):
        list(provider.load_session(ROOT_ID[:12]))
    with pytest.raises(ValueError):
        list(provider.load_session("../synthetic"))


def test_flat_legacy_rollout_is_normalized(provider: CodexProvider) -> None:
    entries = list(provider.load_session(LEGACY_ID))
    assert _visible_text(entries) == [
        "Open the synthetic legacy session.",
        "The synthetic legacy session is open.",
    ]


def test_child_metadata_retains_lineage_and_inherited_prefix(
    provider: CodexProvider,
) -> None:
    info = next(
        item for item in provider.discover_sessions() if item.session_id == CHILD_ID
    )
    # These fields are the frozen extension point for later hierarchical rendering.
    assert getattr(info, "parent_thread_id") == PARENT_ID
    assert getattr(info, "forked_from_id") == "fork-item-001"
    assert getattr(info, "spawn_call_id") == "call-spawn-001"
    assert getattr(info, "source_kind") == "subagent"
    assert getattr(info, "inherited_prefix_records") == 2


def test_unknown_and_malformed_records_warn_without_leaking_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    source = (
        FIXTURES / "malformed" / ("rollout-66666666-6666-4666-8666-666666666666.jsonl")
    )
    shutil.copyfile(source, sessions / source.name)
    monkeypatch.setenv("CODEX_HOME", str(home))
    caplog.set_level(logging.WARNING)

    entries = list(CodexProvider().load_session("66666666-6666-4666-8666-666666666666"))

    assert _visible_text(entries) == ["Safe line before malformed input."]
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert source.name in warnings
    assert "line 3" in warnings.lower()
    assert "Safe line before malformed input." not in warnings
    assert "synthetic malformed json" not in warnings


def test_fixture_json_and_privacy_contract() -> None:
    allowed_path = "/workspace/synthetic-project"
    forbidden_fragments = (
        "/home/",
        "/Users/",
        "github.com/",
        "Bearer ",
        "api_key",
        "access_token",
        "refresh_token",
        "@example.",
    )
    for path in FIXTURES.rglob("*.jsonl"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                assert path.parent.name == "malformed" and line_number == 3
                continue
            serialized = json.dumps(record)
            assert not any(fragment in serialized for fragment in forbidden_fragments)
            cwd = record.get("payload", {}).get("cwd")
            assert cwd is None or cwd == allowed_path
