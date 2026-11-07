"""A date-filtered run must not be recorded as if it covered every message.

A run with ``--from-date`` / ``--to-date`` renders part of each session.
What it leaves in the cache has to stay true for the next run without a
filter: the session rows, the project aggregates and the cross-session
sidecar describe the source files, never the window, and a file written
from a window must not be recorded as current. Otherwise the next
unfiltered run trusts the record and the messages outside the window
never reach the output.

The fixture puts each session's first turn on 2026-01-01 and its second
on 2026-01-02; the filtered runs ask for the first day only.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from claude_code_log.cache import CacheManager, SessionSidecar, get_library_version
from claude_code_log.converter import (
    convert_jsonl_to,
    filter_messages_by_date,
    load_transcript,
)
from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.utils import parse_date_bounds

SESSIONS = ["sess-alpha", "sess-beta", "sess-gamma"]
# The first day, written with times so that the two layers' readings of a
# bare date play no part in these tests.
FIRST_DAY = {"from_date": "2026-01-01 00:00:00", "to_date": "2026-01-01 23:59:59"}


def _entry(
    kind: str, sid: str, uid: str, parent: Optional[str], stamp: str, text: str
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "type": kind,
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": stamp,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
    }
    if kind == "user":
        entry["message"] = {"role": "user", "content": text}
    else:
        entry["requestId"] = f"req_{uid}"
        entry["message"] = {
            "id": f"msg_{uid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return entry


def _turn(
    sid: str, n: int, day: int, text: str, parent: Optional[str] = None
) -> List[Dict[str, Any]]:
    """The n-th exchange of ``sid``, on 2026-01-0<day>."""
    if parent is None and n:
        parent = f"{sid}-a{n - 1}"
    stamp = f"2026-01-0{day}T10:{2 * n:02d}:00Z"
    reply = f"2026-01-0{day}T10:{2 * n + 1:02d}:00Z"
    return [
        _entry("user", sid, f"{sid}-u{n}", parent, stamp, text),
        _entry("assistant", sid, f"{sid}-a{n}", f"{sid}-u{n}", reply, f"reply {n}"),
    ]


def _append(path: Path, entries: List[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    # Coarse-mtime filesystems (and Windows) must still see a change.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _second_day(sid: str) -> str:
    return f"SECOND-DAY-{sid}"


def _build_project(root: Path) -> Path:
    project = root / "projects" / "-proj"
    project.mkdir(parents=True)
    for sid in SESSIONS:
        _append(
            project / f"{sid}.jsonl",
            _turn(sid, 0, 1, f"first {sid}") + _turn(sid, 1, 2, _second_day(sid)),
        )
    return project


# The shapes a session file or a combined page can take. Markdown without a
# combined page is the reported one; the paginated shape writes page 1 to
# the same file name a single combined page uses.
SHAPES: Dict[str, Dict[str, Any]] = {
    "md-sessions": {"output_format": "md", "write_combined": False},
    "html-combined": {"output_format": "html"},
    "html-paginated": {"output_format": "html", "page_size": 4},
    "json": {"output_format": "json"},
}


def _run(
    projects_dir: Path,
    shape: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> None:
    """A pass over the whole hierarchy, as ``--all-projects`` runs it."""
    process_projects_hierarchy(
        projects_dir, from_date, to_date, jobs=1, **SHAPES[shape]
    )


def _convert(
    project: Path,
    shape: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> None:
    """One project converted on its own, as ``claude-code-log <dir>`` runs it.

    Unlike the hierarchy pass, which leaves a project with no changed file
    alone, this renders on every run, so a filtered run reaches files
    that were complete.
    """
    options = dict(SHAPES[shape])
    convert_jsonl_to(
        options.pop("output_format"),
        project,
        from_date=from_date,
        to_date=to_date,
        silent=True,
        **options,
    )


def _missing(project: Path, shape: str) -> List[str]:
    """Each second-day marker an output lacks, as ``file: session``.

    Session files are checked one by one; the combined output as the
    union of its pages (one file unless paginated).
    """
    ext = SHAPES[shape]["output_format"]
    missing: List[str] = []
    for sid in SESSIONS:
        path = project / f"session-{sid}.{ext}"
        if _second_day(sid) not in path.read_text(encoding="utf-8"):
            missing.append(f"{path.name}: {sid}")
    if SHAPES[shape].get("write_combined", True):
        pages = sorted(project.glob(f"combined_transcripts*.{ext}"))
        assert pages, "no combined output"
        union = "".join(p.read_text(encoding="utf-8") for p in pages)
        missing += [f"combined: {s}" for s in SESSIONS if _second_day(s) not in union]
    return missing


# The hierarchy pass converts only a project with a changed file, so it
# reaches a filtered run through an append; a project converted on its own
# reaches it with or without one.
SEQUENCES = {
    "all-projects-appended": ("all-projects", True),
    "project-appended": ("project", True),
    "project-unchanged": ("project", False),
}


@pytest.mark.parametrize("sequence", list(SEQUENCES))
@pytest.mark.parametrize("shape", list(SHAPES))
def test_an_unfiltered_run_after_a_filtered_one_renders_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str, sequence: str
) -> None:
    """A file written from a window is not recorded as current.

    The reported sequence: a turn arrives, a filtered run leaves it out,
    and the next run without a filter must render it. Without the append
    the filtered run still overwrites files that were complete.
    """
    entry, append = SEQUENCES[sequence]
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    def run(from_date: Optional[str] = None, to_date: Optional[str] = None) -> None:
        if entry == "all-projects":
            _run(project.parent, shape, from_date, to_date)
        else:
            _convert(project, shape, from_date, to_date)

    run()
    assert _missing(project, shape) == []
    late = f"{_second_day('sess-alpha')}-late"
    if append:
        _append(project / "sess-alpha.jsonl", _turn("sess-alpha", 2, 2, late))

    run(**FIRST_DAY)
    # Control: the filtered run did overwrite the files without day two.
    assert _missing(project, shape) != [], "the window filtered nothing"

    run()
    assert _missing(project, shape) == []
    if append:
        ext = SHAPES[shape]["output_format"]
        text = (project / f"session-sess-alpha.{ext}").read_text(encoding="utf-8")
        assert late in text


def test_a_json_session_rendered_by_the_pool_from_a_window_is_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON file records its window itself, so a render worker must know
    the window too: the same sequence, with the session files rendered in
    the pool."""
    from claude_code_log import render_dispatch
    from claude_code_log import render_pool as render_pool_module
    from claude_code_log.render_pool import RenderPool, RenderUnit

    monkeypatch.setattr(render_dispatch, "_MIN_MESSAGES_FOR_RENDER_POOL", 0)
    monkeypatch.setattr(render_dispatch, "_MIN_ENTRIES_FOR_RENDER_POOL", 0)
    monkeypatch.setattr(
        render_pool_module, "available_memory_bytes", lambda: 64 * 1024**3
    )
    dispatched: List[str] = []
    original_submit = RenderPool.submit

    def counting_submit(self: RenderPool, unit: RenderUnit) -> Any:
        future = original_submit(self, unit)
        if future is not None:
            dispatched.append(unit.kind)
        return future

    monkeypatch.setattr(RenderPool, "submit", counting_submit)
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    def run(from_date: Optional[str] = None, to_date: Optional[str] = None) -> None:
        convert_jsonl_to(
            "json",
            project,
            from_date=from_date,
            to_date=to_date,
            silent=True,
            render_jobs=2,
        )

    run()
    dispatched.clear()
    run(**FIRST_DAY)
    # Controls: the pool rendered the windowed session files, without day two.
    assert "session" in dispatched, "the pool never rendered"
    assert _missing(project, "json") != [], "the window filtered nothing"

    run()
    assert _missing(project, "json") == []


def _cold_build(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> CacheManager:
    """A cache built from scratch, unfiltered, over ``project``'s files."""
    cold = tmp_path / "cold" / "projects" / project.name
    cold.mkdir(parents=True)
    for f in project.glob("*.jsonl"):
        shutil.copy2(f, cold / f.name)
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cold.db"))
        _run(cold.parent, "md-sessions")
        return CacheManager(cold, get_library_version())


def _rows(manager: CacheManager) -> Tuple[int, Dict[str, Tuple[int, str, str]]]:
    data = manager.get_cached_project_data()
    assert data is not None
    return data.total_message_count, {
        sid: (s.message_count, s.first_timestamp, s.last_timestamp)
        for sid, s in data.sessions.items()
    }


@pytest.mark.parametrize("change", ["unchanged", "appended"])
def test_a_filtered_run_leaves_the_session_rows_describing_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Session rows and aggregates count every message, not the window's."""
    project = _build_project(tmp_path)
    projects_dir = project.parent
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(projects_dir, "md-sessions")
    if change == "appended":
        _append(project / "sess-beta.jsonl", _turn("sess-beta", 2, 2, "late beta"))

    # On its own, so that the unchanged project is converted at all.
    _convert(project, "md-sessions", **FIRST_DAY)
    filtered = _rows(CacheManager(project, get_library_version()))

    reference = _rows(_cold_build(tmp_path, project, monkeypatch))
    # Control: the window does exclude rows' worth of messages.
    assert reference[0] > 6
    assert filtered == reference


def _cold_sidecar(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> Optional[SessionSidecar]:
    return _cold_build(tmp_path, project, monkeypatch).load_session_sidecar()


def test_a_session_resumed_during_a_filtered_run_keeps_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar after filtered + plain runs is the one a cold build writes."""
    project = _build_project(tmp_path)
    projects_dir = project.parent
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(projects_dir, "md-sessions")

    resumed = "sess-resumed"
    _append(
        project / f"{resumed}.jsonl",
        _turn(resumed, 0, 2, "resumed", parent=f"{SESSIONS[0]}-a1"),
    )
    _run(projects_dir, "md-sessions", **FIRST_DAY)
    _run(projects_dir, "md-sessions")

    reference = _cold_sidecar(tmp_path, project, monkeypatch)
    assert reference is not None and resumed in reference.parents
    assert CacheManager(project, get_library_version()).load_session_sidecar() == (
        reference
    )


# --- The cache and the renderer read a date bound the same way ---------------


def _stamps(entries: List[Any]) -> List[str]:
    return sorted(e.timestamp for e in entries if getattr(e, "timestamp", None))


@pytest.mark.parametrize(
    "to_date",
    ["2026-01-01", "2026-01-01 10:00", "2026-01-02 10:00"],
    ids=["bare-date", "date-and-time", "next-day-time"],
)
def test_the_cache_and_the_renderer_cut_at_the_same_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, to_date: str
) -> None:
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(project.parent, "md-sessions")
    path = project / "sess-alpha.jsonl"
    manager = CacheManager(project, get_library_version())
    everything = load_transcript(path, silent=True)

    rendered = filter_messages_by_date(everything, None, to_date)
    cached = manager.load_cached_entries_filtered(path, None, to_date)
    assert cached is not None
    assert _stamps(cached) == _stamps(rendered)


def test_a_bare_to_date_includes_that_whole_day(tmp_path: Path) -> None:
    project = _build_project(tmp_path)
    everything = load_transcript(project / "sess-alpha.jsonl", silent=True)

    kept = filter_messages_by_date(everything, None, "2026-01-01")
    assert _stamps(kept) == ["2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z"]


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("2026-01-01", "2026-01-01 00:00:00", "2026-01-01 23:59:59.999999"),
        ("Jan 5 2026", "2026-01-05 00:00:00", "2026-01-05 23:59:59.999999"),
        ("2026-01-01 10:01", "2026-01-01 10:01:00", "2026-01-01 10:01:00"),
        # An explicit midnight is an instant, not the whole day.
        ("2026-01-01 00:00", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
    ],
)
def test_a_bound_naming_a_day_covers_that_day(text: str, start: str, end: str) -> None:
    from_dt, to_dt = parse_date_bounds(text, text)
    assert (str(from_dt), str(to_dt)) == (start, end)


@pytest.mark.parametrize(
    ("text", "days_back"),
    [
        ("today", 0),
        ("Today", 0),
        ("YESTERDAY", 1),
        ("1 day ago", 1),
        ("2 days ago", 2),
        (" 3 Days Ago ", 3),
    ],
)
def test_a_relative_day_covers_that_day(text: str, days_back: int) -> None:
    from_dt, to_dt = parse_date_bounds(text, text)
    assert from_dt is not None and to_dt is not None
    day = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    assert (from_dt, to_dt) == (
        datetime.combine(day, datetime.min.time()),
        datetime.combine(day, datetime.max.time()),
    )


def test_a_bound_naming_an_instant_keeps_its_time() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    from_dt, to_dt = parse_date_bounds("2 hours ago", "now")
    assert from_dt is not None and to_dt is not None
    assert abs(from_dt - (now - timedelta(hours=2))) < timedelta(minutes=1)
    assert abs(to_dt - now) < timedelta(minutes=1)
    yesterday, today = parse_date_bounds("yesterday", "today")
    assert yesterday is not None and today is not None
    assert str(yesterday.time()) == "00:00:00"
    assert str(today.time()) == "23:59:59.999999"


def test_the_collect_fallback_writes_rows_from_every_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pass's index step falls back to loading the project when the cache
    has no data for it, and writes session rows from that load; under a
    window, the load must still be unfiltered.

    With a cache manager the project row always exists by then, so the
    fallback is reached here by having the cache report no project data.
    """
    from claude_code_log import converter

    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(project.parent, "md-sessions")

    written: List[List[str]] = []
    original = converter._update_cache_with_session_data  # pyright: ignore[reportPrivateUsage]

    def spy(cache_manager: CacheManager, messages: List[Any]) -> None:
        written.append(_stamps(messages))
        original(cache_manager, messages)

    monkeypatch.setattr(converter, "_update_cache_with_session_data", spy)
    monkeypatch.setattr(CacheManager, "get_cached_project_data", _no_project_data)
    capsys.readouterr()
    _run(project.parent, "md-sessions", **FIRST_DAY)

    # Control: the fallback ran.
    assert "using fallback processing" in capsys.readouterr().out
    assert written, "no session rows were written"
    second_day = [s for s in _stamps_of_all(project) if s.startswith("2026-01-02")]
    assert second_day
    for stamps in written:
        assert set(second_day) <= set(stamps), "rows written from the window"


def _no_project_data(_self: CacheManager) -> None:
    return None


def _stamps_of_all(project: Path) -> List[str]:
    stamps: List[str] = []
    for path in sorted(project.glob("*.jsonl")):
        stamps += _stamps(load_transcript(path, silent=True))
    return stamps
