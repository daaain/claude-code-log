"""The recent sessions page under changing transcripts, end to end.

``test/recent_live_fixture.py`` writes sessions across four projects and
moves them on stage by stage, as real JSONL; each stage goes through the
real full run (cache, session pages, ``recent.html``, ``recent-data.js``)
and the page, opened from disk as you would, reloads its data file. The
same stages are the recent sessions demo (``scripts/demos/record.py``) and
its play mode (``scripts/demos/recent_play.py``), so a change that breaks
the demo's story fails here first.

Nothing sleeps: each step waits for the page to show the state it expects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from test.conftest import collect_page_errors
from test.recent_live_fixture import (
    ATTACH,
    BENCH,
    CI_KEY,
    PIN,
    ROWS,
    TABLES,
    ZSH,
    RecentLive,
    wait_for_stage,
)

pytestmark = pytest.mark.browser


def _ids(page: Page) -> list[str]:
    return page.evaluate(
        "[...document.querySelectorAll('.rc-row')].map((r) => r.dataset.id)"
    )


def _statuses(page: Page) -> list[str]:
    return page.locator(".rc-row .rc-block-status").all_text_contents()


def _row(page: Page, session_id: str) -> Locator:
    return page.locator(f".rc-row[data-id='{session_id}']")


def _count(page: Page, name: str) -> Locator:
    return page.locator(f"[data-filter='{name}'] .rc-n")


def test_sessions_move_through_their_states(page: Page, tmp_path: Path) -> None:
    live = RecentLive(tmp_path / "projects")
    live.advance("start")
    errors = collect_page_errors(page, live.url)
    page.goto(live.url)
    wait_for_stage(page, "start")

    # Every state the page tells apart, newest update first, each with its
    # project; the background benchmark makes a finished turn "background".
    assert _ids(page) == [PIN, ROWS, BENCH, ZSH, CI_KEY, ATTACH]
    assert _statuses(page) == [
        "mid-turn",
        "mid-turn",
        "background",
        "error",
        "finished",
        "finished",
    ]
    expect(_count(page, "all")).to_have_text("6")
    expect(_count(page, "move")).to_have_text("3")
    expect(_count(page, "urgent")).to_have_text("1")
    expect(_row(page, PIN).locator(".rc-meta-project")).to_have_text("foldyard")
    expect(_row(page, ZSH).locator(".rc-meta-project")).to_have_text("dotfiles")
    # A title links to its session page, which the run wrote.
    href = _row(page, ROWS).locator("a.rc-title").get_attribute("href")
    assert href and (live.projects / href).is_file(), href

    rows = _row(page, ROWS)
    rows.locator(".rc-block").click()
    expect(rows.locator(".rc-who")).to_contain_text("Tool call")

    live.advance("tool_runs")
    wait_for_stage(page, "tool_runs")
    assert _ids(page)[:2] == [ROWS, PIN]
    # Still open, its last message the call it is waiting on.
    expect(rows.locator(".rc-block")).to_have_attribute("aria-expanded", "true")
    expect(rows.locator(".rc-last-text")).to_contain_text("just ci")

    live.advance("finishes")
    wait_for_stage(page, "finishes")
    expect(rows.locator(".rc-who")).to_contain_text("Claude")
    expect(rows.locator(".rc-last-text li")).to_have_count(3)
    expect(_count(page, "move")).to_have_text("4")

    live.advance("asks_you")
    wait_for_stage(page, "asks_you")
    assert _ids(page)[:2] == [PIN, ROWS]
    expect(_row(page, PIN).locator(".rc-last-text")).to_contain_text(
        "Which cache mode should the 9p mount use?"
    )
    expect(_count(page, "urgent")).to_have_text("2")

    live.advance("new_session")
    wait_for_stage(page, "new_session")
    expect(page.locator(".rc-row")).to_have_count(7)
    expect(_row(page, TABLES).locator(".rc-meta-project")).to_have_text("wenmode")

    live.advance("bg_done")
    wait_for_stage(page, "bg_done")
    assert _ids(page)[:4] == [BENCH, TABLES, PIN, ROWS]
    assert "background" not in _statuses(page)
    expect(_row(page, BENCH).locator(".rc-last-text")).to_contain_text("41 MB/s")

    # Grouped, each project's sessions sit under it, newest project first.
    page.locator("#rc-group").check()
    expect(page.locator(".rc-group-h")).to_have_count(4)
    expect(page.locator(".rc-group-h").first).to_contain_text("wenmode")
    expect(page.locator(".rc-group").first.locator(".rc-row")).to_have_count(2)

    assert errors == [], errors
