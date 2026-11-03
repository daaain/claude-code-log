"""Browser tests for the recent sessions page (recent.html), opened from disk.

The page refreshes itself by re-adding a <script src="recent-data.js?t=…">
(a file:// page can't fetch), so these write the page and its data file
into a temporary directory and drive it as file://…/recent.html?now=<ms>,
which freezes the page's clock.
"""

from __future__ import annotations

import re

from pathlib import Path
from typing import Any, Optional

import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from claude_code_log.html.renderer import generate_recent_html
from claude_code_log.html.utils import render_markdown, render_user_markdown
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
        "lastPrompt": {
            "at": last_at - 30 * MIN,
            "html": render_user_markdown(f"Prompt for {sid}"),
        },
        "last": {
            "at": last_at,
            "who": "claude",
            "html": render_markdown(f"Reply from {sid}"),
        },
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
        "bgCapMinutes": 60,
        "wakeGraceMinutes": 5,
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


def _hhmm(page: Page, ms: int) -> str:
    """A time of day as the page writes it (the browser's local zone)."""
    return page.evaluate(
        "(ms) => { const d = new Date(ms); return String(d.getHours()).padStart(2, '0')"
        " + ':' + String(d.getMinutes()).padStart(2, '0'); }",
        ms,
    )


@pytest.mark.browser
def test_reload_picks_up_new_sessions_in_place(
    isolated_page: Page, tmp_path: Path
) -> None:
    # It installs a clock, which belongs to the context: a fresh one.
    page = isolated_page
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
        lastPrompt={"at": NOW - 5 * MIN, "html": render_user_markdown(EVIL)},
        last={"at": NOW - 2 * MIN, "who": "tool", "text": EVIL},
        bg=[[NOW - 20 * MIN, None, "bash", None, EVIL, None]],
        wakes=[[NOW + 10 * MIN, EVIL]],
    )
    page.goto(_write(tmp_path, _payload([evil])))
    row = _row(page, "dddd4444")
    expect(row.locator(".rc-title")).to_have_text(EVIL)
    row.locator(".rc-block").click()
    expect(row.locator(".rc-last-text")).to_have_text(EVIL)
    expect(row.locator(".rc-bgl li").first).to_contain_text(EVIL)
    assert page.locator(".rc-page img").count() == 0
    # A scheme in href is refused: the title is plain text, not a link.
    assert row.locator("a.rc-title").count() == 0
    assert page.evaluate("window.__pwned") is None


@pytest.mark.browser
def test_rendered_markdown_cannot_run_script(page: Page, tmp_path: Path) -> None:
    """The html fields go in as markup; the pipeline that rendered them
    escaped the transcript's raw HTML, so it arrives as text."""
    evil = _session(
        "dddd5555",
        2 * MIN,
        lastPrompt={
            "at": NOW - 5 * MIN,
            "html": render_user_markdown(EVIL),
        },
        last={
            "at": NOW - 2 * MIN,
            "who": "claude",
            "html": render_markdown(EVIL),
        },
    )
    page.goto(_write(tmp_path, _payload([evil])))
    row = _row(page, "dddd5555")
    row.locator(".rc-block").click()
    expect(row.locator(".rc-last-text")).to_contain_text("onerror=window.__pwned=1")
    expect(row.locator(".rc-prompt .rc-md")).to_contain_text("<img src=x")
    assert page.locator(".rc-page img").count() == 0
    assert page.evaluate("window.__pwned") is None


@pytest.mark.browser
def test_expanded_row_renders_markdown(page: Page, tmp_path: Path) -> None:
    reply = "Done:\n\n- **bold** item\n\n```python\nx = 1\n```"
    session = _session(
        "aaaa7777",
        3 * MIN,
        lastPrompt={
            "at": NOW - 10 * MIN,
            "html": render_user_markdown("Fix `the cache`"),
        },
        last={
            "at": NOW - 3 * MIN,
            "who": "claude",
            "html": render_markdown(reply),
        },
    )
    page.goto(_write(tmp_path, _payload([session])))
    row = _row(page, "aaaa7777")
    row.locator(".rc-block").click()
    last = row.locator(".rc-last-text.rc-md")
    expect(last.locator("li strong")).to_have_text("bold")
    expect(last.locator(".highlight")).to_have_count(1)
    expect(row.locator(".rc-prompt .rc-md code")).to_have_text("the cache")
    # A tool line is plain text, not Markdown.
    tool = _session(
        "bbbb7777",
        4 * MIN,
        "busy",
        last={"at": NOW - 4 * MIN, "who": "tool", "text": "Bash · **not bold**"},
    )
    page.goto(_write(tmp_path, _payload([session, tool])))
    _row(page, "bbbb7777").locator(".rc-block").click()
    tool_last = _row(page, "bbbb7777").locator(".rc-last")
    expect(tool_last.locator(".rc-last-text")).to_have_text("Bash · **not bold**")
    assert tool_last.locator(".rc-md").count() == 0


@pytest.mark.browser
def test_a_plan_or_question_says_so(page: Page, tmp_path: Path) -> None:
    plan = _session(
        "aaaa8888",
        3 * MIN,
        "asked",
        last={"at": NOW - 3 * MIN, "who": "plan", "html": render_markdown("1. Split")},
    )
    question = _session(
        "bbbb8888",
        5 * MIN,
        "asked",
        last={
            "at": NOW - 5 * MIN,
            "who": "question",
            "html": render_markdown("Which?"),
        },
    )
    page.goto(_write(tmp_path, _payload([plan, question])))
    for sid, label in (("aaaa8888", "Plan to approve"), ("bbbb8888", "Claude asked")):
        row = _row(page, sid)
        row.locator(".rc-block").click()
        expect(row.locator(".rc-who")).to_contain_text(label + " · ")
        expect(row.locator(".rc-last-text.rc-md")).to_be_visible()


@pytest.mark.browser
def test_the_chevron_opens_the_row(page: Page, tmp_path: Path) -> None:
    page.goto(_write(tmp_path, _payload([_session("cccc7777", 3 * MIN)])))
    row = _row(page, "cccc7777")
    chevron = row.locator(".rc-chev")
    block = row.locator(".rc-block")
    # Pointer-only: the block stays the one control for keyboards and
    # screen readers, so a row doesn't gain a second tab stop.
    expect(chevron).to_have_attribute("aria-hidden", "true")
    expect(row).not_to_have_attribute("data-open", "true")
    chevron.click()
    expect(block).to_have_attribute("aria-expanded", "true")
    expect(row).to_have_attribute("data-open", "true")
    expect(row.locator(".rc-x")).to_be_visible()
    chevron.click()
    expect(block).to_have_attribute("aria-expanded", "false")
    expect(row.locator(".rc-x")).to_be_hidden()


@pytest.mark.browser
def test_markers_show_their_turns_snippets(page: Page, tmp_path: Path) -> None:
    start, end = NOW - 40 * MIN, NOW - 10 * MIN
    session = _session(
        "dddd7777",
        10 * MIN,
        "asked",
        turns=[[start, end, "ask", "Plan the change", "Which renderer?"]],
        tools=[],
    )
    page.goto(_write(tmp_path, _payload([session])))
    row = _row(page, "dddd7777")
    expect(row.locator(".rc-k-prompt")).to_have_attribute(
        "title", f"You · {_hhmm(page, start)}\nPlan the change"
    )
    expect(row.locator(".rc-mk.rc-k-ask")).to_have_attribute(
        "title", f"Claude asked you · {_hhmm(page, end)}\nWhich renderer?"
    )


@pytest.mark.browser
def test_the_strip_shows_four_hours_back(page: Page, tmp_path: Path) -> None:
    turns = [
        [NOW - 5 * HOUR, NOW - 5 * HOUR + 10 * MIN, "asst"],
        [NOW - 3 * HOUR, NOW - 3 * HOUR + 10 * MIN, "asst"],
    ]
    session = _session("eeee7777", 3 * HOUR - 10 * MIN, turns=turns, tools=[])
    page.goto(_write(tmp_path, _payload([session])))
    row = _row(page, "eeee7777")
    expect(row.locator(".rc-bar")).to_have_count(1)
    expect(row.locator(".rc-earlier")).to_have_text("◂ +1 earlier turn")
    # NOW sits at four fifths of the strip (4 h back, 1 h ahead).
    left = page.evaluate(
        "parseFloat(document.querySelector('.rc-row .rc-now').style.left)"
    )
    assert left == pytest.approx(80)


@pytest.mark.browser
def test_background_work_is_not_your_move(page: Page, tmp_path: Path) -> None:
    """A finished turn with background work still running, or a wake-up
    still to come, is waiting on itself; asked/error/busy are never
    overridden, and work with no end stops counting after the cap."""
    running = [[NOW - 20 * MIN, None, "agent", None, "Survey", None]]
    sessions = [
        _session("aaaa9999", 5 * MIN, "done", bg=running),
        _session("bbbb9999", 6 * MIN, "done", wakes=[[NOW + 20 * MIN, "CI"]]),
        # The wake-up was missed (past its time and the grace).
        _session("cccc9999", 7 * MIN, "done", wakes=[[NOW - 10 * MIN, "CI"]]),
        # No end recorded, but the session went quiet over an hour ago.
        _session("dddd9999", 70 * MIN, "done", bg=running),
        # A monitor past its own expiry.
        _session(
            "eeee9999",
            8 * MIN,
            "done",
            bg=[[NOW - 40 * MIN, None, "monitor", None, "CI", NOW - 10 * MIN]],
        ),
        _session("ffff9999", 9 * MIN, "asked", bg=running),
        _session(
            "abab9999",
            10 * MIN,
            "done",
            bg=[[NOW - 40 * MIN, NOW - 30 * MIN, "bash", "completed", "test", None]],
        ),
    ]
    page.goto(_write(tmp_path, _payload(sessions)))
    status = {
        sid: _row(page, sid).locator(".rc-block-status").inner_text().lower()
        for sid in (
            "aaaa9999",
            "bbbb9999",
            "cccc9999",
            "dddd9999",
            "eeee9999",
            "ffff9999",
            "abab9999",
        )
    }
    assert status == {
        "aaaa9999": "background",
        "bbbb9999": "background",
        "cccc9999": "finished",
        "dddd9999": "finished",
        "eeee9999": "finished",
        "ffff9999": "asked you",
        "abab9999": "finished",
    }
    expect(_row(page, "aaaa9999")).to_have_class(re.compile(r"\brc-state-waiting\b"))
    # Your move: the five finished-or-asked rows, not the two waiting ones.
    expect(page.locator("[data-filter='move'] .rc-n")).to_have_text("5")
    page.get_by_role("button", name="your move").click()
    expect(page.locator(".rc-row")).to_have_count(5)
    expect(_row(page, "aaaa9999")).to_have_count(0)
    page.get_by_role("button", name="sessions").click()
    # The strip's summary says so.
    expect(_row(page, "aaaa9999").locator(".rc-strip")).to_have_attribute(
        "aria-label", re.compile("1 background task running")
    )
    expect(_row(page, "bbbb9999").locator(".rc-strip")).to_have_attribute(
        "aria-label", re.compile("wakes up at " + _hhmm(page, NOW + 20 * MIN))
    )


@pytest.mark.browser
def test_background_lanes_pack_and_overflow(page: Page, tmp_path: Path) -> None:
    # Three in a row share one lane.
    sequential = [
        [NOW - 90 * MIN, NOW - 80 * MIN, "bash", "completed", "one", None],
        [NOW - 70 * MIN, NOW - 60 * MIN, "bash", "failed", "two", None],
        [NOW - 50 * MIN, NOW - 40 * MIN, "bash", "stopped", "three", None],
    ]
    # Six at once: two lanes of their own, the other four folded into one.
    crowded = [
        [NOW - 60 * MIN + i * MIN, None, "agent", None, f"agent {i}", None]
        for i in range(6)
    ]
    sessions = [
        _session("aaaa6666", 5 * MIN, bg=sequential),
        _session("bbbb6666", 5 * MIN, bg=crowded),
    ]
    page.goto(_write(tmp_path, _payload(sessions)))
    seq = _row(page, "aaaa6666")
    expect(seq.locator(".rc-bg")).to_have_count(3)
    assert set(
        seq.locator(".rc-bg").evaluate_all("els => els.map(e => e.dataset.lane)")
    ) == {"0"}
    expect(seq.locator(".rc-bg-end-completed")).to_have_count(1)
    expect(seq.locator(".rc-bg-end-failed")).to_have_count(1)
    expect(seq.locator(".rc-bg-end-stopped")).to_have_count(1)
    expect(seq.locator(".rc-bg-failed")).to_have_attribute(
        "title",
        f"Bash “two” · {_hhmm(page, NOW - 70 * MIN)}–{_hhmm(page, NOW - 60 * MIN)}"
        " · failed",
    )

    crowd = _row(page, "bbbb6666")
    lanes = crowd.locator(".rc-bg").evaluate_all("els => els.map(e => e.dataset.lane)")
    assert sorted(set(lanes)) == ["0", "1", "2"]
    expect(crowd.locator(".rc-bg.rc-bg-more")).to_have_count(1)
    expect(crowd.locator(".rc-bg-more-n")).to_have_text("+4 more")
    expect(crowd.locator(".rc-bg-running").first).to_have_attribute(
        "title", f"Agent “agent 0” · {_hhmm(page, NOW - 60 * MIN)} – running"
    )
    # The lanes stay inside the strip.
    box = crowd.locator(".rc-strip").bounding_box()
    for lane in crowd.locator(".rc-bg").all():
        seg = lane.bounding_box()
        assert box and seg and seg["y"] + seg["height"] <= box["y"] + box["height"]


@pytest.mark.browser
def test_lost_work_and_wake_markers(page: Page, tmp_path: Path) -> None:
    lost_start = NOW - 3 * HOUR
    session = _session(
        "cccc6666",
        90 * MIN,
        "done",
        bg=[[lost_start, None, "monitor", None, "CI", None]],
        turns=[
            [NOW - 3 * HOUR, NOW - 170 * MIN, "asst", "Watch CI", "Watching", "you"],
            [
                NOW - 2 * HOUR,
                NOW - 115 * MIN,
                "asst",
                "Monitor “CI” stream ended",
                "CI is green",
                "task",
            ],
            [
                NOW - 95 * MIN,
                NOW - 90 * MIN,
                "asst",
                "check the deploy",
                "Done",
                "wake",
            ],
        ],
        tools=[],
        wakes=[],
    )
    waiting = _session(
        "dddd6666",
        3 * MIN,
        "done",
        wakes=[[NOW + 25 * MIN, "Recheck CI"]],
    )
    page.goto(_write(tmp_path, _payload([session, waiting])))
    row = _row(page, "cccc6666")
    lost = row.locator(".rc-bg-lost")
    expect(lost).to_have_attribute(
        "title", f"Monitor “CI” · {_hhmm(page, lost_start)} – no end recorded"
    )
    expect(row.locator(".rc-mk.rc-k-task")).to_have_attribute(
        "title",
        f"Background work reported · {_hhmm(page, NOW - 2 * HOUR)}"
        "\nMonitor “CI” stream ended",
    )
    expect(row.locator(".rc-k-wake")).to_have_attribute(
        "title", f"Woke up · {_hhmm(page, NOW - 95 * MIN)}\ncheck the deploy"
    )
    pending = _row(page, "dddd6666").locator(".rc-mk.rc-k-wake.rc-pending")
    expect(pending).to_have_attribute(
        "title", f"Wakes up · {_hhmm(page, NOW + 25 * MIN)}\nRecheck CI"
    )
    # In the hour ahead: right of NOW (at 80%).
    assert float(pending.evaluate("e => parseFloat(e.style.left)")) > 80

    # The expanded row lists it all, running first.
    _row(page, "dddd6666").locator(".rc-block").click()
    expect(_row(page, "dddd6666").locator(".rc-bgl li")).to_have_text(
        [f"Wakes up {_hhmm(page, NOW + 25 * MIN)} · Recheck CI"]
    )
    row.locator(".rc-block").click()
    expect(row.locator(".rc-bgl li")).to_have_text(
        [f"Monitor · CI · {_hhmm(page, lost_start)} – no end recorded"]
    )


@pytest.mark.browser
def test_background_list_orders_running_first(page: Page, tmp_path: Path) -> None:
    bg = [
        [NOW - 50 * MIN, NOW - 40 * MIN, "bash", "completed", "build", None],
        [NOW - 45 * MIN, None, "agent", None, "Survey", None],
        [NOW - 30 * MIN, NOW - 20 * MIN, "bash", "failed", "test", None],
    ]
    page.goto(_write(tmp_path, _payload([_session("eeee6666", 5 * MIN, bg=bg)])))
    row = _row(page, "eeee6666")
    row.locator(".rc-block").click()
    expect(row.locator(".rc-bgl li")).to_have_text(
        [
            f"Agent · Survey · {_hhmm(page, NOW - 45 * MIN)} – running",
            f"Bash · build · {_hhmm(page, NOW - 50 * MIN)}–"
            f"{_hhmm(page, NOW - 40 * MIN)} · completed",
            f"Bash · test · {_hhmm(page, NOW - 30 * MIN)}–"
            f"{_hhmm(page, NOW - 20 * MIN)} · failed",
        ]
    )
    # The legend explains the new marks.
    expect(page.locator("#rc-legend")).to_contain_text("background work")
    expect(page.locator("#rc-legend")).to_contain_text("wake-up")


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
    turns: list[list[Any]] = [
        [NOW - 60 * MIN + i * 4 * MIN, NOW - 58 * MIN + i * 4 * MIN, "asst"]
        for i in range(12)
    ]
    busy = _session("eeee5555", 10 * MIN, "done", turns=turns, tools=[])
    page.goto(_write(tmp_path, _payload([busy])))
    row = _row(page, "eeee5555")
    expect(row.locator(".rc-mk")).not_to_have_count(0)
    assert row.locator(".rc-dot").count() > 0
    last = row.locator(".rc-mk.rc-k-asst").last
    # No snippets in these turns: just what and when.
    expect(last).to_have_attribute(
        "title", "Claude finished · " + _hhmm(page, turns[-1][1])
    )


@pytest.mark.browser
def test_stale_notice_and_empty_state(isolated_page: Page, tmp_path: Path) -> None:
    # It installs a clock, which belongs to the context: a fresh one.
    page = isolated_page
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


@pytest.mark.browser
def test_phone_layout_saves_rows(page: Page, tmp_path: Path) -> None:
    """At phone width: Updated shares the last filter row, right-aligned;
    the list has no box; an open row puts Transcript beside the copy
    button and its counts on one line."""
    page.set_viewport_size({"width": 375, "height": 800})
    try:
        page.goto(_write(tmp_path, _payload([_session("abcd1234", 3 * MIN, "asked")])))

        def box(selector: str) -> FloatRect:
            rect = page.locator(selector).first.bounding_box()
            assert rect is not None, f"{selector} is not laid out"
            return rect

        def middle(rect: FloatRect) -> float:
            return rect["y"] + rect["height"] / 2

        cache, updated, head = (
            box("[data-filter='cache']"),
            box(".rc-updated"),
            box(".rc-head"),
        )
        assert abs(updated["y"] - cache["y"]) < 2, (updated, cache)
        assert abs((updated["x"] + updated["width"]) - (head["x"] + head["width"])) < 2

        panel = page.evaluate(
            "(() => { const s = getComputedStyle(document.querySelector('.rc-panel'));"
            " return [s.borderTopWidth, s.borderRadius]; })()"
        )
        assert panel == ["0px", "0px"], panel

        row = _row(page, "abcd1234")
        row.locator(".rc-block").click()
        copy, link = box(".rc-copy"), box(".rc-x-link")
        assert abs(middle(copy) - middle(link)) < 2, (copy, link)
        tops = page.evaluate(
            "[...document.querySelectorAll('.rc-x-side dl > div')]"
            ".map(d => Math.round(d.getBoundingClientRect().top))"
        )
        assert len(tops) == 3 and len(set(tops)) == 1, tops
        widths = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert widths[0] <= widths[1], widths
    finally:
        page.set_viewport_size({"width": 1280, "height": 720})
