"""The minimal theme on the project index and the archive search page.

`index.html` and `search.html` are rewritten on every run and carry no
staleness decision, so they never needed the generator stamp the
transcript pages use to follow a theme switch — but they did need the
theme itself, on every path that writes them: the all-projects run,
`serve` (start-up and `--watch` ticks), `watch --all-projects`, and the
provider (Codex) hierarchy. The TUI writes neither page. These tests pin
that plumbing, the helpers behind the minimal rows, and that the classic
pages are untouched (their snapshots are pinned in test_snapshot_html.py).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.converter import (
    process_projects_hierarchy,
    render_provider_wholesale,
)
from claude_code_log.html import minimal_theme
from claude_code_log.html.renderer import (
    check_html_theme,
    generate_archive_search_html,
    generate_html,
    generate_projects_index_html,
)
from claude_code_log.converter import load_transcript
from claude_code_log.renderer import TemplateProject
from claude_code_log.utils import THEME_ENV_VAR

TEST_DATA = Path(__file__).parent / "test_data"
REPRESENTATIVE = TEST_DATA / "representative_messages.jsonl"
CODEX_SESSIONS = TEST_DATA / "codex" / "sessions"

MINIMAL_INDEX = "class='theme-minimal mn-page mn-index'"
MINIMAL_SEARCH = "class='theme-minimal mn-page mn-search'"


def _archive(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    project = projects / "-home-u-testproj"
    project.mkdir(parents=True)
    shutil.copy(REPRESENTATIVE, project / "session.jsonl")
    return projects


def _pages(root: Path) -> tuple[str, str]:
    return (
        (root / "index.html").read_text(encoding="utf-8"),
        (root / "search.html").read_text(encoding="utf-8"),
    )


def _assert_minimal(root: Path) -> None:
    index, search = _pages(root)
    assert MINIMAL_INDEX in index
    assert MINIMAL_SEARCH in search
    assert check_html_theme(root / "index.html") == "minimal"
    assert check_html_theme(root / "search.html") == "minimal"


def _assert_classic(root: Path) -> None:
    index, search = _pages(root)
    assert "theme-minimal" not in index
    assert "theme-minimal" not in search
    assert check_html_theme(root / "index.html") == "classic"
    assert check_html_theme(root / "search.html") == "classic"


# ---------------------------------------------------------------- plumbing


class TestEveryPathCarriesTheTheme:
    def test_all_projects_run_and_back(self, tmp_path: Path) -> None:
        projects = _archive(tmp_path)
        process_projects_hierarchy(projects, silent=True, theme="minimal")
        _assert_minimal(projects)
        # Rewritten on every run: switching back needs no stamp check.
        process_projects_hierarchy(projects, silent=True, theme="classic")
        _assert_classic(projects)
        process_projects_hierarchy(projects, silent=True, theme="default")
        _assert_classic(projects)

    def test_watch_tick_shape(self, tmp_path: Path) -> None:
        """`serve --watch` and `watch --all-projects` ticks run the hierarchy
        with `write_combined=False`; the pages still follow the theme."""
        projects = _archive(tmp_path)
        process_projects_hierarchy(projects, silent=True, theme="classic")
        process_projects_hierarchy(
            projects, silent=True, theme="minimal", write_combined=False
        )
        _assert_minimal(projects)

    def test_cli_all_projects_flag_and_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects = _archive(tmp_path)
        monkeypatch.delenv(THEME_ENV_VAR, raising=False)
        args = ["--projects-dir", str(projects)]
        result = CliRunner().invoke(main, [*args, "--theme", "minimal"])
        assert result.exit_code == 0, result.output
        _assert_minimal(projects)

        monkeypatch.setenv(THEME_ENV_VAR, "classic")
        assert CliRunner().invoke(main, args).exit_code == 0
        _assert_classic(projects)
        monkeypatch.setenv(THEME_ENV_VAR, "minimal")
        assert CliRunner().invoke(main, args).exit_code == 0
        _assert_minimal(projects)

    def test_serve_writes_themed_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The conversion `serve` runs before serving writes both pages in
        the resolved theme (stopped before the server binds a port)."""
        projects = _archive(tmp_path)

        class _NoServer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise SystemExit(0)

        monkeypatch.setattr("claude_code_log.server.ArchiveServer", _NoServer)
        monkeypatch.setenv(THEME_ENV_VAR, "minimal")
        args = ["serve", "--projects-dir", str(projects), "--no-index"]
        assert CliRunner().invoke(main, args).exit_code == 0
        _assert_minimal(projects)
        assert CliRunner().invoke(main, [*args, "--theme", "classic"]).exit_code == 0
        _assert_classic(projects)

    def test_watch_all_projects_writes_themed_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`watch --all-projects` converts once before it starts polling."""
        projects = _archive(tmp_path)

        class _Engine:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.stats = type("S", (), {"conversions": 0, "polls": 0, "errors": 0})

            def prime(self) -> None:
                pass

            def run(self) -> None:
                pass

        monkeypatch.setattr("claude_code_log.watch.WatchEngine", _Engine)
        monkeypatch.delenv(THEME_ENV_VAR, raising=False)
        args = ["watch", "--all-projects", "--projects-dir", str(projects)]
        result = CliRunner().invoke(main, [*args, "--theme", "minimal"])
        assert result.exit_code == 0, result.output
        _assert_minimal(projects)
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        _assert_classic(projects)

    def test_provider_index(self, tmp_path: Path) -> None:
        """The Codex hierarchy writes its own index (and no search page)."""
        out = tmp_path / "ccl"
        render_provider_wholesale(
            "codex", CODEX_SESSIONS, out, theme="minimal", silent=True
        )
        index = (out / "index.html").read_text(encoding="utf-8")
        assert MINIMAL_INDEX in index
        assert check_html_theme(out / "index.html") == "minimal"
        render_provider_wholesale(
            "codex", CODEX_SESSIONS, out, theme="classic", silent=True
        )
        assert "theme-minimal" not in (out / "index.html").read_text(encoding="utf-8")

    def test_markdown_index_ignores_the_theme(self, tmp_path: Path) -> None:
        projects = _archive(tmp_path)
        process_projects_hierarchy(
            projects, silent=True, theme="minimal", output_format="md"
        )
        assert "theme" not in (projects / "index.md").read_text(encoding="utf-8")
        assert not (projects / "search.html").exists()


@pytest.mark.tui
def test_tui_writes_neither_page(tmp_path: Path) -> None:
    """The TUI exports session files only (in its `html_theme`); the index
    and search page are the conversion's, so there is nothing to theme."""
    from claude_code_log.tui import SessionBrowser

    projects = _archive(tmp_path)
    project = projects / "-home-u-testproj"
    app = SessionBrowser(project, html_theme="minimal")
    session_file = app._ensure_session_file("session", "html")
    assert session_file is not None
    assert check_html_theme(session_file) == "minimal"
    assert not (projects / "index.html").exists()
    assert not (projects / "search.html").exists()


# ---------------------------------------------------------------- templates


def _toggle(html: str) -> str:
    match = re.search(r"<div class='mn-seg mn-seg-icons'.*?</div>", html, re.S)
    assert match, "no colour-scheme toggle"
    return match.group(0)


class TestPages:
    def test_classic_pages_carry_nothing_minimal(self) -> None:
        index = generate_projects_index_html([])
        search = generate_archive_search_html()
        for html in (index, search):
            assert "theme-minimal" not in html
            assert "data-mn-theme" not in html
            assert "mn-" not in re.sub(r"<script>.*?</script>", "", html, flags=re.S)
            assert "claude-code-log:theme" not in html

    def test_same_toggle_and_store_as_transcripts(self) -> None:
        """One toggle, one stored choice: the index, the search page and a
        transcript render the same buttons and read the same key."""
        transcript = generate_html(
            load_transcript(REPRESENTATIVE, silent=True), "T", theme="minimal"
        )
        index = generate_projects_index_html([], theme="minimal")
        search = generate_archive_search_html(theme="minimal")
        assert _toggle(index) == _toggle(transcript) == _toggle(search)
        for html in (index, search):
            assert "'claude-code-log:theme'" in html
            # Applied in <head>, before the body paints.
            head = html[: html.index("</head>")]
            assert "localStorage.getItem('claude-code-log:theme')" in head

    def test_index_keeps_the_finder_hooks(self, tmp_path: Path) -> None:
        """The session finder (components/search.html) reads these."""
        projects = _archive(tmp_path)
        process_projects_hierarchy(projects, silent=True, theme="minimal")
        index, _ = _pages(projects)
        for hook in (
            "class='project-list",
            "class='project-card",
            "class='project-name'><a href=",
            "class='project-stats",
            "class='session-link",
            "class='session-preview'",
            "class='session-link-meta'",
            'id="searchInput"',
        ):
            assert hook in index, hook
        assert "href='search.html'" in index
        assert "data-mn-from=" in index and "data-mn-ts=" in index

    def test_search_page_renders_rows_and_stays_self_contained(self) -> None:
        html = generate_archive_search_html(theme="minimal")
        assert "function mnRenderGroups(" in html
        assert "mnRenderGroups(body.results, query, append)" in html
        assert "<script src=" not in html
        assert "<link rel=" not in html
        assert "id='setup'" in html


# ---------------------------------------------------------------- helpers


def _project(**overrides: Any) -> TemplateProject:
    data: dict[str, Any] = {
        "name": "-p",
        "html_file": "-p/combined_transcripts.html",
        "jsonl_count": 3,
        "message_count": 1234,
        "last_modified": 1700000000.0,
        "total_input_tokens": 1000,
        "total_output_tokens": 2500,
        "total_cache_creation_tokens": 500,
        "total_cache_read_tokens": 1_500_000,
        "earliest_timestamp": "2025-01-01T09:00:00Z",
        "latest_timestamp": "2025-01-15T10:00:00Z",
        "sessions": [],
    }
    data.update(overrides)
    return TemplateProject(data)


class TestHelpers:
    def test_date_span(self) -> None:
        assert (
            minimal_theme.date_span("2025-01-01T09:00:00Z", "2025-01-15T10:00:00Z")
            == "2025-01-01 – 2025-01-15"
        )
        assert (
            minimal_theme.date_span("2025-01-01T09:00:00Z", "2025-01-01T23:00:00Z")
            == "2025-01-01"
        )
        assert minimal_theme.date_span("", "") == ""

    def test_when(self) -> None:
        assert minimal_theme.when("", "") is None
        single = minimal_theme.when("2025-01-01T09:00:00Z", "2025-01-01T09:00:00Z")
        assert single == {
            "start": "2025-01-01T09:00:00Z",
            "end": "",
            "text": "2025-01-01",
        }

    def test_project_when_falls_back_to_last_modified(self) -> None:
        project = _project(earliest_timestamp="", latest_timestamp="")
        found = minimal_theme.project_when(project)
        assert found == {
            "start": "2023-11-14T22:13:20Z",
            "end": "",
            "text": "2023-11-14",
        }

    def test_project_meta(self) -> None:
        assert minimal_theme.project_meta(_project()) == [
            "3 files",
            "1,234 msgs",
            "1.5M in",
            "2.5k out",
        ]
        one = _project(sessions=[{"id": "a"}])
        assert minimal_theme.project_meta(one)[0] == "1 session"

    def test_index_sessions_newest_first_with_links(self) -> None:
        project = _project(
            sessions=[
                {
                    "id": "aaaaaaaa-1",
                    "summary": "Old",
                    "first_timestamp": "2025-01-01T09:00:00Z",
                    "last_timestamp": "2025-01-01T09:30:00Z",
                    "message_count": 1,
                    "first_user_message": "hi",
                },
                {
                    "id": "bbbbbbbb-2",
                    "first_timestamp": "2025-02-01T10:05:00Z",
                    "message_count": 4,
                    "first_user_message": "later",
                    "file": "-p/session-bbbbbbbb-2.low.html",
                },
                {"is_fork_point": True, "first_user_message": "not a session"},
            ]
        )
        rows = minimal_theme.index_sessions(project)
        assert [row["short_id"] for row in rows] == ["bbbbbbbb", "aaaaaaaa"]
        newest, oldest = rows
        assert newest["href"] == "-p/session-bbbbbbbb-2.low.html"
        assert oldest["href"] == "-p/session-aaaaaaaa-1.html"
        assert (newest["date"], newest["time"]) == ("2025-02-01", "10:05")
        assert newest["title"] == "" and newest["preview"] == "later"
        assert newest["meta"] == ["bbbbbbbb", "4 msgs"]
        assert oldest["meta"] == ["aaaaaaaa", "1 msg"]
        assert oldest["timestamp_end"] == "2025-01-01T09:30:00Z"
