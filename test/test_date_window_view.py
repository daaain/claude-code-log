# pyright: reportPrivateUsage=false
"""A date-filtered run writes a view of its window, and only there.

Every page such a run writes, and every page and link those pages show,
comes from the window: the master index lists the projects with a
session in it, with the window's totals, and each project links only the
session pages the run wrote. The view goes into an output directory that
holds none of the files it writes, or over them with ``--force``, which
overwrites and removes nothing. Its pages are not recorded in the render
cache as the full view's, which shares their file names.

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
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.converter import (
    WindowOutputExists,
    convert_jsonl_to,
    process_projects_hierarchy,
)

from test.test_date_filtered_runs import _append, _entry, _turn

# The window every view here is written for: one day, from and to.
DAY = "2026-01-02"
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
    force: bool = False,
    jobs: int = 1,
) -> Path:
    return process_projects_hierarchy(
        projects,
        jobs=jobs,
        output_format=fmt,
        output_dir=output_dir,
        force=force,
        from_date=DAY,
        to_date=DAY,
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
def test_the_pages_written_are_the_pages_linked_and_the_pages_checked(
    projects: Path, tmp_path: Path, jobs: int
) -> None:
    """One set of sessions decides all three: which session pages the run
    writes, which the index links, and which files a destination may not
    hold. Each would otherwise read the window its own way: the session
    rows' first and last stamps (s-gap straddles the window), or the
    window's own user messages (s-span has none)."""
    view = tmp_path / "view"
    _window(projects, view, jobs=jobs)
    written = set(_pages(view))
    assert _session_ids(written) == INSIDE

    index = (view / "index.html").read_text(encoding="utf-8")
    linked = {link for link in _hrefs(index) if "session-" in link}
    assert linked == {p for p in written if "/session-" in p}

    # Each page the run writes makes a destination holding it refuse the
    # run; a page of the full view outside the window does not.
    for page in sorted(written):
        dest = tmp_path / f"holds-{page.replace('/', '_')}"
        (dest / page).parent.mkdir(parents=True)
        (dest / page).write_text("mine", encoding="utf-8")
        with pytest.raises(WindowOutputExists) as raised:
            _window(projects, dest)
        assert raised.value.path == dest / page
        assert (dest / page).read_text(encoding="utf-8") == "mine"
    for page in ("-mixed/session-s-day1.html", "-mixed/session-s-gap.html"):
        dest = tmp_path / f"holds-{page.replace('/', '_')}"
        (dest / page).parent.mkdir(parents=True)
        (dest / page).write_text("mine", encoding="utf-8")
        _window(projects, dest)
        assert (dest / page).read_text(encoding="utf-8") == "mine"


def test_without_a_cache_the_view_shows_the_same_sessions(
    projects: Path, tmp_path: Path
) -> None:
    """``--no-cache`` has no session rows to read the window's sessions
    from: they come from the transcripts themselves, by the same rules, so
    the index lists and links the same sessions, whole-session titles and
    window counts included, and the same session pages are written."""
    cached = tmp_path / "cached"
    _window(projects, cached, fmt="json")
    view = tmp_path / "view"
    process_projects_hierarchy(
        projects,
        jobs=1,
        output_format="json",
        output_dir=view,
        use_cache=False,
        from_date=DAY,
        to_date=DAY,
    )
    summary = "all-projects-summary.json"

    def cards(root: Path) -> List[Any]:
        index = json.loads((root / summary).read_text(encoding="utf-8"))
        return [(p["name"], p["sessions"]) for p in index["projects"]]

    assert cards(view) == cards(cached)
    assert _session_ids(set(_pages(view))) == INSIDE

    html = tmp_path / "html"
    process_projects_hierarchy(
        projects, jobs=1, output_dir=html, use_cache=False, from_date=DAY, to_date=DAY
    )
    written = set(_pages(html))
    assert _session_ids(written) == INSIDE
    index = (html / "index.html").read_text(encoding="utf-8")
    linked = {link for link in _hrefs(index) if "session-" in link}
    assert linked == {p for p in written if "/session-" in p}


def test_one_project_without_a_cache_writes_the_window_s_session_pages(
    projects: Path, tmp_path: Path
) -> None:
    view = tmp_path / "view"
    convert_jsonl_to(
        "html",
        projects / "-mixed",
        output_root=view,
        use_cache=False,
        silent=True,
        from_date=DAY,
        to_date=DAY,
    )
    assert _session_ids(set(_pages(view))) == INSIDE
    assert not (tmp_path / "cache.db").exists()


def _recent_payload(index_dir: Path) -> Dict[str, Any]:
    text = (index_dir / "recent-data.js").read_text(encoding="utf-8")
    start = text.index("__cclRecent(") + len("__cclRecent(")
    return json.loads(text[start : text.rindex(");")])


def test_the_recent_page_lists_the_window_s_sessions_with_its_counts(
    projects: Path, tmp_path: Path
) -> None:
    """Not the sessions active in the last few days: the window's, however
    old, each with what the window holds of it. s-span's prompt was on the
    first day, so in the window it has none; its title stays the whole
    session's, as on the index."""
    _full(projects)
    view = tmp_path / "view"
    _window(projects, view)
    payload = _recent_payload(view)
    sessions = {s["id"]: s for s in payload["sessions"]}
    assert set(sessions) == INSIDE
    assert payload["window"] == "from 2026-01-02 to 2026-01-02"
    assert sessions["s-day2"]["messages"] == 2
    span = sessions["s-span"]
    assert span["messages"] == 1
    assert span["firstAt"] == span["lastAt"] == 1767351600000  # 2026-01-02 11:00Z
    assert span["lastPrompt"] is None
    assert span["title"] == "SPAN-ASKED"
    page = (view / "recent.html").read_text(encoding="utf-8")
    assert "<title>Recent sessions (from 2026-01-02 to 2026-01-02)</title>" in page
    header = re.search(r"<p class='rc-window'>(.*?)</p>", page, re.S)
    assert header is not None and "from 2026-01-02 to 2026-01-02" in header.group(1)


def test_the_search_page_says_it_searches_the_whole_archive(
    projects: Path, tmp_path: Path
) -> None:
    _full(projects)
    full = (projects / "search.html").read_text(encoding="utf-8")
    view = tmp_path / "view"
    _window(projects, view)
    page = (view / "search.html").read_text(encoding="utf-8")
    note = re.search(r"<p class='archive-search-window'>(.*?)</p>", page, re.S)
    assert note is not None, page[:2000]
    assert "from 2026-01-02 to 2026-01-02" in note.group(1)
    assert "whole archive" in note.group(1)
    assert "archive-search-window" not in full


def test_a_session_that_arrives_during_the_pass_is_left_to_the_next(
    projects: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass reads the window before converting, links what it read and
    checks those pages at the destination: a session started in between is
    neither, so its page is not written either."""
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


def test_the_progress_line_counts_the_window_s_session_pages(
    projects: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _full(projects)
    capsys.readouterr()
    _window(projects, tmp_path / "view")
    out = capsys.readouterr().out
    assert f"-mixed: {len(INSIDE)} sessions" in out, out


# ---- Where the view goes ---------------------------------------------------


def test_a_page_of_a_project_outside_the_window_does_not_count(
    projects: Path, tmp_path: Path
) -> None:
    """Only the pages the view writes count: an idle project's combined
    page is none of them."""
    dest = tmp_path / "view"
    held = dest / "-idle" / "combined_transcripts.html"
    held.parent.mkdir(parents=True)
    held.write_text("mine", encoding="utf-8")
    _window(projects, dest)
    assert held.read_text(encoding="utf-8") == "mine"
    assert (dest / "-mixed" / "session-s-day2.html").is_file()


def test_one_project_counts_only_the_window_s_session_pages(
    projects: Path, tmp_path: Path
) -> None:
    """Converted on its own, a project checks the session pages of the
    window, not every session it has."""
    mixed = projects / "-mixed"
    convert_jsonl_to("html", mixed, silent=True)
    dest = tmp_path / "view"
    held = dest / "session-s-day1.html"
    held.parent.mkdir(parents=True)
    held.write_text("mine", encoding="utf-8")
    convert_jsonl_to(
        "html", mixed, output_root=dest, silent=True, from_date=DAY, to_date=DAY
    )
    assert held.read_text(encoding="utf-8") == "mine"
    assert _session_ids(set(_pages(dest))) == INSIDE | {"s-day1"}


def test_a_view_over_a_full_one_is_refused_and_writes_nothing(
    projects: Path, tmp_path: Path
) -> None:
    _full(projects)
    before = _pages(projects)
    rows = _render_rows(tmp_path / "cache.db")
    with pytest.raises(WindowOutputExists) as raised:
        _window(projects)
    # The master index is the first file such a run would write.
    assert raised.value.path == projects / "index.html"
    assert _pages(projects) == before
    assert _render_rows(tmp_path / "cache.db") == rows


def test_forced_in_place_it_overwrites_and_removes_nothing(
    projects: Path, tmp_path: Path
) -> None:
    _full(projects)
    _window(projects, force=True)
    mixed = projects / "-mixed"
    # Rewritten from the window.
    combined = (mixed / "combined_transcripts.html").read_text(encoding="utf-8")
    assert "ONLY-DAY-TWO" in combined and "ONLY-DAY-ONE" not in combined
    # Left as they were, unlinked: the full view's pages outside the window.
    for sid in OUTSIDE:
        assert (mixed / f"session-{sid}.html").is_file()
    assert "ONLY-DAY-ONE" in (mixed / "session-s-day1.html").read_text(encoding="utf-8")
    assert (projects / "-idle" / "combined_transcripts.html").is_file()
    assert "-idle" not in (projects / "index.html").read_text(encoding="utf-8")


def test_forced_in_place_it_leaves_the_rewritten_pages_stale(
    projects: Path, tmp_path: Path
) -> None:
    """The next run without a window rebuilds what the view replaced, and
    only that."""
    _full(projects)
    _window(projects, force=True)
    marked = {
        (Path(path).name, page)
        for path, page, count, _at in _render_rows(tmp_path / "cache.db")
        if count == -1
    }
    assert marked == {
        ("-mixed", "combined_transcripts.html"),
        ("-mixed", "session-s-day2.html"),
        ("-mixed", "session-s-span.html"),
    }


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


def test_one_project_converted_on_its_own_follows_the_same_rule(
    projects: Path, tmp_path: Path
) -> None:
    mixed = projects / "-mixed"
    convert_jsonl_to("html", mixed, silent=True)
    rows = _render_rows(tmp_path / "cache.db")
    with pytest.raises(WindowOutputExists) as raised:
        convert_jsonl_to("html", mixed, silent=True, from_date=DAY, to_date=DAY)
    assert raised.value.path == mixed / "combined_transcripts.html"

    view = tmp_path / "view"
    convert_jsonl_to(
        "html", mixed, output_root=view, silent=True, from_date=DAY, to_date=DAY
    )
    assert _session_ids(set(_pages(view))) == INSIDE
    assert _render_rows(tmp_path / "cache.db") == rows

    convert_jsonl_to("html", mixed, silent=True, force=True, from_date=DAY, to_date=DAY)
    assert "ONLY-DAY-ONE" not in (mixed / "combined_transcripts.html").read_text(
        encoding="utf-8"
    )


def test_a_combined_page_exported_elsewhere_still_marks_the_session_pages(
    projects: Path, tmp_path: Path
) -> None:
    """``-o page.html`` on a project sends only the combined page elsewhere:
    the session pages are still written into the project, over the full
    view's, so they are left stale like any page a view replaced there."""
    mixed = projects / "-mixed"
    _full(projects)
    result = CliRunner().invoke(
        main,
        [
            str(mixed),
            "-o",
            str(tmp_path / "page.html"),
            "--from-date",
            "2026-01-02",
            "--to-date",
            "2026-01-02",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ONLY-DAY-TWO" in (mixed / "session-s-day2.html").read_text(encoding="utf-8")
    marked = {
        page
        for path, page, count, _at in _render_rows(tmp_path / "cache.db")
        if Path(path).name == "-mixed" and count == -1
    }
    assert marked == {f"session-{sid}.html" for sid in INSIDE}


def test_one_transcript_file_follows_the_same_rule(
    projects: Path, tmp_path: Path
) -> None:
    transcript = projects / "-mixed" / "s-day2.jsonl"
    page = convert_jsonl_to("html", transcript, silent=True)
    with pytest.raises(WindowOutputExists) as raised:
        convert_jsonl_to("html", transcript, silent=True, from_date=DAY, to_date=DAY)
    assert raised.value.path == page
    # One file's view is one page, which another --output does move.
    assert "another --output" in str(raised.value)
    assert "--combined only" not in str(raised.value)
    elsewhere = tmp_path / "day-two.html"
    convert_jsonl_to(
        "html", transcript, elsewhere, silent=True, from_date=DAY, to_date=DAY
    )
    assert "ONLY-DAY-TWO" in elsewhere.read_text(encoding="utf-8")


# ---- The command line ------------------------------------------------------


def test_the_refusal_names_the_file_and_both_ways_out(projects: Path) -> None:
    _full(projects)
    result = CliRunner().invoke(
        main,
        [
            "--all-projects",
            "--projects-dir",
            str(projects),
            "--from-date",
            "2026-01-02",
            "--to-date",
            "2026-01-02",
        ],
    )
    assert result.exit_code == 1, result.output
    assert str(projects / "index.html") in result.output
    assert "--force" in result.output
    # Another directory moves every page of an --all-projects view.
    assert "another --output" in result.output
    assert "--combined only" not in result.output


def test_the_refusal_for_one_project_offers_what_can_move_its_pages(
    projects: Path,
) -> None:
    """For a project, ``--output`` names the combined page only: its
    session pages are written into the project whatever it names, so a
    refusal there offers the combined page alone instead."""
    _full(projects)
    result = CliRunner().invoke(
        main,
        [
            str(projects / "-mixed"),
            "-o",
            str(projects.parent / "page.html"),
            "--from-date",
            "2026-01-02",
            "--to-date",
            "2026-01-02",
        ],
    )
    assert result.exit_code == 1, result.output
    assert str(projects / "-mixed" / "session-s-day2.html") in result.output
    assert "--force" in result.output
    assert "--combined only" in result.output
    assert "another --output" not in result.output


def test_the_refusal_for_one_project_s_session_pages_alone_offers_force(
    projects: Path,
) -> None:
    """With ``--combined no`` there is no combined page to send elsewhere,
    and the session pages go into the project: only --force is left."""
    _full(projects)
    result = CliRunner().invoke(
        main,
        [
            str(projects / "-mixed"),
            "--combined",
            "no",
            "--from-date",
            "2026-01-02",
            "--to-date",
            "2026-01-02",
        ],
    )
    assert result.exit_code == 1, result.output
    assert str(projects / "-mixed" / "session-s-day2.html") in result.output
    assert "--force" in result.output
    assert "--combined only" not in result.output
    assert "another --output" not in result.output


def test_force_is_accepted_with_a_window_only(projects: Path) -> None:
    result = CliRunner().invoke(
        main, ["--all-projects", "--projects-dir", str(projects), "--force"]
    )
    assert result.exit_code == 2, result.output
    assert "--force" in result.output

    result = CliRunner().invoke(
        main,
        [
            "--all-projects",
            "--projects-dir",
            str(projects),
            "--force",
            "--from-date",
            "2026-01-02",
            "--to-date",
            "2026-01-02",
        ],
    )
    assert result.exit_code == 0, result.output


def test_the_provider_pipeline_takes_no_force(tmp_path: Path) -> None:
    """Its date-filtered run still writes over a full view, so there is
    nothing for --force to allow there yet."""
    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--from-date",
            "2026-01-02",
            "--force",
            "-o",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--force" in result.output
