"""Cross-provider discovery, lookup, and normalized-entry contracts."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from claude_code_log.providers.agy import AgyProvider
from claude_code_log.providers.base import BaseProvider
from claude_code_log.providers.claude import ClaudeProvider
from claude_code_log.providers.codex import CodexProvider
from claude_code_log.providers.registry import ProviderRegistry


CODEX_FIXTURES = Path(__file__).parent / "test_data" / "codex"
CODEX_ID = "11111111-1111-4111-8111-111111111111"


def _claude_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ClaudeProvider:
    projects = tmp_path / "claude-projects"
    project = projects / "synthetic-project"
    project.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parent / "test_data" / "dag_simple.jsonl",
        project / "session-a.jsonl",
    )
    provider = ClaudeProvider()
    monkeypatch.setattr(provider, "get_data_dir", lambda: projects)
    return provider


def _agy_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgyProvider:
    root = tmp_path / "agy"
    logs = root / "brain" / "abcd" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    records = [
        {
            "type": "USER_INPUT",
            "created_at": "2026-07-14T00:00:00Z",
            "content": "Start",
        },
        {
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-07-14T00:00:01Z",
            "content": "Finished",
            "tool_calls": [
                {"name": "first", "args": {"value": 1}},
                {"name": "second", "args": {"value": 2}},
            ],
        },
    ]
    (logs / "transcript.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )
    provider = AgyProvider()
    monkeypatch.setattr(provider, "get_data_dir", lambda: root)
    return provider


@pytest.mark.parametrize("provider_class", [ClaudeProvider, AgyProvider, CodexProvider])
def test_unavailable_provider_has_empty_discovery_and_clear_load_error(
    provider_class: type[BaseProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_class()
    monkeypatch.setattr(provider, "get_data_dir", lambda: None)

    assert provider.is_available() is False
    assert list(provider.discover_sessions()) == []
    with pytest.raises(ValueError, match="data directory not found"):
        list(provider.load_session("abcd"))


def test_claude_contract_and_strict_normalized_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _claude_provider(tmp_path, monkeypatch)

    assert [item.session_id for item in provider.discover_sessions()] == ["session-a"]
    entries = list(provider.load_session("session-a"))
    assert len(entries) > 2
    assert list(provider.load_session("session-a", max_messages=2)) == entries[:2]
    with pytest.raises(ValueError, match="Invalid session_id"):
        list(provider.load_session("../session-a"))


def test_agy_contract_caps_expanded_raw_record_and_chains_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _agy_provider(tmp_path, monkeypatch)

    assert [item.session_id for item in provider.discover_sessions()] == ["abcd"]
    entries = list(provider.load_session("abcd"))
    assert len(entries) == 4
    assert [entry.parentUuid for entry in entries] == [
        None,
        entries[0].uuid,
        entries[1].uuid,
        entries[2].uuid,
    ]
    assert list(provider.load_session("abcd", max_messages=2)) == entries[:2]
    with pytest.raises(ValueError, match="Invalid session_id"):
        list(provider.load_session("../abcd"))


def test_codex_contract_caps_normalized_entries_and_chains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(CODEX_FIXTURES))
    provider = CodexProvider()
    entries = list(provider.load_session(CODEX_ID))

    assert list(provider.load_session(CODEX_ID, max_messages=3)) == entries[:3]
    assert [entry.parentUuid for entry in entries] == [
        None,
        *[entry.uuid for entry in entries[:-1]],
    ]


def test_discovery_order_is_deterministic_for_directory_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude_provider(tmp_path, monkeypatch)
    first_project = claude.get_data_dir() / "synthetic-project"  # type: ignore[operator]
    shutil.copyfile(
        first_project / "session-a.jsonl", first_project / "session-z.jsonl"
    )
    shutil.copyfile(
        first_project / "session-a.jsonl", first_project / "session-b.jsonl"
    )
    assert [item.session_id for item in claude.discover_sessions()] == [
        "session-a",
        "session-b",
        "session-z",
    ]

    agy = _agy_provider(tmp_path, monkeypatch)
    brain = agy.get_data_dir() / "brain"  # type: ignore[operator]
    shutil.copytree(brain / "abcd", brain / "ffff")
    shutil.copytree(brain / "abcd", brain / "beef")
    assert [item.session_id for item in agy.discover_sessions()] == [
        "abcd",
        "beef",
        "ffff",
    ]


def test_registry_logs_constructor_failure_without_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SENSITIVE-CONSTRUCTOR-PAYLOAD"

    class BrokenProvider:
        def __init__(self) -> None:
            raise RuntimeError(secret)

    registry = ProviderRegistry()
    registry.register_class("broken", BrokenProvider)  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING)

    registry.instantiate_registered()

    warning = "\n".join(record.getMessage() for record in caplog.records)
    assert "broken" in warning
    assert "RuntimeError" in warning
    assert secret not in warning
