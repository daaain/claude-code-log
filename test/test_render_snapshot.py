# pyright: reportPrivateUsage=false
"""A run renders what its cache refresh committed.

A run refreshes the cache, then renders. A session file can grow in
between: the session is still being written. If the render then parses
the grown file and writes its entry rows itself, it also stamps the
file's new size and mtime, but the session row keeps the counts the
refresh computed. The run's pages then show lines its session row does
not count, and the cache calls the file current. Only the pending-refresh
flag the entry write left set (migration 015, dropped by 016) made the
next run refresh it: without it, the session row would have stayed
behind until the file was written again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Tuple

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.entry_store import ParsedEntryStore
from claude_code_log.watch import WatchEngine

from test.test_date_filtered_runs import (
    SESSIONS,
    SHAPES,
    _append,
    _build_project,
    _convert,
    _second_day,
    _turn,
)

GROWN = SESSIONS[0]
# The late turn's reply, the last entry of the grown file.
LATE_STAMP = "2026-01-03T10:07:00Z"


def _session_row(project: Path, sid: str) -> Tuple[Any, ...]:
    data = CacheManager(project, get_library_version()).get_cached_project_data()
    assert data is not None
    row = data.sessions[sid]
    return (
        row.message_count,
        row.last_timestamp,
        row.total_input_tokens,
        row.total_output_tokens,
    )


class _GrowAfterTheRefresh:
    """Append a late turn to ``path`` once, right after a run's cache
    refresh, and record every entry-row write that follows it."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        path: Path,
        text: str = "LATE-TURN",
        module: Any = converter,
        refresh_name: str = "ensure_fresh_cache_detailed",
    ) -> None:
        self.path = path
        self.grown = False
        self.writes_after: list[str] = []
        refresh = getattr(module, refresh_name)

        def refresh_then_grow(*args: Any, **kwargs: Any) -> Any:
            result = refresh(*args, **kwargs)
            if not self.grown:
                _append(path, _turn(path.stem, 3, 3, text))
                self.grown = True
            return result

        monkeypatch.setattr(module, refresh_name, refresh_then_grow)
        for name in ("save_cached_entries", "extend_cached_entries"):
            monkeypatch.setattr(CacheManager, name, self._spy(name))

    def _spy(self, name: str) -> Any:
        write = getattr(CacheManager, name)

        def spied(
            manager: CacheManager, jsonl_path: Path, *args: Any, **kwargs: Any
        ) -> Any:
            if self.grown:
                self.writes_after.append(f"{name} {jsonl_path.name}")
            return write(manager, jsonl_path, *args, **kwargs)

        return spied


# The paginated shape once more, with streaming forced, so the page loads
# of the streaming path are the render's loads.
STREAMED = "html-paginated-streamed"


def _shape(shape: str) -> str:
    return "html-paginated" if shape == STREAMED else shape


def _counted(function: Any, calls: list[int]) -> Any:
    """``function``, recording each call that did not decline (None)."""

    def counted(*args: Any, **kwargs: Any) -> Any:
        result = function(*args, **kwargs)
        if result is not None:
            calls.append(1)
        return result

    return counted


def _page(project: Path, shape: str) -> str:
    ext = SHAPES[_shape(shape)]["output_format"]
    return (project / f"session-{GROWN}.{ext}").read_text(encoding="utf-8")


def _cold_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: Path, shape: str
) -> Tuple[Any, ...]:
    """The session row a cache built cold from the files gives."""
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "control.db"))
    _convert(project, _shape(shape))
    row = _session_row(project, GROWN)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    return row


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize(
    "shape", ["html-combined", "md-sessions", "html-paginated", STREAMED]
)
def test_a_file_grown_between_refresh_and_render_is_refreshed_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, warm: bool
) -> None:
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    if shape == STREAMED:
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
    streamed: list[int] = []
    monkeypatch.setattr(
        converter,
        "_stream_paginated_conversion",
        _counted(converter._stream_paginated_conversion, streamed),
    )
    shape_run = _shape(shape)
    jsonl = project / f"{GROWN}.jsonl"
    if warm:
        # The daily shape: a converted project whose session moved on, so
        # the refresh is the incremental one over a populated cache.
        _convert(project, shape_run)
        _append(jsonl, _turn(GROWN, 2, 2, "WARM-TURN"))

    with monkeypatch.context() as m:
        grow = _GrowAfterTheRefresh(m, jsonl)
        _convert(project, shape_run)
    assert grow.grown, "the file never grew between refresh and render"

    # The run's outputs agree: its session page shows the late turn exactly
    # when its session row counts it.
    page = _page(project, shape)
    assert ("LATE-TURN" in page) == (_session_row(project, GROWN)[1] == LATE_STAMP), (
        f"the page {'shows' if 'LATE-TURN' in page else 'lacks'} the late turn,"
        f" the session row says {_session_row(project, GROWN)}"
    )
    # And they agree on the refresh's moment: the render read what the
    # refresh committed, wrote no entry rows, and left the late turn out.
    assert grow.writes_after == []
    assert _second_day(GROWN) in page, "the render lost what the refresh committed"
    if warm:
        assert "WARM-TURN" in page, "the render lost what the refresh committed"
    assert "LATE-TURN" not in page
    if shape == STREAMED:
        assert streamed, "the run did not stream"

    # Nothing changes now: this run must still bring the row up to the file.
    _convert(project, shape_run)
    assert "LATE-TURN" in _page(project, shape)
    assert _session_row(project, GROWN) == _cold_row(
        tmp_path, monkeypatch, project, shape
    )


def test_a_line_landing_mid_render_shows_on_the_next_watch_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watch mode runs a conversion per tick, holding one entry store across
    them. A line appended while a tick renders is left out of that tick's
    pages and is not lost: the engine stamped the files before converting,
    so the append is a change, and the next tick shows it."""
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    jsonl = project / f"{GROWN}.jsonl"
    shape = "html-combined"
    _convert(project, shape)

    store = ParsedEntryStore()
    now = [0.0]

    def convert(_paths: set[Path]) -> None:
        converter.convert_jsonl_to("html", project, silent=True, entry_store=store)

    engine = WatchEngine(
        [project.parent],
        convert,
        quiet_period=1.0,
        max_latency=5.0,
        clock=lambda: now[0],
    )
    engine.prime()

    def tick_after(seconds: float) -> Any:
        now[0] += seconds
        return engine.tick()

    _append(jsonl, _turn(GROWN, 2, 2, "WARM-TURN"))
    assert tick_after(0) is None  # seen, not yet quiet
    with monkeypatch.context() as m:
        grow = _GrowAfterTheRefresh(m, jsonl)
        assert tick_after(1.0) == {jsonl}
    assert grow.grown and grow.writes_after == []
    page = _page(project, shape)
    assert "WARM-TURN" in page
    assert "LATE-TURN" not in page

    assert tick_after(0) is None  # the late turn, seen
    assert tick_after(1.0) == {jsonl}
    assert "LATE-TURN" in _page(project, shape)
    assert engine.stats.conversions == 2
    assert _session_row(project, GROWN) == _cold_row(
        tmp_path, monkeypatch, project, shape
    )


@pytest.mark.tui
def test_a_tui_export_renders_lines_written_since_the_tui_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TUI refreshes the cache before it exports a session, since it
    renders what the cache holds and that may date from its start."""
    from claude_code_log.tui import SessionBrowser

    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _convert(project, "html-combined")
    app = SessionBrowser(project)

    _append(project / f"{GROWN}.jsonl", _turn(GROWN, 2, 3, "SINCE-THE-TUI-STARTED"))
    session_file = app._ensure_session_file(GROWN, "html", force=True)

    assert session_file is not None
    assert "SINCE-THE-TUI-STARTED" in session_file.read_text(encoding="utf-8")
    assert _session_row(project, GROWN) == _cold_row(
        tmp_path, monkeypatch, project, "html-combined"
    )

    # And it renders what that refresh committed, as a run does.
    from claude_code_log import tui

    with monkeypatch.context() as m:
        grow = _GrowAfterTheRefresh(
            m, project / f"{GROWN}.jsonl", module=tui, refresh_name="ensure_fresh_cache"
        )
        app._ensure_session_file(GROWN, "html", force=True)
    assert grow.grown and grow.writes_after == []
    page = session_file.read_text(encoding="utf-8")
    assert "SINCE-THE-TUI-STARTED" in page
    assert "LATE-TURN" not in page


def test_a_single_session_export_renders_what_its_refresh_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _convert(project, "html-combined")
    _append(project / f"{GROWN}.jsonl", _turn(GROWN, 2, 2, "WARM-TURN"))

    with monkeypatch.context() as m:
        grow = _GrowAfterTheRefresh(
            m, project / f"{GROWN}.jsonl", refresh_name="ensure_fresh_cache"
        )
        output = converter.generate_single_session_file("html", project, GROWN)
    assert grow.grown and grow.writes_after == []
    page = output.read_text(encoding="utf-8")
    assert "WARM-TURN" in page
    assert "LATE-TURN" not in page

    output = converter.generate_single_session_file("html", project, GROWN)
    assert "LATE-TURN" in output.read_text(encoding="utf-8")
    assert _session_row(project, GROWN) == _cold_row(
        tmp_path, monkeypatch, project, "html-combined"
    )
