"""The all-providers command links provider-specific indexes without mixing sources."""

import json
from pathlib import Path

from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.providers.aggregate import write_all_providers_index


def _write_claude_session(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "user",
            "timestamp": "2026-01-01T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": session_id,
            "version": "1.0.0",
            "uuid": f"{session_id}-user",
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:01Z",
            "parentUuid": f"{session_id}-user",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": session_id,
            "version": "1.0.0",
            "uuid": f"{session_id}-assistant",
            "message": {
                "id": f"{session_id}-assistant",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _write_codex_session(path: Path, thread_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": "/workspace"},
        },
        {
            "timestamp": "2026-01-02T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hello from codex"},
        },
        {
            "timestamp": "2026-01-02T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "hi"},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_all_providers_renders_provider_indexes_and_root_index(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    _write_claude_session(
        home / ".claude" / "projects" / "-tmp" / "claude-session.jsonl",
        "claude-session",
    )
    codex_home = tmp_path / "codex-home"
    _write_codex_session(
        codex_home / "sessions" / "2026" / "01" / "rollout-codex-thread.jsonl",
        "codex-thread",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    output = tmp_path / "aggregate"

    result = CliRunner().invoke(main, ["--all-providers", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert (output / "claude" / "index.html").is_file()
    assert (output / "codex" / "index.html").is_file()
    aggregate = (output / "index.html").read_text(encoding="utf-8")
    assert 'href="claude/index.html"' in aggregate
    assert 'href="codex/index.html"' in aggregate
    assert "1 sessions discovered" in aggregate


def test_all_providers_with_none_available_fails_without_creating_output(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    output = tmp_path / "aggregate"

    result = CliRunner().invoke(main, ["--all-providers", "--output", str(output)])

    assert result.exit_code != 0
    assert "No providers available" in result.output
    assert not output.exists()


def test_all_providers_reports_unsupported_provider_and_does_not_write_empty_index(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    transcript = (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / "session-1"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    output = tmp_path / "aggregate"

    result = CliRunner().invoke(main, ["--all-providers", "--output", str(output)])

    assert result.exit_code != 0
    assert "agy: provider does not support whole-provider rendering" in result.output
    assert not (output / "index.html").exists()


def test_all_providers_rejects_single_provider_options(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["--all-providers", "--provider", "codex", "--output", str(tmp_path / "out")],
    )

    assert result.exit_code == 2
    assert "does not support --provider" in result.output


def test_all_providers_rejects_file_output(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _write_codex_session(
        tmp_path / "codex-home" / "sessions" / "rollout-thread.jsonl", "thread"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    output_file = tmp_path / "all.html"

    result = CliRunner().invoke(main, ["--all-providers", "--output", str(output_file)])

    assert result.exit_code == 2
    assert "pass --output as a directory" in result.output
    assert not output_file.exists()


def test_aggregate_index_supports_markdown_and_json(tmp_path: Path) -> None:
    output = tmp_path / "aggregate"
    provider_index = output / "codex" / "index.md"
    provider_index.parent.mkdir(parents=True)
    provider_index.write_text("provider index", encoding="utf-8")
    row = [{"name": "Codex", "index_path": provider_index, "session_count": 3}]

    markdown_path = write_all_providers_index(output, row, [], "markdown")
    assert markdown_path.name == "index.md"
    assert (
        "[Codex](codex/index.md) — 3 sessions discovered" in markdown_path.read_text()
    )

    json_provider_index = output / "codex" / "all-projects-summary.json"
    json_provider_index.touch()
    json_row = [
        {"name": "Codex", "index_path": json_provider_index, "session_count": 3}
    ]
    json_path = write_all_providers_index(output, json_row, [], "json")
    assert json_path.name == "all-projects-summary.json"
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["providers"] == [
        {"name": "Codex", "href": "codex/all-projects-summary.json", "session_count": 3}
    ]
