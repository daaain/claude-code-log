"""TUI sinks for transcript ids: resume argv, session files, markup.

Ingestion replaces invalid ids (``test_transcript_ids.py``), but a cache
built elsewhere or by an older version can still hand the TUI the original
string, so these tests set ``selected_session_id`` directly, as such a cache
would, and check that each sink refuses or neutralises it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from textual.content import Content
from textual.widgets import DataTable, Static

from claude_code_log.cache import SessionCacheData
from claude_code_log.tui import SessionBrowser

pytestmark = pytest.mark.tui

INJECTED_OPTION = '--settings={"apiKeyHelper":"touch /tmp/ccl-pwn"}'
TRAVERSAL = "../../victim"
CLICK = "[@click=app.quit]PWN"


def _session(session_id: str, cwd: str = "/") -> SessionCacheData:
    return SessionCacheData(
        session_id=session_id,
        summary=None,
        first_timestamp="2025-01-01T00:00:00Z",
        last_timestamp="2025-01-01T00:01:00Z",
        message_count=1,
        first_user_message="hello",
        cwd=cwd,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_creation_tokens=0,
        total_cache_read_tokens=0,
    )


def _victim(tmp_path: Path) -> tuple[Path, Path]:
    """A project dir two levels down and a `.jsonl` outside it."""
    project = tmp_path / "projects" / "proj"
    project.mkdir(parents=True)
    victim = tmp_path / "victim.jsonl"
    victim.write_text("precious\n", encoding="utf-8")
    return project, victim


@pytest.mark.parametrize("session_id", [INJECTED_OPTION, "-r", TRAVERSAL])
def test_resume_refuses_an_id_that_could_be_an_option(
    tmp_path: Path, session_id: str
) -> None:
    app = SessionBrowser(tmp_path)
    app.selected_session_id = session_id
    app.sessions = {session_id: _session(session_id)}
    with (
        patch("claude_code_log.tui.os.execvp") as execvp,
        patch("claude_code_log.tui.os.chdir") as chdir,
        patch.object(app, "suspend") as suspend,
        patch.object(app, "notify"),
    ):
        suspend.return_value.__enter__ = Mock(return_value=None)
        suspend.return_value.__exit__ = Mock(return_value=False)
        app.action_resume_selected()
    execvp.assert_not_called()
    chdir.assert_not_called()


def test_resume_attaches_the_id_to_the_option(tmp_path: Path) -> None:
    app = SessionBrowser(tmp_path)
    app.selected_session_id = "29ccd257-68b1-427f-ae5f-6524b7cb6f20"
    with (
        patch("claude_code_log.tui.os.execvp") as execvp,
        patch.object(app, "suspend") as suspend,
    ):
        suspend.return_value.__enter__ = Mock(return_value=None)
        suspend.return_value.__exit__ = Mock(return_value=False)
        app.action_resume_selected()
    execvp.assert_called_once_with(
        "claude", ["claude", "--resume=29ccd257-68b1-427f-ae5f-6524b7cb6f20"]
    )


def test_archive_does_not_delete_outside_the_project(tmp_path: Path) -> None:
    project, victim = _victim(tmp_path)
    app = SessionBrowser(project)
    app.selected_session_id = TRAVERSAL
    with patch.object(app, "notify"), patch.object(app, "load_sessions"):
        app._on_archive_confirm(True)
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_delete_both_does_not_delete_outside_the_project(tmp_path: Path) -> None:
    project, victim = _victim(tmp_path)
    app = SessionBrowser(project)
    app.selected_session_id = TRAVERSAL
    app.sessions = {TRAVERSAL: _session(TRAVERSAL)}
    with (
        patch.object(app, "notify"),
        patch.object(app, "load_sessions"),
        patch.object(app.cache_manager, "delete_session", create=True),
    ):
        app._on_delete_confirm("both")
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_restore_does_not_write_outside_the_project(tmp_path: Path) -> None:
    project, victim = _victim(tmp_path)
    app = SessionBrowser(project)
    app.selected_session_id = TRAVERSAL
    app.archived_sessions = {TRAVERSAL: _session(TRAVERSAL)}
    with (
        patch.object(app, "notify"),
        patch.object(app, "load_sessions"),
        patch.object(
            app.cache_manager,
            "export_session_to_jsonl",
            return_value=['{"attacker": "content"}'],
        ),
    ):
        app.action_restore_jsonl()
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_restore_project_skips_ids_outside_the_project(tmp_path: Path) -> None:
    project, victim = _victim(tmp_path)
    victim.unlink()
    selector_cache = Mock()
    selector_cache.get_cached_project_data.return_value = Mock(
        sessions={TRAVERSAL: _session(TRAVERSAL)}
    )
    selector_cache.export_session_to_jsonl.return_value = ['{"attacker": 1}']
    from claude_code_log.tui import ProjectSelector

    app = ProjectSelector([project], [], archived_projects={project})
    app.selected_project_path = project
    with (
        patch("claude_code_log.tui.CacheManager", return_value=selector_cache),
        patch.object(app, "notify"),
        patch.object(app, "populate_table", create=True),
    ):
        app._handle_restore_project_confirm(True)
    assert not victim.exists()


# Textual's markup parser accepts tags Rich's escaper leaves alone (a leading
# space, `$variables`), and malformed markup raises; each must show as text.
MARKUP_PAYLOADS = [
    CLICK,
    "[ @click=app.quit]PWN[/]",
    "[$primary]PWN[/]",
    "/tmp/$a([/[a\\",
]


def _no_actions(content: Content, payload: str) -> None:
    assert not [s for s in content.spans if "click" in str(s.style)], content.spans
    assert payload in content.plain


def _expanded_content(app: SessionBrowser) -> Content:
    captured: dict[str, Any] = {}
    widget = Mock(spec=Static)
    widget.styles = Mock(display="none")
    widget.update.side_effect = lambda content: captured.setdefault("c", content)

    def query_one(selector: str, *_: Any) -> Any:
        return widget if selector == "#expanded-content" else Mock(spec=DataTable)

    with patch.object(app, "query_one", side_effect=query_one):
        app.action_toggle_expanded()
    content = captured["c"]
    return content if isinstance(content, Content) else Content.from_markup(content)


@pytest.mark.parametrize("payload", MARKUP_PAYLOADS)
def test_expanded_details_render_transcript_markup_as_text(
    tmp_path: Path, payload: str
) -> None:
    app = SessionBrowser(tmp_path)
    app.selected_session_id = payload
    session = _session(payload, cwd=f"/home/x/{payload}")
    session.first_user_message = payload
    session.summary = payload
    session.ai_title = payload
    app.sessions = {payload: session}
    content = _expanded_content(app)
    _no_actions(content, payload)
    assert content.plain.count(payload) == 5  # id, title, summary, message, cwd


@pytest.mark.parametrize("payload", MARKUP_PAYLOADS)
def test_missing_cwd_warning_is_not_markup(tmp_path: Path, payload: str) -> None:
    app = SessionBrowser(tmp_path)
    sid = "29ccd257-68b1-427f-ae5f-6524b7cb6f20"
    app.selected_session_id = sid
    app.sessions = {sid: _session(sid, cwd=f"/nonexistent/{payload}")}
    with (
        patch("claude_code_log.tui.os.execvp"),
        patch.object(app, "suspend") as suspend,
        patch.object(app, "notify") as notify,
    ):
        suspend.return_value.__enter__ = Mock(return_value=None)
        suspend.return_value.__exit__ = Mock(return_value=False)
        app.action_resume_selected()
    call = notify.call_args_list[0]
    assert payload in call.args[0]
    assert call.kwargs.get("markup") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", MARKUP_PAYLOADS)
async def test_confirm_dialogs_render_names_as_text(payload: str) -> None:
    from textual.app import App

    from claude_code_log.tui import (
        ArchiveConfirmScreen,
        ArchiveProjectConfirmScreen,
        DeleteConfirmScreen,
        DeleteProjectConfirmScreen,
        RestoreProjectConfirmScreen,
    )

    screens = [
        ArchiveConfirmScreen(payload),
        DeleteConfirmScreen(payload),
        ArchiveProjectConfirmScreen(payload, 1),
        DeleteProjectConfirmScreen(payload, 1),
        RestoreProjectConfirmScreen(payload, 1),
    ]
    app: App[None] = App()
    async with app.run_test() as pilot:
        for screen in screens:
            app.push_screen(screen)
            await pilot.pause()
            visuals = [w.visual for w in screen.query(Static)]
            shown = "".join(str(v) for v in visuals)
            assert payload[:8] in shown
            for visual in visuals:
                assert isinstance(visual, Content)
                assert not [s for s in visual.spans if "click" in str(s.style)]
            app.pop_screen()
            await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", MARKUP_PAYLOADS)
async def test_stats_bar_renders_project_name_as_text(
    tmp_path: Path, payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / "proj"
    project.mkdir()
    sid = "29ccd257-68b1-427f-ae5f-6524b7cb6f20"
    (project / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": f"/home/x/{payload}",
                "sessionId": sid,
                "version": "1",
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    app = SessionBrowser(project)
    with patch("claude_code_log.tui.get_project_display_name", return_value=payload):
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.update_stats()
            await pilot.pause()
            from textual.widgets import Label

            visual = app.query_one("#stats", Label).visual
            assert isinstance(visual, Content)
            _no_actions(visual, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [p for p in MARKUP_PAYLOADS if "/" not in p])
async def test_project_rows_render_names_as_text(
    tmp_path: Path, payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DataTable cells are Rich markup, where Rich's own escaper is correct.
    from textual.widgets._data_table import default_cell_formatter

    from claude_code_log.tui import ProjectSelector

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / payload
    project.mkdir()
    app = ProjectSelector([project], [])
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        cell = app.query_one(DataTable).get_row_at(0)[0]
    text = default_cell_formatter(cell)
    assert payload in text.plain  # type: ignore[union-attr]
    assert not [s for s in text.spans if "click" in str(s.style)]  # type: ignore[union-attr]


def _write_hostile_project(project: Path, ids: list[str]) -> None:
    import json

    project.mkdir(parents=True)
    for n, bad in enumerate(ids):
        (project / f"file{n}.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"u{n}",
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "external",
                    "cwd": "/home/x/p",
                    "sessionId": bad,
                    "version": "1",
                    "timestamp": f"2025-01-0{n + 1}T00:00:00Z",
                    "message": {"role": "user", "content": f"message {n}"},
                }
            )
            + "\n",
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_quarantined_sessions_are_distinct_current_and_not_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_log.transcript_ids import quarantined_id

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    bad = ["../../a", "--settings={}", "[ @click=app.quit]x"]
    project = tmp_path / "proj"
    _write_hostile_project(project, bad)
    expected = {quarantined_id(b) for b in bad}

    app = SessionBrowser(project)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause(0.5)
        # Current, not archived: their source files exist.
        assert set(app.sessions) == expected
        assert app.archived_sessions == {}

        # Each row selects its own session, though every id shares a prefix.
        table = app.query_one("#sessions-table", DataTable)
        seen: list[str] = []
        for row in range(table.row_count):
            table.move_cursor(row=row)
            await pilot.pause()
            app._update_selected_session_from_cursor()
            assert app.selected_session_id is not None
            seen.append(app.selected_session_id)
        assert sorted(seen) == sorted(expected)
        shown = {str(table.get_row_at(r)[0]) for r in range(table.row_count)}
        assert len(shown) == 3 and not any(s.startswith("quaranti") for s in shown)

        # Neither resumed, archived nor restored.
        with (
            patch("claude_code_log.tui.os.execvp") as execvp,
            patch.object(app, "suspend"),
            patch.object(app, "push_screen") as push_screen,
            patch.object(
                app.cache_manager, "export_session_to_jsonl", return_value=["{}"]
            ) as export,
        ):
            app.action_resume_selected()
            app.action_archive_session()
            app.archived_sessions = dict(app.sessions)
            app.action_restore_jsonl()
        execvp.assert_not_called()
        push_screen.assert_not_called()
        export.assert_not_called()
    assert sorted(p.name for p in project.glob("*.jsonl")) == [
        "file0.jsonl",
        "file1.jsonl",
        "file2.jsonl",
    ]


@pytest.mark.parametrize("fmt", ["html", "md"])
def test_session_file_for_an_invalid_id_is_not_written(
    tmp_path: Path, fmt: str
) -> None:
    project, _ = _victim(tmp_path)
    app = SessionBrowser(project)
    with patch("claude_code_log.tui.get_renderer") as get_renderer:
        assert app._ensure_session_file(TRAVERSAL, fmt) is None
    get_renderer.assert_not_called()
    assert not list(tmp_path.rglob(f"*.{fmt}"))


@pytest.mark.asyncio
async def test_markdown_viewer_toc_renders_headings_as_text() -> None:
    # Heading text comes from the transcript; Tree labels given as str are
    # parsed as Rich markup, where `[@click=app.…]` runs an app action.
    from typing import cast

    from textual.app import App
    from textual.widgets import Tree

    from claude_code_log.tui import MarkdownViewerScreen

    md = f"# Title\n\n## 🤷 User: *{CLICK} of the task*\n\nbody\n"
    app: App[None] = App()
    async with app.run_test(size=(160, 40)) as pilot:
        screen = MarkdownViewerScreen(md, title="t")
        app.push_screen(screen)
        await pilot.pause(0.5)
        screen._customize_toc_tree()
        await pilot.pause()
        tree = cast(
            Tree[Any], screen.query_one("MarkdownTableOfContents").query_one(Tree)
        )
        labels = []
        pending = [tree.root]
        while pending:
            node = pending.pop()
            labels.append(node.label)
            pending.extend(node.children)
        # Shown literally: parsed as markup, the tag would vanish from the
        # text and leave a clickable action behind.
        assert any(CLICK in label.plain for label in labels), [
            label.plain for label in labels
        ]


@pytest.mark.asyncio
async def test_session_table_populates_with_markup_in_a_cached_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / "proj"
    project.mkdir()
    app = SessionBrowser(project)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        bad = "[/x]abcd"
        app.sessions = {bad: _session(bad)}
        app.populate_table()  # raised MarkupError before
        await pilot.pause()
        table = app.query_one("#sessions-table", DataTable)
        assert table.row_count == 1


@pytest.mark.parametrize("fmt", ["html", "md"])
def test_a_shipped_session_file_with_a_stamp_is_regenerated(
    tmp_path: Path, fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An archive can carry session-<id>.html already stamped with this
    # version; opening it as-is would skip every gate the renderer applies.
    from claude_code_log.cache import get_library_version

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / "proj"
    project.mkdir()
    sid = "29ccd257-68b1-427f-ae5f-6524b7cb6f20"
    shipped = project / f"session-{sid}.{fmt}"
    shipped.write_text(
        f"<!-- Generated by claude-code-log v{get_library_version()} -->\n"
        "<img src=x onerror=alert(1)>\n",
        encoding="utf-8",
    )
    app = SessionBrowser(project)
    with patch(
        "claude_code_log.tui.load_directory_transcripts", return_value=([], None)
    ) as load:
        app._ensure_session_file(sid, fmt)
    load.assert_called_once()  # regenerated rather than trusted
