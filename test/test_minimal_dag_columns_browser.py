"""Browser tests for the minimal theme's DAG engine, P7 (``minimal_dag.js``).

work/minimal-theme-dag.md P7; dev-docs/minimal-theme.md. Column (swimlane)
mode per branch and as the global "Columns" choice, collapsing a column to a
strip, the per-turn "+N more branches" overflow, reveals past the cap
(search, the timeline), teammate anchors and the async result shown at its
merge row only.

Fixtures (``test/dag_demo_fixture.py``): the mockup-shaped demo project, the
same with four extra background agents in its first turn (``wide=4``), and a
lead + teammate exchanging messages both ways (``write_team_demo``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from test.dag_demo_fixture import write_dag_demo, write_team_demo
from test.test_minimal_dag_browser import (
    BRANCHES_KEY,
    DEPTH_KEY,
    A,
    B,
    C,
    D,
    _head_attr,
    _lane_card_id,
    _mode,
    _open,
    _rail,
    _render,
    _settle,
    _toggle,
    _visible_count,
)

pytestmark = pytest.mark.browser

EXTRA = [f"agent-e000x0{k}" for k in range(4)]  # wide=4: ranks 3–6 in turn 1


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("dag7")
    return {
        "demo": _render(write_dag_demo(tmp / "demo"), tmp / "demo.html", "Demo"),
        "wide": _render(write_dag_demo(tmp / "wide", 1, 4), tmp / "wide.html", "Wide"),
        "team": _render(write_team_demo(tmp / "team"), tmp / "team.html", "Team"),
    }


@pytest.fixture
def clean(page: Page):
    yield page
    try:
        page.evaluate(
            f"localStorage.removeItem('{BRANCHES_KEY}'); localStorage.removeItem('{DEPTH_KEY}')"
        )
    except Exception:  # page already closed
        pass


def _column_of(page: Page, lane: str) -> list[str]:
    """Computed grid-column-start of the lane's visible cards."""
    return page.evaluate(
        """(lane) => [...document.querySelectorAll(`#transcript .message[data-lane="${lane}"]`)]
            .filter(el => el.checkVisibility())
            .map(el => getComputedStyle(el).gridColumnStart)""",
        lane,
    )


def _columns(page: Page) -> None:
    page.locator("[data-mn-branches='columns']").click()


# Visible cards with a timestamp: [top, bottom, lane, ms, column].
CARDS = """() => [...document.querySelectorAll('#transcript .message')]
    .filter(el => el.checkVisibility())
    .map(el => {
        const stamp = el.querySelector('.timestamp[data-timestamp]');
        const box = el.getBoundingClientRect();
        return [box.top, box.bottom, el.getAttribute('data-lane'),
                stamp ? Date.parse(stamp.getAttribute('data-timestamp')) : null,
                getComputedStyle(el).gridColumnStart];
    })
    .filter(row => row[3] !== null)"""


# ---------------------------------------------------------------- columns


class TestColumns:
    def test_one_lane_in_a_column(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        cards = page.locator(f"#transcript .message[data-lane='{A}']").count()
        page.locator(f"[data-lane-ref='{A}'] .mn-bcol").click()
        assert _mode(page, A) == "column"
        assert page.locator(".mn-stage.dag-cols").count() == 1
        assert page.locator("body.dag-wide").count() == 1
        # Every card of the lane, in the column next to main.
        assert _visible_count(page, A) == cards
        assert set(_column_of(page, A)) == {"2"}
        assert set(_column_of(page, "main")) == {"1"}
        # Its head: the name, "⇤ Interleave" and "Collapse".
        head = page.locator(f".dag-chrome[data-col='{A}'] .dag-colhead")
        expect(head).to_be_visible()
        assert head.locator(".dag-colname").get_attribute("data-label") == _head_attr(
            page, A, "data-lane-name"
        )
        expect(page.locator(".dag-mainhead")).to_be_visible()
        # The spawn row's control says so, and its button switches back.
        ctl = page.locator(f"[data-lane-ref='{A}']")
        assert (
            ctl.locator(".mn-bfold")
            .get_attribute("data-label")
            .endswith("· in column →")
        )
        assert ctl.locator(".mn-bcol").get_attribute("data-label") == "⇤ Interleave"
        # A column has no rail lane; the others keep theirs.
        assert _rail(page, A)["paths"] == []
        assert _rail(page, B)["paths"]

    def test_rows_are_time_aligned(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _columns(page)
        cards = page.evaluate(CARDS)
        assert {c[4] for c in cards} >= {"1", "2", "3"}
        # Time runs top to bottom across columns: a card wholly below another
        # is never earlier.
        for top, _bottom, lane, ms, _col in cards:
            for top2, _b2, lane2, ms2, _c2 in cards:
                if top2 > top + 1:
                    assert ms2 >= ms, (lane, ms, lane2, ms2)
        # Packing: cards in different columns share rows (equal tops).
        shared = {
            (round(c[0]), round(d[0]))
            for c in cards
            for d in cards
            if c[4] != d[4] and abs(c[0] - d[0]) <= 1
        }
        assert shared
        # A lane starts below its spawn row, an async merge row (the
        # notification) sits below the lane's last row.
        spawn = page.evaluate(
            "(id) => document.getElementById(id).getBoundingClientRect().top",
            _lane_card_id(page, A, "data-lane-from"),
        )
        merge = page.evaluate(
            "(id) => document.getElementById(id).getBoundingClientRect().top",
            _lane_card_id(page, A, "data-lane-to"),
        )
        tops = [c[0] for c in cards if c[2] == A]
        assert min(tops) > spawn and merge > max(tops)

    def test_all_columns_with_nesting_and_persistence(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        _columns(page)
        lanes = page.evaluate(
            "[...document.querySelectorAll('[data-lane-id]')].map(e => e.dataset.laneId)"
        )
        assert len(lanes) == 5
        assert all(_mode(page, x) == "column" for x in lanes)
        cols = {lane: set(_column_of(page, lane)) for lane in lanes}
        assert all(len(c) == 1 for c in cols.values())
        assert len({next(iter(c)) for c in cols.values()}) == 5  # distinct
        # The nested lane sits right next to its parent's column.
        assert int(next(iter(cols[D]))) == int(next(iter(cols[C]))) + 1
        btn = page.locator("[data-mn-branches='columns']")
        expect(btn).to_have_attribute("aria-pressed", "true")
        assert page.evaluate(f"localStorage.getItem('{BRANCHES_KEY}')") == "columns"
        page.reload()
        page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
        assert all(_mode(page, x) == "column" for x in lanes)
        # Main only puts everything back: no columns, no chrome, narrow page.
        page.locator("[data-mn-branches='main']").click()
        assert all(_mode(page, x) == "folded" for x in lanes)
        assert page.locator(".mn-stage.dag-cols").count() == 0
        assert page.locator(".dag-chrome").count() == 0
        assert page.locator("body.dag-wide").count() == 0

    def test_collapse_to_a_strip_and_expand(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _columns(page)
        page.locator(f".dag-chrome[data-col='{A}'] [data-col-act='collapse']").click()
        assert _mode(page, A) == "strip"
        assert _visible_count(page, A) == 0
        strip = page.locator(f".dag-chrome[data-col='{A}'] .dag-strip")
        expect(strip).to_be_visible()
        width = page.evaluate(
            f"document.querySelector('.dag-chrome[data-col=\"{A}\"]').getBoundingClientRect().width"
        )
        assert width == pytest.approx(34, abs=1)
        assert strip.locator("span").get_attribute("data-label") == _head_attr(
            page, A, "data-lane-name"
        )
        strip.click()
        assert _mode(page, A) == "column"
        assert _visible_count(page, A) > 0

    def test_back_to_interleave(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        page.locator(f"[data-lane-ref='{A}'] .mn-bcol").click()
        page.locator(f".dag-chrome[data-col='{A}'] [data-col-act='interleave']").click()
        assert _mode(page, A) == "interleaved"
        assert page.locator(".mn-stage.dag-cols").count() == 0
        assert set(_column_of(page, A)) == {"auto"}  # the one-column grid
        assert page.locator(f"#transcript .message.dag-in[data-lane='{A}']").count()
        assert _rail(page, A)["paths"]
        # And from the spawn row's own button.
        page.locator(f"[data-lane-ref='{B}'] .mn-bcol").click()
        assert _mode(page, B) == "column"
        page.locator(f"[data-lane-ref='{B}'] .mn-bcol").click()
        assert _mode(page, B) == "interleaved"
        # The chevron folds a column.
        page.locator(f"[data-lane-ref='{B}'] .mn-bcol").click()
        _toggle(page, B)
        assert _mode(page, B) == "folded"

    def test_page_scrolls_sideways_and_the_toolbar_stays(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        page.set_viewport_size({"width": 1280, "height": 900})
        _open(page, pages["demo"])
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        _columns(page)
        # 360px main + 5 × 300px columns: wider than the viewport.
        assert page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 400"
        )
        page.evaluate("window.scrollTo(600, 0)")
        _settle(page)
        box = page.locator(".mn-toolbar").bounding_box()
        assert box is not None
        assert box["x"] == pytest.approx(16, abs=1)
        assert box["x"] + box["width"] <= 1280
        # The last column is reachable.
        page.evaluate("window.scrollTo(document.documentElement.scrollWidth, 0)")
        _settle(page)
        last = page.evaluate(
            """() => { const heads = [...document.querySelectorAll('.dag-chrome')];
                return heads[heads.length - 1].getBoundingClientRect().right; }"""
        )
        assert last <= 1280 + 1


# ---------------------------------------------------------------- overflow


class TestOverflow:
    def test_more_branches_toggle(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["wide"])
        ranks = page.evaluate(
            """() => Object.fromEntries([...document.querySelectorAll('[data-lane-id]')]
                .map(e => [e.dataset.laneId, +e.dataset.laneRank]))"""
        )
        assert ranks[A] == 1 and ranks[B] == 2 and ranks[EXTRA[0]] == 3
        hidden = [lane for lane, rank in ranks.items() if rank > 3]
        assert set(EXTRA[1:]) <= set(hidden)
        # Three controls in turn 1; the rest behind the toggle on the third.
        for lane in (A, B, EXTRA[0]):
            expect(page.locator(f"[data-lane-ref='{lane}']")).to_be_visible()
        for lane in hidden:
            expect(page.locator(f"[data-lane-ref='{lane}']")).to_be_hidden()
            assert _rail(page, lane)["paths"] == []
        more = page.locator(f"[data-lane-ref='{EXTRA[0]}'] .mn-bmore")
        assert more.get_attribute("data-label") == f"+{len(hidden)} more branches"
        assert page.locator(".mn-bmore").count() == 1
        # Revealed: every control and dashed lane.
        more.click()
        for lane in hidden:
            expect(page.locator(f"[data-lane-ref='{lane}']")).to_be_visible()
        assert _rail(page, EXTRA[3])["paths"]
        assert more.get_attribute("data-label") == "− fewer branches"
        # The reader chooses which three are interleaved (LRU per turn).
        for lane in (A, B, EXTRA[3]):
            _toggle(page, lane)
        _toggle(page, EXTRA[2])
        assert _mode(page, A) == "folded"
        assert [_mode(page, x) for x in (B, EXTRA[3], EXTRA[2])] == ["interleaved"] * 3
        # Fewer again: open lanes keep their controls; folded extras hide.
        more.click()
        expect(page.locator(f"[data-lane-ref='{EXTRA[3]}']")).to_be_visible()
        expect(page.locator(f"[data-lane-ref='{EXTRA[1]}']")).to_be_hidden()
        assert more.get_attribute("data-label") == f"+{len(hidden) - 2} more branches"

    def test_columns_are_not_capped(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["wide"])
        _columns(page)
        lanes = page.evaluate(
            "[...document.querySelectorAll('[data-lane-id]')].map(e => e.dataset.laneId)"
        )
        assert len(lanes) == 9
        assert all(_mode(page, x) == "column" for x in lanes)
        assert page.locator(".dag-chrome").count() == 10  # main + 9
        assert page.locator(".mn-bctl[hidden]").count() == 0
        assert page.locator(".mn-bmore").count() == 0

    def test_search_across_many_lanes_shows_every_hit(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["wide"])
        _toggle(page, A)
        _toggle(page, B)
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("corner-")
        for lane in EXTRA:
            page.wait_for_function(
                f"window.claudeLogDag.mode('{lane}') === 'interleaved'"
            )
        # Past the cap of three: nothing the reader had open was folded, and
        # every hit is on screen.
        assert _mode(page, A) == "interleaved" and _mode(page, B) == "interleaved"
        for lane in EXTRA:
            expect(
                page.locator(
                    f"#transcript .message.search-match[data-lane='{lane}']"
                ).first
            ).to_be_visible()
        # The next manual selection trims the turn back to three.
        page.locator("#searchInput").fill("")
        _toggle(page, A)  # fold
        _toggle(page, A)  # re-select
        turn_open = [x for x in [A, B] + EXTRA if _mode(page, x) == "interleaved"]
        assert len(turn_open) == 3 and A in turn_open

    def test_search_under_columns_opens_columns(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["wide"])
        _columns(page)
        page.locator(
            f".dag-chrome[data-col='{EXTRA[1]}'] [data-col-act='collapse']"
        ).click()
        _toggle(page, EXTRA[2])  # fold
        assert _mode(page, EXTRA[2]) == "folded"
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("corner-")
        page.wait_for_function(f"window.claudeLogDag.mode('{EXTRA[2]}') === 'column'")
        page.wait_for_function(f"window.claudeLogDag.mode('{EXTRA[1]}') === 'column'")


# ------------------------------------------------------------- reveals


class TestReveal:
    def test_timeline_click_opens_a_folded_lane(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        # Tall enough for the timeline to render its Sub-assistant group (vis
        # only draws the groups in view).
        page.set_viewport_size({"width": 1280, "height": 1600})
        _open(page, pages["demo"])
        assert _mode(page, A) == "folded"
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-timeline", timeout=30000)
        page.wait_for_selector(".vis-item", timeout=10000)
        item = page.locator(".vis-item:has-text('96 matches')").first
        item.click(force=True)
        page.wait_for_function(f"window.claudeLogDag.mode('{A}') === 'interleaved'")
        target = page.locator(
            f"#transcript .message[data-lane='{A}']:has-text('96 matches in 9 files')"
        ).first
        expect(target).to_be_visible()
        expect(target).to_be_in_viewport(timeout=5000)


# ------------------------------------------------------------ teammates


class TestTeammateAnchors:
    def test_teammates_are_not_lanes(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["team"])
        assert page.locator("[data-lane-id]").count() == 0
        assert page.locator(".mn-bctls").count() == 0
        assert page.locator(".mn-branches").is_hidden()

    def test_spawn_links_to_the_thread(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["team"])
        spawn = page.locator("[data-teammate-link]")
        first = "msg-" + (spawn.get_attribute("data-teammate-link") or "")
        link = spawn.locator(".mn-xlink")
        assert link.get_attribute("href") == f"#{first}"
        assert link.get_attribute("data-label") == "→ alice's thread"
        # Collapsed by default, revealed by the link.
        assert not page.locator(f"#{first}").is_visible()
        link.click()
        expect(page.locator(f"#{first}")).to_be_visible()
        expect(page.locator(f"#{first}")).to_be_in_viewport()

    def test_messages_link_both_ways(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["team"])
        # alice → lead: the lead's <teammate-message> links back to alice's
        # SendMessage inside her (folded) thread …
        received = page.locator(
            "#transcript .message.teammate:not(.sidechain):has-text('Relay coverage')"
        )
        back = received.locator(".mn-xlink")
        assert back.get_attribute("data-label") == "← sent by alice"
        target = (back.get_attribute("href") or "")[1:]
        back.click()
        sent = page.locator(f"#{target}")
        expect(sent).to_be_visible()
        expect(sent).to_be_in_viewport()
        assert "Relay coverage" in sent.inner_text()
        assert sent.locator(".mn-xlink").get_attribute("href") == "#" + (
            received.get_attribute("id") or ""
        )
        # lead → alice: the lead's SendMessage links to the copy in her thread.
        reply = page.locator(
            "#transcript .message.tool_use:not(.sidechain):has-text('calculate_next_retry')"
        )
        fwd = reply.locator(".mn-xlink")
        assert fwd.get_attribute("data-label") == "→ received by alice"
        fwd.click()
        copy = page.locator((fwd.get_attribute("href") or ""))
        expect(copy).to_be_visible()
        assert "sidechain" in (copy.get_attribute("class") or "")
        # A message with no counterpart gets no link.
        stray = page.locator("#transcript .message.teammate:has-text('heartbeat')")
        assert stray.locator(".mn-xlink").count() == 0


# --------------------------------------------------------- result at merge


class TestResultAtMerge:
    def test_async_answer_only_on_the_notification(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        answer = "14 files, 412 literals"
        spawn = _lane_card_id(page, A, "data-lane-from")
        note = _lane_card_id(page, A, "data-lane-to")
        # The spawn row: the call, its control and stats, the launch line.
        spawn_text = page.evaluate(
            """(id) => { const call = document.getElementById(id);
                const node = call.closest('.message-node');
                const result = node.nextElementSibling
                    && node.nextElementSibling.querySelector(':scope > .message');
                return call.innerText + '\\n' + (result ? result.innerText : ''); }""",
            spawn,
        )
        assert answer not in spawn_text
        assert "from async notification" not in spawn_text
        # The answer is on the notification, at its arrival time …
        expect(page.locator(f"#{note}")).to_contain_text(answer)
        # … and the spawn's result links there.
        jump = page.locator(f".mn-async-jump a[href='#{note}']")
        expect(jump).to_have_count(1)
        jump.click()
        expect(page.locator(f"#{note}")).to_be_in_viewport()
        # Exactly one copy of the answer on the page.
        count = page.evaluate(
            "(text) => [...document.querySelectorAll('#transcript .message')].filter(el => el.textContent.includes(text)).length",
            answer,
        )
        assert count == 1


class TestColumnsLiveUpdate:
    def test_a_wholesale_swap_rebuilds_the_columns(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        page.locator(f"[data-lane-ref='{A}'] .mn-bcol").click()
        # live_update.js's fallback: a fresh server render replaces
        # #transcript; it carries none of the engine's state or chrome.
        page.evaluate(
            """() => {
                const old = document.getElementById('transcript');
                const next = old.cloneNode(true);
                next.querySelectorAll('.mn-bctls, .dag-chrome').forEach(el => el.remove());
                next.querySelectorAll('[style]').forEach(el => {
                    el.style.removeProperty('grid-row'); el.style.removeProperty('grid-column');
                    el.style.removeProperty('--dag-tag');
                });
                next.querySelectorAll('*').forEach(el => [...el.classList]
                    .filter(c => c.startsWith('dag-')).forEach(c => el.classList.remove(c)));
                old.replaceWith(next);
                window.claudeLogRehydrate(next);
            }"""
        )
        _settle(page)
        assert _mode(page, A) == "column"
        assert page.locator(f"#transcript > .dag-chrome[data-col='{A}']").count() == 1
        assert page.locator("#transcript > .dag-chrome").count() == 2  # main + A
        assert set(_column_of(page, A)) == {"2"}
        # Chrome stays ahead of every card (it paints underneath them).
        assert page.evaluate(
            """() => [...document.getElementById('transcript').children]
                .findIndex(el => !el.classList.contains('dag-chrome')) === 2"""
        )
