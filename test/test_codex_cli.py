"""Focused tests for provider-backed single-session CLI exports."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.providers.base import (
    SessionInfo,
    make_assistant_entry,
    make_user_entry,
)


class FakeProvider:
    def __init__(self, session_ids: list[str], available: bool = True):
        self.session_ids = session_ids
        self.available = available
        self.loaded: list[str] = []

    def is_available(self) -> bool:
        return self.available

    def discover_sessions(self):
        for session_id in self.session_ids:
            yield SessionInfo(
                provider="codex", session_id=session_id, title="Codex test"
            )

    def load_session(self, session_id: str, max_messages: int | None = None):
        self.loaded.append(session_id)
        user = make_user_entry(
            session_id, "user-1", "2026-01-01T00:00:00Z", "hello provider"
        )
        assistant = make_assistant_entry(
            session_id,
            "assistant-1",
            "2026-01-01T00:00:01Z",
            "codex-test",
            "provider reply",
        )
        assistant.parentUuid = user.uuid
        return iter([user, assistant])


class FakeRegistry:
    def __init__(self, provider: FakeProvider | None):
        self.provider = provider

    def get_provider(self, name: str):
        return self.provider if name == "codex" else None


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider(["01234567-89ab-cdef"])
    monkeypatch.setattr(
        "claude_code_log.providers.discover_providers",
        lambda: FakeRegistry(fake),
    )
    return fake


def test_provider_exact_session_writes_requested_output(
    provider: FakeProvider, tmp_path: Path
) -> None:
    output = tmp_path / "codex.html"
    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--session-id",
            "01234567-89ab-cdef",
            "-o",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert provider.loaded == ["01234567-89ab-cdef"]
    assert output.exists()
    assert "hello provider" in output.read_text()
    assert "provider reply" in output.read_text()


def test_provider_unique_prefix_and_output_suffix_inference(
    provider: FakeProvider, tmp_path: Path
) -> None:
    output = tmp_path / "codex.md"
    result = CliRunner().invoke(
        main,
        ["--provider", "codex", "--session-id", "0123", "-o", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert provider.loaded == ["01234567-89ab-cdef"]
    assert "# Codex test" in output.read_text()


def test_provider_directory_output_creates_nested_destination(
    provider: FakeProvider, tmp_path: Path
) -> None:
    output_root = tmp_path / "nested" / "exports"
    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--session-id",
            "0123",
            "-o",
            str(output_root),
            "-f",
            "md",
        ],
    )

    destination = output_root / "session-01234567-89ab-cdef.md"
    assert result.exit_code == 0, result.output
    assert destination.exists()
    assert "hello provider" in destination.read_text()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--provider", "codex"], "requires --session-id"),
        (
            ["some-project", "--provider", "codex", "--session-id", "0123"],
            "INPUT_PATH",
        ),
        (
            ["--provider", "codex", "--session-id", "0123", "--all-projects"],
            "--all-projects",
        ),
        (
            ["--provider", "codex", "--session-id", "0123", "--combined", "no"],
            "--combined",
        ),
        (
            ["--provider", "codex", "--session-id", "0123", "--clear-cache"],
            "--clear-cache",
        ),
    ],
)
def test_provider_rejects_unsupported_combinations(
    provider: FakeProvider, args: list[str], message: str
) -> None:
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert message in result.output
    assert provider.loaded == []


def test_provider_prefix_errors_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(["abcd-1111", "abcd-2222"])
    monkeypatch.setattr(
        "claude_code_log.providers.discover_providers",
        lambda: FakeRegistry(fake),
    )
    runner = CliRunner()

    ambiguous = runner.invoke(main, ["--provider", "codex", "--session-id", "abcd"])
    missing = runner.invoke(main, ["--provider", "codex", "--session-id", "missing"])

    assert ambiguous.exit_code != 0
    assert "Ambiguous session ID prefix" in ambiguous.output
    assert missing.exit_code != 0
    assert "not found for provider codex" in missing.output


def test_unknown_and_unavailable_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(
        "claude_code_log.providers.discover_providers",
        lambda: FakeRegistry(None),
    )
    unknown = runner.invoke(main, ["--provider", "unknown", "--session-id", "session"])
    assert unknown.exit_code != 0
    assert "Unknown provider: unknown" in unknown.output

    monkeypatch.setattr(
        "claude_code_log.providers.discover_providers",
        lambda: FakeRegistry(FakeProvider(["session"], available=False)),
    )
    unavailable = runner.invoke(
        main, ["--provider", "codex", "--session-id", "session"]
    )
    assert unavailable.exit_code != 0
    assert "Provider codex is not available" in unavailable.output


def test_provider_stdout_contains_only_document(
    provider: FakeProvider,
) -> None:
    result = CliRunner().invoke(
        main,
        ["--provider", "codex", "--session-id", "0123", "-o", "-", "-f", "md"],
    )

    assert result.exit_code == 0, result.output
    assert "hello provider" in result.stdout
    assert "Successfully converted" not in result.stdout
    assert "Successfully converted codex:01234567-89ab-cdef to stdout" in result.stderr
