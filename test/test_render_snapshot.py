# pyright: reportPrivateUsage=false
"""A run renders what its cache refresh committed.

A run refreshes the cache, then renders. A session file can grow in
between: the session is still being written. If the render then parses
the grown file and writes its entry rows itself, it also stamps the
file's new size and mtime, but the session row keeps the counts the
refresh computed. The run's pages then show lines its session row does
not count, and the cache calls the file current. Only the pending-refresh
flag the entry write leaves set makes the next run refresh it: without
it, the session row would stay behind until the file is written again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Tuple

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, get_library_version

from test.test_date_filtered_runs import (
    SESSIONS,
    SHAPES,
    _append,
    _build_project,
    _convert,
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


def _grow_after_the_refresh(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[Path]:
    """Append a turn to ``path`` once, right after the run's cache refresh."""
    refresh = converter.ensure_fresh_cache_detailed
    grown: list[Path] = []

    def refresh_then_grow(*args: Any, **kwargs: Any) -> Any:
        result = refresh(*args, **kwargs)
        if not grown:
            _append(path, _turn(path.stem, 3, 3, "LATE-TURN"))
            grown.append(path)
        return result

    monkeypatch.setattr(converter, "ensure_fresh_cache_detailed", refresh_then_grow)
    return grown


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("shape", ["html-combined", "md-sessions", "html-paginated"])
def test_a_file_grown_between_refresh_and_render_is_refreshed_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, warm: bool
) -> None:
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    jsonl = project / f"{GROWN}.jsonl"
    if warm:
        # The daily shape: a converted project whose session moved on, so
        # the refresh is the incremental one over a populated cache.
        _convert(project, shape)
        _append(jsonl, _turn(GROWN, 2, 2, "WARM-TURN"))

    with monkeypatch.context() as m:
        grown = _grow_after_the_refresh(m, jsonl)
        _convert(project, shape)
    assert grown == [jsonl], "the file never grew between refresh and render"

    # The run's outputs agree: its session page shows the late turn exactly
    # when its session row counts it.
    ext = SHAPES[shape]["output_format"]
    page = (project / f"session-{GROWN}.{ext}").read_text(encoding="utf-8")
    assert ("LATE-TURN" in page) == (_session_row(project, GROWN)[1] == LATE_STAMP), (
        f"the page {'shows' if 'LATE-TURN' in page else 'lacks'} the late turn,"
        f" the session row says {_session_row(project, GROWN)}"
    )

    # Nothing changes now: this run must still bring the row up to the file.
    _convert(project, shape)

    # Control: a cache built cold from the same files.
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "control.db"))
    _convert(project, shape)
    expected = _session_row(project, GROWN)

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    assert _session_row(project, GROWN) == expected
