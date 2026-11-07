# pyright: reportPrivateUsage=false
"""A date-filtered pass over every project renders the projects active in
its window, and only those.

The pass used to convert only the projects with a changed source file, so
a project active inside the window but already converted kept its
unfiltered output and was reported "cached". Converting every project
instead would give each idle one an empty combined page, and mark it as
rendered from a window, so that the next plain run re-rendered the whole
archive. A project is converted when a visible session overlaps the
window; the others are left untouched and reported as such.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from claude_code_log.cache import CacheManager, get_library_version

from test.test_date_filtered_runs import (
    SESSIONS,
    SHAPES,
    _append,
    _build_project,
    _run,
    _second_day,
    _turn,
)

# The second day only: the active project's second turns, nothing of the
# idle project.
SECOND_DAY = {"from_date": "2026-01-02", "to_date": "2026-01-02"}
IDLE = "-idle"


def _build_idle_project(root: Path) -> Path:
    """A project whose every message is on the first day."""
    project = root / "projects" / IDLE
    project.mkdir(parents=True)
    for sid in ("idle-one", "idle-two"):
        _append(project / f"{sid}.jsonl", _turn(sid, 0, 1, f"first {sid}"))
    return project


def _outputs(project: Path, shape: str) -> Dict[str, bytes]:
    ext = SHAPES[shape]["output_format"]
    return {
        p.name: p.read_bytes()
        for p in sorted(project.glob(f"*.{ext}"))
        if p.name.startswith(("session-", "combined_transcripts"))
    }


@pytest.mark.parametrize("shape", ["md-sessions", "html-combined"])
def test_a_filtered_pass_renders_an_unchanged_project_active_in_the_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    active = _build_project(tmp_path)
    idle = _build_idle_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(active.parent, shape)
    idle_before = _outputs(idle, shape)
    assert idle_before

    capsys.readouterr()
    _run(active.parent, shape, **SECOND_DAY)
    out = capsys.readouterr().out

    # The active project, unchanged on disk, now shows only its window.
    ext = SHAPES[shape]["output_format"]
    for sid in SESSIONS:
        text = (active / f"session-{sid}.{ext}").read_text(encoding="utf-8")
        assert _second_day(sid) in text
        # The first day's reply (the title repeats the first prompt anyway).
        assert "reply 0" not in text, f"session-{sid} was not filtered"
    # The idle project is left exactly as it was, and said to be so.
    assert _outputs(idle, shape) == idle_before
    assert f"{IDLE}: no messages in the window" in out, out
    assert f"{IDLE}: cached" not in out, out


@pytest.mark.parametrize("shape", ["md-sessions", "html-combined"])
def test_a_filtered_pass_leaves_an_idle_project_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    """An idle project's outputs are not marked as rendered from the window,
    so the next plain run re-renders only the projects the window touched."""
    active = _build_project(tmp_path)
    idle = _build_idle_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(active.parent, shape)
    _run(active.parent, shape, **SECOND_DAY)

    manager = CacheManager(idle, get_library_version())
    ext = SHAPES[shape]["output_format"]
    stale = manager.get_stale_sessions(
        {"idle-one", "idle-two"}, ext=ext, output_dir=idle
    )
    assert stale == []

    capsys.readouterr()
    _run(active.parent, shape)
    out = capsys.readouterr().out
    assert f"{IDLE}: cached" in out, out
    # Control: the active project, rendered from the window, is redone.
    assert f"{active.name}: cached" not in out, out
    for sid in SESSIONS:
        text = (active / f"session-{sid}.{ext}").read_text(encoding="utf-8")
        assert "reply 0" in text


def test_a_filtered_pass_still_converts_a_changed_idle_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed file may hold messages its cached rows do not know of yet,
    so the window cannot be judged from them: such a project is converted
    as before."""
    active = _build_project(tmp_path)
    idle = _build_idle_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(active.parent, "md-sessions")
    _append(idle / "idle-one.jsonl", _turn("idle-one", 1, 2, "LATE-IDLE"))

    _run(active.parent, "md-sessions", **SECOND_DAY)
    text = (idle / "session-idle-one.md").read_text(encoding="utf-8")
    assert "LATE-IDLE" in text
    assert "reply 0" not in text


def test_a_filtered_pass_into_a_fresh_output_writes_every_linked_combined_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle project with no output at the destination still gets its
    combined page, empty for the window: the index links every project's
    combined page, and leaving one out would make that link dead."""
    import re

    from claude_code_log.converter import process_projects_hierarchy

    active = _build_project(tmp_path)
    _build_idle_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(active.parent, "html-combined")

    fresh = tmp_path / "fresh"
    process_projects_hierarchy(
        active.parent,
        from_date=SECOND_DAY["from_date"],
        to_date=SECOND_DAY["to_date"],
        jobs=1,
        output_format="html",
        output_dir=fresh,
    )

    index = (fresh / "index.html").read_text(encoding="utf-8")
    # The cards' links are built by script from data embedded in the page.
    links = set(re.findall(r"[^\"' <>=]*combined_transcripts[^\"' <>]*", index))
    assert any(IDLE in link for link in links), links
    dead = sorted(link for link in links if not (fresh / link).is_file())
    assert dead == []
