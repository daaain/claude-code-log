"""Browser tests for the recent sessions page (recent.html), opened from disk.

The page refreshes itself by re-adding a <script src="recent-data.js?t=…">
(a file:// page can't fetch), so these write the page and its data file
into a temporary directory and drive it as file://…/recent.html?now=<ms>,
which freezes the page's clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from playwright.sync_api import Locator, Page, expect

from claude_code_log.html.renderer import generate_recent_html
from claude_code_log.recent import recent_data_js

NOW = 1793465000000
MIN = 60000
HOUR = 60 * MIN
EVIL = "</script><img src=x onerror=window.__pwned=1>"


def _session(
    sid: str, last_ago: int, state: str = "done", **extra: Any
) -> dict[str, Any]:
    last_at = NOW - last_ago
    end = {"asked": "ask", "error": "err", "busy": "open"}.get(state, "asst")
    session: dict[str, Any] = {
        "id": f"{sid}-0000-4000-8000-000000000000",
        "short": sid,
        "project": "foldyard",
        "projectDir": "-home-u-foldyard",
        "worktree": None,
        "branch": "main",
        "title": f"Session {sid}",
        "href": f"-home-u-foldyard/session-{sid}.html",
        "firstAt": last_at - HOUR,
        "lastAt": last_at,
        "state": state,
        "messages": 12,
        "turnCount": 2,
        "toolCount": 3,
        "turns": [
            [last_at - HOUR, last_at - 40 * MIN, "asst"],
            [last_at - 30 * MIN, last_at, end],
        ],
        "earlierTurns": 0,
        "tools": [last_at - 50 * MIN, last_at - 20 * MIN, last_at - 10 * MIN],
        "lastPrompt": {"at": last_at - 30 * MIN, "text": f"Prompt for {sid}"},
        "last": {"at": last_at, "who": "claude", "text": f"Reply from {sid}"},
    }
    session.update(extra)
    return session


def _payload(
    sessions: list[dict[str, Any]], generated_at: Optional[int] = None
) -> dict[str, Any]:
    return {
        "v": 1,
        "generatedAt": NOW if generated_at is None else generated_at,
        "windowDays": 3,
        "cacheTtlMinutes": 60,
        "sessions": sessions,
    }


def _write(
    tmp_path: Path, payload: dict[str, Any], data: Optional[dict[str, Any]] = None
) -> str:
    (tmp_path / "recent.html").write_text(generate_recent_html(), encoding="utf-8")
    (tmp_path / "recent-data.js").write_text(
        recent_data_js(data or payload), encoding="utf-8"
    )
    return (tmp_path / "recent.html").as_uri() + f"?now={NOW}"


def _row(page: Page, sid: str) -> Locator:
    return page.locator(f".rc-row[data-id^='{sid}']")


@pytest.mark.browser
def test_reload_picks_up_new_sessions_in_place(page: Page, tmp_path: Path) -> None:
    first = _payload(
        [_session("aaaa1111", 4 * MIN, "asked"), _session("bbbb2222", 37 * MIN)]
    )
    page.clock.install()
    page.goto(_write(tmp_path, first))
    rows = page.locator(".rc-row")
    expect(rows).to_have_count(2)
    # Newest first, with its time since the update and its state.
    expect(rows.nth(0)).to_have_attribute(
        "data-id", "aaaa1111-0000-4000-8000-000000000000"
    )
    expect(rows.nth(0).locator(".rc-block-n")).to_have_text("4")
    expect(rows.nth(0).locator(".rc-block-status")).to_have_text("asked you")
    expect(rows.nth(1).locator(".rc-block-status")).to_have_text("finished")

    # Open a row and pick a filter: both must survive the refresh.
    block = _row(page, "bbbb2222").locator(".rc-block")
    block.click()
    expect(block).to_have_attribute("aria-expanded", "true")
    expect(_row(page, "bbbb2222").locator(".rc-last-text")).to_have_text(
        "Reply from bbbb2222"
    )
    page.get_by_role("button", name="your move").click()
    marker = page.evaluate(
        "document.querySelector('.rc-row[data-id^=bbbb2222]').dataset.marker = 'kept'"
    )
    assert marker == "kept"

    # The data file changes on disk; the page reloads it on its timer.
    newer = _payload(
        [_session("cccc3333", 1 * MIN, "error")] + first["sessions"],
    )
    (tmp_path / "recent-data.js").write_text(recent_data_js(newer), encoding="utf-8")
    page.clock.run_for(16000)
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_have_attribute(
        "data-id", "cccc3333-0000-4000-8000-000000000000"
    )
    expect(rows.nth(0).locator(".rc-block-status")).to_have_text("error")
    # Same page (no reload), the row element kept, still open, filter kept.
    assert (
        page.evaluate(
            "document.querySelector('.rc-row[data-id^=bbbb2222]').dataset.marker"
        )
        == "kept"
    )
    expect(block).to_have_attribute("aria-expanded", "true")
    expect(page.locator("[data-filter='move']")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("[data-filter='move'] .rc-n")).to_have_text("3")
    # Each reload's script tag is removed again (the page's own first-load
    # tag stays: it has no "?t=").
    expect(page.locator("script[src^='recent-data.js?t=']")).to_have_count(0)


@pytest.mark.browser
def test_transcript_text_stays_text(page: Page, tmp_path: Path) -> None:
    evil = _session(
        "dddd4444",
        2 * MIN,
        title=EVIL,
        worktree=EVIL,
        branch=EVIL,
        href="javascript:window.__pwned=2",
        lastPrompt={"at": NOW - 5 * MIN, "text": EVIL},
        last={"at": NOW - 2 * MIN, "who": "tool", "text": EVIL},
    )
    page.goto(_write(tmp_path, _payload([evil])))
    row = _row(page, "dddd4444")
    expect(row.locator(".rc-title")).to_have_text(EVIL)
    row.locator(".rc-block").click()
    expect(row.locator(".rc-last-text")).to_have_text(EVIL)
    assert page.locator(".rc-page img").count() == 0
    # A scheme in href is refused: the title is plain text, not a link.
    assert row.locator("a.rc-title").count() == 0
    assert page.evaluate("window.__pwned") is None


@pytest.mark.browser
def test_filters_cache_and_grouping(page: Page, tmp_path: Path) -> None:
    sessions = [
        _session("aaaa1111", 3 * MIN, "busy"),
        _session("bbbb2222", 50 * MIN, "asked", project="tangible", projectDir="-t"),
        _session("cccc3333", 2 * HOUR, "done"),
        _session("dddd4444", 30 * HOUR, "done", project="tangible", projectDir="-t"),
    ]
    page.goto(_write(tmp_path, _payload(sessions)))
    counts = {
        name: page.locator(f"[data-filter='{name}'] .rc-n").inner_text()
        for name in ("all", "move", "urgent", "cache")
    }
    assert counts == {"all": "4", "move": "3", "urgent": "1", "cache": "1"}

    # 50 min since the update: the cache has 10 min left, chip and band.
    cached = _row(page, "bbbb2222")
    expect(cached.locator(".rc-chip")).to_have_text("cache 10 min")
    expect(cached.locator(".rc-band.rc-band-risk")).to_have_count(1)
    # Under a day: H:MM h; older: a weekday, and the strip's summary.
    expect(_row(page, "cccc3333").locator(".rc-block-u")).to_have_text("h")
    expect(_row(page, "cccc3333").locator(".rc-block-n")).to_have_text("2:00")
    old = _row(page, "dddd4444")
    expect(old.locator(".rc-outside")).to_contain_text("2 turns · 12 messages")
    expect(old.locator(".rc-bar")).to_have_count(0)
    # The last turn's end and an ask always stay full markers.
    expect(cached.locator(".rc-mk.rc-k-ask")).to_have_count(1)
    expect(cached.locator(".rc-strip")).to_have_attribute("role", "img")

    page.get_by_role("button", name="asked / error").click()
    expect(page.locator(".rc-row")).to_have_count(1)
    page.get_by_role("button", name="cache expiring").click()
    expect(page.locator(".rc-row")).to_have_count(1)
    page.get_by_role("button", name="sessions").click()
    expect(page.locator(".rc-row")).to_have_count(4)

    page.get_by_label("by project").check()
    headings = page.locator(".rc-group-h")
    expect(headings).to_have_count(2)
    # Groups ordered by their newest session.
    expect(headings.nth(0)).to_contain_text("foldyard")
    expect(headings.nth(1)).to_contain_text("tangible")
    expect(page.locator(".rc-group").nth(1).locator(".rc-row")).to_have_count(2)


@pytest.mark.browser
def test_crowded_markers_shrink_to_dots(page: Page, tmp_path: Path) -> None:
    turns = [
        [NOW - 60 * MIN + i * 4 * MIN, NOW - 58 * MIN + i * 4 * MIN, "asst"]
        for i in range(12)
    ]
    busy = _session("eeee5555", 10 * MIN, "done", turns=turns, tools=[])
    page.goto(_write(tmp_path, _payload([busy])))
    row = _row(page, "eeee5555")
    expect(row.locator(".rc-mk")).not_to_have_count(0)
    assert row.locator(".rc-dot").count() > 0
    last = row.locator(".rc-mk.rc-k-asst").last
    expect(last).to_have_attribute(
        "title",
        "Claude finished · "
        + page.evaluate(
            "(() => { const d = new Date(%d); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); })()"
            % turns[-1][1]
        ),
    )


@pytest.mark.browser
def test_stale_notice_and_empty_state(page: Page, tmp_path: Path) -> None:
    stale = _payload([], generated_at=NOW - 5 * MIN)
    fresh = _payload([_session("ffff6666", 1 * MIN)])
    page.clock.install()
    page.goto(_write(tmp_path, stale, stale))
    expect(page.locator("#rc-empty")).to_have_text(
        "No sessions updated in the last 3 days."
    )
    notice = page.locator("#rc-stale")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("Data from 5 min ago")
    expect(notice).to_contain_text("claude-code-log watch")
    (tmp_path / "recent-data.js").write_text(recent_data_js(fresh), encoding="utf-8")
    page.clock.run_for(16000)
    expect(page.locator(".rc-row")).to_have_count(1)
    expect(notice).to_be_hidden()
    expect(page.locator("#rc-empty")).to_be_hidden()


@pytest.mark.browser
def test_copy_button_and_phone_width(page: Page, tmp_path: Path) -> None:
    page.set_viewport_size({"width": 375, "height": 800})
    try:
        page.goto(_write(tmp_path, _payload([_session("abcd1234", 3 * MIN, "asked")])))
        widths = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert widths[0] <= widths[1], widths
        copy = page.get_by_role(
            "button", name="Copy session id abcd1234-0000-4000-8000-000000000000"
        )
        expect(copy).to_have_text("abcd1234")
        copy.click()
        expect(copy).to_have_text("copied")
        expect(copy).to_have_text("abcd1234", timeout=5000)
    finally:
        page.set_viewport_size({"width": 1280, "height": 720})
