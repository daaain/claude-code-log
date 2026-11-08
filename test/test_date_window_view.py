# pyright: reportPrivateUsage=false
"""A date-filtered run writes a view of its window, and only there.

Every page such a run writes, and every page and link those pages show,
comes from the window: the master index lists the projects with a
session in it, with the window's totals, and each project links only the
session pages the run wrote. Written outside the project's own
directory, its pages are not recorded in the render cache as the full
view's, which shares their file names.

The fixture's window is 2026-01-02. In ``-mixed``:

- ``s-day1`` is all on the first day: outside;
- ``s-day2`` is all on the second: inside;
- ``s-span`` starts on the first day and has only a reply on the second,
  so its window part has no user message: inside;
- ``s-gap`` runs from the first day to the third with nothing on the
  second, so its first and last messages straddle the window: outside.

``-idle`` is all on the first day.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest
from claude_code_log.converter import process_projects_hierarchy

from test.test_date_filtered_runs import _append, _entry, _turn

WINDOW = {"from_date": "2026-01-02", "to_date": "2026-01-02"}
INSIDE = {"s-day2", "s-span"}
OUTSIDE = {"s-day1", "s-gap"}


def _archive(root: Path) -> Path:
    """Two projects under ``root/projects``; returns that directory."""
    mixed = root / "projects" / "-mixed"
    mixed.mkdir(parents=True)
    _append(mixed / "s-day1.jsonl", _turn("s-day1", 0, 1, "ONLY-DAY-ONE"))
    _append(mixed / "s-day2.jsonl", _turn("s-day2", 0, 2, "ONLY-DAY-TWO"))
    _append(
        mixed / "s-span.jsonl",
        _turn("s-span", 0, 1, "SPAN-ASKED")
        + [
            _entry(
                "assistant",
                "s-span",
                "s-span-a1",
                "s-span-a0",
                "2026-01-02T11:00:00Z",
                "SPAN-ANSWERED-LATE",
            )
        ],
    )
    _append(
        mixed / "s-gap.jsonl",
        _turn("s-gap", 0, 1, "GAP-FIRST") + _turn("s-gap", 1, 3, "GAP-THIRD"),
    )
    idle = root / "projects" / "-idle"
    idle.mkdir()
    _append(idle / "i-one.jsonl", _turn("i-one", 0, 1, "IDLE-ONE"))
    return root / "projects"


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    return _archive(tmp_path)


def _full(projects: Path, fmt: str = "html") -> None:
    process_projects_hierarchy(projects, jobs=1, output_format=fmt)


def _window(
    projects: Path,
    output_dir: Optional[Path] = None,
    *,
    fmt: str = "html",
    jobs: int = 1,
) -> Path:
    return process_projects_hierarchy(
        projects,
        jobs=jobs,
        output_format=fmt,
        output_dir=output_dir,
        **WINDOW,
    )


def _pages(root: Path) -> Dict[str, bytes]:
    """Every rendered page under ``root``, by relative path.

    The vendored ``assets/`` are not pages: the same bytes whatever the
    view, and left out of the destination check on purpose.
    """
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and "assets" not in p.relative_to(root).parts
        and p.suffix in (".html", ".md", ".json", ".js")
    }


def _render_rows(cache: Path) -> List[Tuple[Any, ...]]:
    with sqlite3.connect(cache) as conn:
        rows = conn.execute(
            "SELECT p.project_path, h.html_path, h.message_count, h.generated_at"
            " FROM html_cache h JOIN projects p ON p.id = h.project_id"
            " ORDER BY 1, 2"
        ).fetchall()
        rows += conn.execute(
            "SELECT p.project_path, h.html_path, h.message_count, h.generated_at"
            " FROM html_pages h JOIN projects p ON p.id = h.project_id"
            " ORDER BY 1, 2"
        ).fetchall()
    return rows


def _hrefs(text: str) -> Set[str]:
    """The relative page links in an index page or the recent-data script."""
    found = re.findall(r"""href=['"]([^'"#?]+)""", text)
    found += re.findall(r'"href":\s*"([^"#?]+)"', text)
    # Not the search script's template literals, nor absolute links.
    return {link for link in found if not link.startswith(("http", "/", "$"))}


def _session_ids(names: Set[str]) -> Set[str]:
    return {m.group(1) for n in names if (m := re.search(r"session-(.+)\.\w+$", n))}


# ---- What the view shows ---------------------------------------------------


def test_the_index_lists_only_the_projects_active_in_the_window(
    projects: Path, tmp_path: Path
) -> None:
    _window(projects, tmp_path / "view", fmt="json")
    index = json.loads(
        (tmp_path / "view" / "all-projects-summary.json").read_text(encoding="utf-8")
    )
    assert [p["name"] for p in index["projects"]] == ["-mixed"]
    card = index["projects"][0]
    assert {s["id"] for s in card["sessions"]} == INSIDE
    # The window's totals: s-day2's exchange and s-span's late reply.
    assert card["message_count"] == 3
    assert card["total_output_tokens"] == 10
    assert card["earliest_timestamp"] == "2026-01-02T10:00:00Z"
    assert card["latest_timestamp"] == "2026-01-02T11:00:00Z"
    span = next(s for s in card["sessions"] if s["id"] == "s-span")
    assert span["message_count"] == 1
    # Named by its whole-session title, though its window part has no
    # user message.
    assert span["first_user_message"] == "SPAN-ASKED"


def test_no_page_of_the_view_links_a_file_it_did_not_write(
    projects: Path, tmp_path: Path
) -> None:
    """Rendered over a full view in place, a dead link would be hidden by
    the full view's files: a fresh directory shows every one."""
    _full(projects)
    view = tmp_path / "view"
    _window(projects, view)
    links = _hrefs((view / "index.html").read_text(encoding="utf-8"))
    links |= _hrefs((view / "recent-data.js").read_text(encoding="utf-8"))
    assert any("session-" in link for link in links), links
    dead = sorted(link for link in links if not (view / link).is_file())
    assert dead == []
    assert "-idle" not in (view / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("jobs", [1, 2])
def test_the_pages_written_are_the_pages_linked(
    projects: Path, tmp_path: Path, jobs: int
) -> None:
    """One set of sessions decides both: which session pages the run
    writes, and which the index links. Each would otherwise read the
    window its own way: the session rows' first and last stamps (s-gap
    straddles the window), or the window's own user messages (s-span has
    none)."""
    view = tmp_path / "view"
    _window(projects, view, jobs=jobs)
    written = set(_pages(view))
    assert _session_ids(written) == INSIDE

    index = (view / "index.html").read_text(encoding="utf-8")
    linked = {link for link in _hrefs(index) if "session-" in link}
    assert linked == {p for p in written if "/session-" in p}


def test_a_session_that_arrives_during_the_pass_is_left_to_the_next(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass reads the window before converting and links what it read:
    a session started in between is not linked, so its page is not
    written either."""
    from claude_code_log import converter

    read = converter._project_window_view

    def read_then_start_a_session(project_dir: Path, *args: Any) -> Any:
        view = read(project_dir, *args)
        if project_dir.name == "-mixed":
            _append(project_dir / "s-late.jsonl", _turn("s-late", 0, 2, "LATE"))
        return view

    monkeypatch.setattr(converter, "_project_window_view", read_then_start_a_session)
    view = tmp_path / "view"
    _window(projects, view)
    # Control: the conversion did see it, in the combined page.
    combined = (view / "-mixed" / "combined_transcripts.html").read_text(
        encoding="utf-8"
    )
    assert "LATE" in combined
    assert not (view / "-mixed" / "session-s-late.html").exists()


def test_an_archived_project_is_not_part_of_a_view(
    projects: Path, tmp_path: Path
) -> None:
    """With its source gone, a window of it cannot be rendered: its pages
    are the full view's."""
    _full(projects)
    for f in (projects / "-idle").glob("*.jsonl"):
        _append(f, _turn("i-one", 1, 2, "IDLE-LATER"))
    _full(projects)
    for f in (projects / "-idle").glob("*.jsonl"):
        f.unlink()
    view = tmp_path / "view"
    _window(projects, view)
    assert "-idle" not in (view / "index.html").read_text(encoding="utf-8")


# ---- The render cache ------------------------------------------------------


def test_a_view_written_elsewhere_leaves_the_render_cache_alone(
    projects: Path, tmp_path: Path
) -> None:
    """The render cache's rows are keyed by project and file name: marking
    them for a view written elsewhere would make the next plain run
    rebuild the full view's pages, which nothing touched."""
    _full(projects)
    rows = _render_rows(tmp_path / "cache.db")
    _window(projects, tmp_path / "view")
    assert _render_rows(tmp_path / "cache.db") == rows
