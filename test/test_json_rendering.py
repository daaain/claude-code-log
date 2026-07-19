"""Tests for JSON output format (claude_code_log.json.renderer)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Generator

import pytest
from click.testing import CliRunner

from claude_code_log.cache import get_library_version
from claude_code_log.cli import _clear_output_files, main
from claude_code_log.converter import load_transcript
from claude_code_log.json.renderer import JsonRenderer
from claude_code_log.models import RenderingDepth
from claude_code_log.renderer import get_renderer


# ---------- shared CLI fixtures (mirrored from test_cli.py) -------------------
# Redefined locally rather than moved into conftest.py to keep the PR's test
# surface contained to this file.


class _ProjectsSetup:
    def __init__(self, projects_dir: Path, db_path: Path):
        self.projects_dir = projects_dir
        self.db_path = db_path


@pytest.fixture
def cli_projects_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[_ProjectsSetup, None, None]:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    isolated_db = tmp_path / "test-cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(isolated_db))
    yield _ProjectsSetup(projects_dir, isolated_db)


@pytest.fixture
def sample_jsonl_content() -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2023-01-01T10:00:00Z",
            "sessionId": "session-1",
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "user",
            "cwd": "/test",
            "message": {"role": "user", "content": "Héllo, 世界 🌍"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2023-01-01T10:01:00Z",
            "sessionId": "session-1",
            "version": "1.0.0",
            "parentUuid": "user-1",
            "isSidechain": False,
            "userType": "assistant",
            "cwd": "/test",
            "requestId": "req-1",
            "message": {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-3",
                "content": [{"type": "text", "text": "Hi there!"}],
                "usage": {"input_tokens": 10, "output_tokens": 15},
            },
        },
        {"type": "summary", "summary": "A greeting", "leafUuid": "assistant-1"},
    ]


def _create_project_with_jsonl(
    projects_dir: Path, name: str, jsonl_data: list[dict]
) -> Path:
    project_dir = projects_dir / name
    project_dir.mkdir(exist_ok=True)
    jsonl_file = project_dir / "session-1.jsonl"
    with open(jsonl_file, "w") as f:
        for entry in jsonl_data:
            f.write(json.dumps(entry) + "\n")
    return project_dir


def _walk(nodes: list[dict]):
    """Yield every node in a JSON message tree (roots + descendants)."""
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


class TestJsonGenerate:
    """JsonRenderer.generate produces a structured, JSON-serialisable payload."""

    def test_top_level_shape(self, test_data_dir: Path):
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        out = JsonRenderer().generate(messages, "Test")
        data = json.loads(out)

        assert data["version"] == get_library_version()
        assert data["title"] == "Test"
        assert data["depth"] == RenderingDepth.HOOK.value
        assert data["compact"] is False
        assert isinstance(data["sessions"], list)
        assert isinstance(data["messages"], list)

    def test_default_title_when_none(self, test_data_dir: Path):
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        data = json.loads(JsonRenderer().generate(messages))
        assert data["title"] == "Claude Transcript"

    def test_tree_preserved_as_children(self, test_data_dir: Path):
        """Root messages keep their children; paired messages expose pair metadata."""
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        data = json.loads(JsonRenderer().generate(messages, "Test"))

        # Session-header root carries its session's messages as children.
        roots = data["messages"]
        assert roots, "expected at least one root message"
        assert any(r["type"] == "session_header" for r in roots)

        # Walking the tree surfaces every known message category.
        types = {node["type"] for node in _walk(roots)}
        assert {"user", "assistant", "tool_use", "tool_result"} <= types

        # Tool-use/tool-result pairs expose pair_first/pair_last back-references.
        for node in _walk(roots):
            if node["type"] == "tool_use":
                # A paired tool_use has pair_last set; tool_result has pair_first.
                assert ("pair_first" in node) or ("pair_last" in node)
                break
        else:  # pragma: no cover - defensive
            pytest.fail("no tool_use node found in tree")

    def test_pydantic_tool_input_is_dumped(self, test_data_dir: Path):
        """Tool inputs embed Pydantic models; they must serialise to dicts, not reprs."""
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        data = json.loads(JsonRenderer().generate(messages, "Test"))

        tool_use_nodes = [n for n in _walk(data["messages"]) if n["type"] == "tool_use"]
        assert tool_use_nodes, "expected at least one tool_use"

        # Find an Edit tool — its input is a Pydantic model with file_path/old_string/new_string.
        edit_nodes = [
            n for n in tool_use_nodes if n["content"].get("tool_name") == "Edit"
        ]
        assert edit_nodes, "representative_messages.jsonl should contain an Edit tool"
        inp = edit_nodes[0]["content"]["input"]
        assert isinstance(inp, dict), "Pydantic input must not be stringified via repr"
        assert {"file_path", "old_string", "new_string"} <= set(inp.keys())

    def test_content_meta_removed(self, test_data_dir: Path):
        """Meta is surfaced at the node level; the nested copy is dropped for clarity."""
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        data = json.loads(JsonRenderer().generate(messages, "Test"))
        for node in _walk(data["messages"]):
            assert "meta" not in node["content"]
            # message_index is exposed as `index` at the node level.
            assert "message_index" not in node["content"]
            assert isinstance(node["index"], int)

    def test_triple_emits_pair_middle(self, tmp_path: Path):
        """Slash-command triples (UserSlash → Slash → CommandOutput) must
        emit `pair_middle` so downstream tools can reconstruct the full
        triple. Forgetting this field broke the symmetry of the pair_first /
        pair_middle / pair_last fields after PR #127's data-model lift."""
        lines = [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-04-17T01:13:55Z",
                "sessionId": "s1",
                "version": "1",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "user",
                "cwd": "/x",
                "isMeta": True,
                "message": {"role": "user", "content": "Caveat: ..."},
            },
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "2026-04-17T01:13:55Z",
                "sessionId": "s1",
                "version": "1",
                "parentUuid": "u1",
                "isSidechain": False,
                "userType": "user",
                "cwd": "/x",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>exit</command-name>"
                        "<command-message>exit</command-message>"
                        "<command-args></command-args>"
                    ),
                },
            },
            {
                "type": "user",
                "uuid": "u3",
                "timestamp": "2026-04-17T01:13:55Z",
                "sessionId": "s1",
                "version": "1",
                "parentUuid": "u2",
                "isSidechain": False,
                "userType": "user",
                "cwd": "/x",
                "message": {
                    "role": "user",
                    "content": "<local-command-stdout>See ya!</local-command-stdout>",
                },
            },
        ]
        fn = tmp_path / "t.jsonl"
        fn.write_text("\n".join(json.dumps(line) for line in lines))
        messages = load_transcript(fn)
        data = json.loads(JsonRenderer().generate(messages, "Test"))

        # Locate the three pair members by message_type.
        nodes_by_type: dict[str, dict] = {}
        for node in _walk(data["messages"]):
            t = node["type"]
            if t in ("user", "slash_command", "command_output"):
                # User wraps the UserSlash caveat (pair_first); SlashCommand and
                # CommandOutput are distinct types in the rendered tree.
                nodes_by_type.setdefault(t, node)

        # The pair_first (UserSlash caveat) carries pair_middle + pair_last.
        first = next(n for n in _walk(data["messages"]) if "pair_middle" in n)
        assert first["pair_middle"] != first.get("pair_last")
        # Middle (Slash) carries pair_first + pair_last (no pair_middle on itself).
        middle = next(
            n for n in _walk(data["messages"]) if "pair_first" in n and "pair_last" in n
        )
        assert middle["pair_first"] == first["index"]
        # Last (CommandOutput) carries only pair_first.
        last = next(
            n
            for n in _walk(data["messages"])
            if "pair_first" in n and "pair_last" not in n
        )
        assert last["pair_first"] == first["index"]

    def test_detail_minimal_filters_tool_messages(self, test_data_dir: Path):
        """--detail minimal should strip tool_use/tool_result nodes."""
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        renderer = get_renderer("json", depth=RenderingDepth.ASSISTANT)
        out = renderer.generate(messages, "Test")
        assert out is not None
        data = json.loads(out)
        types = {node["type"] for node in _walk(data["messages"])}
        assert "tool_use" not in types
        assert "tool_result" not in types
        assert {"user", "assistant"} <= types

    def test_output_is_valid_utf8_without_escapes(
        self, tmp_path: Path, sample_jsonl_content: list[dict]
    ):
        """ensure_ascii=False: non-ASCII passes through as UTF-8, not \\uXXXX."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for entry in sample_jsonl_content:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        messages = load_transcript(jsonl_file)
        out = JsonRenderer().generate(messages, "Test")
        # Literal non-ASCII must appear — not \u-escaped.
        assert "Héllo, 世界 🌍" in out
        assert "\\u4e16\\u754c" not in out


class TestJsonGenerateSession:
    """JsonRenderer.generate_session filters to the requested session."""

    def test_only_messages_for_session(
        self, test_data_dir: Path, tmp_path: Path
    ) -> None:
        import shutil

        # Combine two sessions, then ask for just one.
        shutil.copy(
            test_data_dir / "representative_messages.jsonl", tmp_path / "a.jsonl"
        )
        shutil.copy(test_data_dir / "session_b.jsonl", tmp_path / "b.jsonl")

        # Load both sessions into a single message list (mirrors how convert_jsonl_to does it).
        messages = []
        for f in (tmp_path / "a.jsonl", tmp_path / "b.jsonl"):
            messages.extend(load_transcript(f))

        renderer = JsonRenderer()
        out = renderer.generate_session(messages, "test_session", title="A")
        data = json.loads(out)

        # Every node in the tree must belong to the requested session
        # (or its synthetic "{sid}#agent-…" subagent variant).
        for node in _walk(data["messages"]):
            sid = node["session_id"]
            assert sid == "test_session" or sid.startswith("test_session#agent-")

    def test_combined_link_when_cache_present(
        self,
        isolated_cache_manager,
        test_data_dir: Path,
    ) -> None:
        """With a cache_manager, a combined_transcript_link is embedded."""
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        out = JsonRenderer().generate_session(
            messages,
            "test_session",
            title="A",
            cache_manager=isolated_cache_manager,
        )
        data = json.loads(out)
        assert data["combined_transcript_link"].endswith(".json")

    def test_no_combined_link_without_cache(self, test_data_dir: Path) -> None:
        messages = load_transcript(test_data_dir / "representative_messages.jsonl")
        out = JsonRenderer().generate_session(messages, "test_session", title="A")
        data = json.loads(out)
        assert "combined_transcript_link" not in data


class TestJsonProjectsIndex:
    """JsonRenderer.generate_projects_index aggregates project summaries."""

    def test_shape_and_totals(self) -> None:
        summaries = [
            {
                "name": "alpha",
                "path": Path("/tmp/alpha"),
                "jsonl_count": 2,
                "message_count": 42,
                "total_input_tokens": 100,
                "total_output_tokens": 200,
                "total_cache_creation_tokens": 10,
                "total_cache_read_tokens": 20,
                "earliest_timestamp": "2025-01-01T00:00:00Z",
                "latest_timestamp": "2025-01-02T00:00:00Z",
                "working_directories": ["/work/alpha"],
                "is_archived": False,
                "sessions": [
                    {"id": "s1", "message_count": 10},
                    {"id": "s2", "message_count": 32},
                ],
            },
            {
                "name": "beta",
                "path": Path("/tmp/beta"),
                "jsonl_count": 1,
                "message_count": 8,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
                "earliest_timestamp": "",
                "latest_timestamp": "",
                "working_directories": [],
                "is_archived": True,
                "sessions": [{"id": "s3", "message_count": 8}],
            },
        ]

        out = JsonRenderer().generate_projects_index(
            summaries, from_date="yesterday", to_date="today"
        )
        data = json.loads(out)

        assert data["version"] == get_library_version()
        assert data["total_projects"] == 2
        assert data["total_sessions"] == 3
        assert data["total_messages"] == 42 + 8
        assert data["date_range"] == {"from": "yesterday", "to": "today"}
        assert len(data["projects"]) == 2
        # Path serialised as str — match the platform's own representation
        # so the test passes on Windows (where Path("/tmp/alpha") becomes
        # `\tmp\alpha`) as well as POSIX.
        assert data["projects"][0]["path"] == str(Path("/tmp/alpha"))
        assert data["projects"][1]["is_archived"] is True

    def test_empty_list(self) -> None:
        data = json.loads(JsonRenderer().generate_projects_index([]))
        assert data["total_projects"] == 0
        assert data["total_sessions"] == 0
        assert data["total_messages"] == 0
        assert data["projects"] == []


class TestJsonIsOutdated:
    """JsonRenderer.is_outdated handles missing, current, stale, malformed files."""

    def test_missing_file(self, tmp_path: Path) -> None:
        assert JsonRenderer().is_outdated(tmp_path / "missing.json") is True

    def test_current_version(self, tmp_path: Path) -> None:
        p = tmp_path / "cur.json"
        p.write_text(json.dumps({"version": get_library_version()}))
        assert JsonRenderer().is_outdated(p) is False

    def test_different_version(self, tmp_path: Path) -> None:
        p = tmp_path / "old.json"
        p.write_text(json.dumps({"version": "0.0.0-stale"}))
        assert JsonRenderer().is_outdated(p) is True

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        assert JsonRenderer().is_outdated(p) is True

    def test_non_dict_payload(self, tmp_path: Path) -> None:
        """A list/scalar payload has no version field; treat as outdated."""
        p = tmp_path / "list.json"
        p.write_text(json.dumps([1, 2, 3]))
        assert JsonRenderer().is_outdated(p) is True


class TestCliJsonFormat:
    """End-to-end CLI coverage for --format json."""

    def test_generates_combined_and_session_files(
        self, cli_projects_setup: _ProjectsSetup, sample_jsonl_content: list[dict]
    ) -> None:
        project_dir = _create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--format", "json"])
        assert result.exit_code == 0, result.output

        combined = project_dir / "combined_transcripts.json"
        assert combined.exists()

        data = json.loads(combined.read_text(encoding="utf-8"))
        assert data["version"] == get_library_version()
        assert isinstance(data["messages"], list)

        # Per-session JSON file is generated when the cache is populated.
        assert list(project_dir.glob("session-*.json"))

    def test_all_projects_index_uses_dedicated_filename(
        self, cli_projects_setup: _ProjectsSetup, sample_jsonl_content: list[dict]
    ) -> None:
        """The top-level projects index must not collide with per-project files.

        HTML/Markdown use index.{ext}, but JSON writes all-projects-summary.json
        so it doesn't clash with a project directory that happens to be named
        "index" or with unrelated user JSON.
        """
        projects_dir = cli_projects_setup.projects_dir
        _create_project_with_jsonl(projects_dir, "project-a", sample_jsonl_content)
        _create_project_with_jsonl(projects_dir, "project-b", sample_jsonl_content)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--projects-dir", str(projects_dir), "--format", "json"],
        )
        assert result.exit_code == 0, result.output

        assert not (projects_dir / "index.json").exists()
        summary = projects_dir / "all-projects-summary.json"
        assert summary.exists()

        data = json.loads(summary.read_text(encoding="utf-8"))
        assert data["total_projects"] == 2
        assert {p["name"] for p in data["projects"]} == {"project-a", "project-b"}

    def test_clear_output_preserves_unrelated_json(
        self, cli_projects_setup: _ProjectsSetup, sample_jsonl_content: list[dict]
    ) -> None:
        """`--clear-output --format json` must not touch unrelated user `.json` files.

        This is the safety contract behind `_list_generated_outputs` in the PR:
        a project directory may legitimately contain foreign JSON (configs,
        exports, etc.) that a naive `glob("*.json")` sweep would nuke.
        """
        project_dir = _create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )
        foreign = project_dir / "user_config.json"
        foreign.write_text('{"keep": "me"}')

        # Generate JSON outputs, then clear them.
        runner = CliRunner()
        runner.invoke(main, [str(project_dir), "--format", "json"])
        assert (project_dir / "combined_transcripts.json").exists()

        _clear_output_files(project_dir, all_projects=False, output_format="json")

        # Generated JSON gone; foreign JSON intact.
        assert not (project_dir / "combined_transcripts.json").exists()
        assert not list(project_dir.glob("session-*.json"))
        assert foreign.exists()
        assert json.loads(foreign.read_text(encoding="utf-8")) == {"keep": "me"}

    def test_clear_output_removes_top_level_summary(
        self, cli_projects_setup: _ProjectsSetup, sample_jsonl_content: list[dict]
    ) -> None:
        """--clear-output --format json removes all-projects-summary.json."""
        projects_dir = cli_projects_setup.projects_dir
        _create_project_with_jsonl(projects_dir, "project-a", sample_jsonl_content)

        runner = CliRunner()
        result = runner.invoke(
            main, ["--projects-dir", str(projects_dir), "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        assert (projects_dir / "all-projects-summary.json").exists()

        _clear_output_files(projects_dir, all_projects=True, output_format="json")
        assert not (projects_dir / "all-projects-summary.json").exists()
