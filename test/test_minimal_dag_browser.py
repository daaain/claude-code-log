"""Browser tests for the minimal theme's DAG engine (P6, ``minimal_dag.js``).

work/minimal-theme-dag.md § 1.6 / § 3.5; dev-docs/minimal-theme.md. Branch
lanes — sub-agents at any depth and rewind forks — are folded to a control on
their spawn row by default ("Main only"), or interleaved with the main line at
their real times, drawn on an SVG rail beside it.

Fixtures: the mockup-shaped demo project (``test/dag_demo_fixture.py``: two
background agents, a synchronous agent with a nested one, a rewound prompt),
``nested_agents/`` (six lanes in one turn, for the per-turn cap) and
``dag_within_fork.jsonl``.

The browser context is shared by every browser test (persistent, for the HTTP
cache), and so is ``file://`` localStorage: tests clear the stored Branches
choice before and after.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_directory_transcripts, load_transcript
from claude_code_log.html.renderer import HtmlRenderer
from test.dag_demo_fixture import write_dag_demo

pytestmark = pytest.mark.browser

TEST_DATA = Path(__file__).parent / "test_data"
BRANCHES_KEY = "claude-code-log:branches"
DEPTH_KEY = "claude-code-log:fold-depth"

A = "agent-a000audit"  # async, 4 tool pairs + an answer
B = "agent-b000pyg"  # async, 2 tool pairs + an answer
C = "agent-c000sync"  # synchronous, spawns D
D = "agent-d000leaf"  # nested in C


def _render(source: Path, out: Path, title: str) -> Path:
    if source.is_dir():
        entries, tree = load_directory_transcripts(source, silent=True)
    else:
        entries, tree = load_transcript(source, silent=True), None
    renderer = HtmlRenderer()
    renderer.theme = "minimal"
    out.write_text(
        renderer.generate(entries, title, session_tree=tree), encoding="utf-8"
    )
    return out


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("dag")
    return {
        "demo": _render(write_dag_demo(tmp / "demo"), tmp / "demo.html", "Demo"),
        "nested": _render(TEST_DATA / "nested_agents", tmp / "nested.html", "Nested"),
        "fork": _render(TEST_DATA / "dag_within_fork.jsonl", tmp / "fork.html", "Fork"),
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


def _open(page: Page, path: Path, query: str = "") -> None:
    """Load with no stored Branches choice or fold depth."""
    page.goto(path.as_uri() + query)
    page.evaluate(
        f"localStorage.removeItem('{BRANCHES_KEY}'); localStorage.removeItem('{DEPTH_KEY}')"
    )
    page.reload()
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")


def _toggle(page: Page, lane: str) -> None:
    page.locator(f"[data-lane-ref='{lane}'] .mn-bfold").click()


def _settle(page: Page) -> None:
    """Wait two frames: the engine relayouts / redraws in requestAnimationFrame."""
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def _visible_count(page: Page, lane: str) -> int:
    return page.evaluate(
        """(lane) => [...document.querySelectorAll('#transcript .message')]
            .filter(el => el.getAttribute('data-lane') === lane && el.checkVisibility()).length""",
        lane,
    )


def _mode(page: Page, lane: str) -> str:
    return page.evaluate("(lane) => window.claudeLogDag.mode(lane)", lane)


# Visible cards with a timestamp, in visual order: [lane, ms, id].
VISUAL_ORDER = """() => [...document.querySelectorAll('#transcript .message')]
    .filter(el => el.checkVisibility())
    .map(el => {
        const stamp = el.querySelector('.timestamp[data-timestamp]');
        return [el.getBoundingClientRect().top, el.getAttribute('data-lane'),
                stamp ? Date.parse(stamp.getAttribute('data-timestamp')) : null, el.id];
    })
    .sort((a, b) => a[0] - b[0])
    .filter(row => row[2] !== null)
    .map(row => row.slice(1))"""

# The rail's paths for a lane, with stage-relative coordinates, plus the
# main line's x and each card's dot y (padding-top + .7em, layout.css).
RAIL = """(lane) => {
    const stage = document.querySelector('.mn-stage').getBoundingClientRect();
    const paths = [...document.querySelectorAll('#dag-rail path')]
        .filter(p => p.getAttribute('data-lane') === lane)
        .map(p => ({part: p.getAttribute('data-part'), d: p.getAttribute('d'),
                    dashed: p.classList.contains('dag-dash')}));
    const line = getComputedStyle(document.querySelector('.mn-stage'), '::before');
    return {paths, mainX: parseFloat(line.left) + parseFloat(line.width) / 2};
}"""
DOT_Y = """(id) => {
    const el = document.getElementById(id);
    const stage = document.querySelector('.mn-stage').getBoundingClientRect();
    const cs = getComputedStyle(el);
    return el.getBoundingClientRect().top - stage.top
        + parseFloat(cs.paddingTop) + 0.7 * parseFloat(cs.fontSize);
}"""


def _numbers(d: str) -> list[float]:
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]


def _rail(page: Page, lane: str) -> dict[str, Any]:
    rail = page.evaluate(RAIL, lane)
    rail["parts"] = {p["part"]: p for p in rail["paths"]}
    return rail


def _head_attr(page: Page, lane: str, name: str) -> str:
    return page.evaluate(
        '([lane, name]) => document.querySelector(`[data-lane-id="${lane}"]`).getAttribute(name)',
        [lane, name],
    )


def _lane_card_id(page: Page, lane: str, attr: str) -> str:
    """``d-N`` of a lane head's spawn (``data-lane-from``) or merge (``-to``)."""
    return "msg-" + _head_attr(page, lane, attr)


def _fork_lane(page: Page) -> str:
    return page.evaluate(
        "document.querySelector('[data-lane-kind=\"fork\"]').getAttribute('data-lane-id')"
    )


# ------------------------------------------------------------ main only


class TestMainOnly:
    def test_branches_are_folded_by_default(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        assert page.locator(".mn-stage.dag-on").count() == 1
        # No sub-agent card and no fork-lane card is visible.
        hidden = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message')]
                .filter(el => el.getAttribute('data-lane') !== 'main')
                .map(el => el.checkVisibility())"""
        )
        assert hidden and not any(hidden)
        # Every lane is folded; its spawn card carries a control whose label
        # is the server's stats.
        for lane in (A, B, C):
            assert _mode(page, lane) == "folded"
            label = page.locator(f"[data-lane-ref='{lane}'] .mn-bfold")
            expect(label).to_be_visible()
            assert label.get_attribute("data-label") == _head_attr(
                page, lane, "data-lane-stats"
            )
            assert label.get_attribute("aria-expanded") == "false"
            spawn = _lane_card_id(page, lane, "data-lane-from")
            assert (
                page.locator(f"#{spawn} > .mn-bctls [data-lane-ref='{lane}']").count()
                == 1
            )
        # The nested lane's control is inside its folded parent: not shown.
        expect(page.locator(f"[data-lane-ref='{D}']")).to_have_count(1)
        assert not page.locator(f"[data-lane-ref='{D}']").is_visible()
        # The Column button (P7) is live.
        col = page.locator(f"[data-lane-ref='{A}'] .mn-bcol")
        assert col.is_enabled()
        assert col.get_attribute("data-label") == "Column ⇥"

    def test_folded_lanes_draw_dashed_from_spawn_to_merge(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        for lane in (A, B, C):
            rail = _rail(page, lane)
            assert set(rail["parts"]) == {"fork", "lane", "merge"}, rail
            assert rail["parts"]["lane"]["dashed"]
            assert not rail["parts"]["fork"]["dashed"]
            spawn_y = page.evaluate(DOT_Y, _lane_card_id(page, lane, "data-lane-from"))
            merge_y = page.evaluate(DOT_Y, _lane_card_id(page, lane, "data-lane-to"))
            fork = _numbers(rail["parts"]["fork"]["d"])
            merge = _numbers(rail["parts"]["merge"]["d"])
            assert fork[0] == pytest.approx(
                rail["mainX"], abs=1.5
            )  # from the main line
            assert fork[1] == pytest.approx(spawn_y, abs=1.5)  # at the spawn row
            assert merge[-1] == pytest.approx(rail["mainX"], abs=1.5)  # back to main
            assert merge[-2] == pytest.approx(merge_y, abs=1.5)  # at the merge row
        # Async: the merge row is the <task-notification>.
        merge_card = _lane_card_id(page, A, "data-lane-to")
        assert "task-notification" in (
            page.locator(f"#{merge_card}").get_attribute("class") or ""
        )
        # Two concurrent lanes take two slots.
        xa = _numbers(_rail(page, A)["parts"]["lane"]["d"])[0]
        xb = _numbers(_rail(page, B)["parts"]["lane"]["d"])[0]
        assert xa != xb

    def test_fork_is_a_stub(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        fork = _fork_lane(page)
        rail = _rail(page, fork)
        assert set(rail["parts"]) == {"stub"}
        stub = _numbers(rail["parts"]["stub"]["d"])
        spawn_y = page.evaluate(DOT_Y, _lane_card_id(page, fork, "data-lane-from"))
        assert stub[1] == pytest.approx(spawn_y, abs=1.5)
        assert stub[-1] - stub[1] == pytest.approx(12, abs=0.5)


# ----------------------------------------------------------- interleaved


class TestInterleave:
    def test_one_lane_merges_by_time(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        cards = page.locator(f"#transcript .message[data-lane='{A}']").count()
        _toggle(page, A)
        assert _mode(page, A) == "interleaved"
        assert _visible_count(page, A) == cards
        assert (
            page.locator(f"[data-lane-ref='{A}'] .mn-bfold").get_attribute(
                "aria-expanded"
            )
            == "true"
        )
        assert (
            page.locator(f"[data-lane-ref='{A}'] .mn-bfold")
            .get_attribute("data-label")
            .endswith("· interleaved")
        )
        order = page.evaluate(VISUAL_ORDER)
        stamps = [ms for _lane, ms, _id in order]
        assert stamps == sorted(stamps), order
        lanes = [lane for lane, _ms, _id in order]
        # Really interleaved: main rows sit between lane rows.
        first, last = lanes.index(A), len(lanes) - 1 - lanes[::-1].index(A)
        assert "main" in lanes[first:last]
        # Interleaved rows carry the lane's gutter tag and slot.
        tag = page.evaluate(
            """(lane) => { const el = document.querySelector(`#transcript .message[data-lane="${lane}"]`);
                return [getComputedStyle(el.querySelector('.header-info'), '::after').content,
                        el.classList.contains('dag-in')]; }""",
            A,
        )
        assert tag == ['"' + _head_attr(page, A, "data-lane-tag") + '"', True]
        # Solid lane between a fork and a merge connector.
        rail = _rail(page, A)
        assert set(rail["parts"]) == {"fork", "lane", "merge"}
        assert not rail["parts"]["lane"]["dashed"]

    def test_tool_pairs_stay_together(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        _toggle(page, B)
        order = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message')]
                .filter(el => el.checkVisibility())
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
                .map(el => [el.getAttribute('data-lane'), el.className])"""
        )
        for (lane, cls), (next_lane, next_cls) in zip(order, order[1:]):
            if "pair_first" in cls.split() and next_lane != lane:
                pytest.fail(f"a {lane} call is followed by a {next_lane} row: {order}")

    def test_several_lanes(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        _toggle(page, B)
        assert _visible_count(page, A) and _visible_count(page, B)
        stamps = [ms for _lane, ms, _id in page.evaluate(VISUAL_ORDER)]
        assert stamps == sorted(stamps)
        slots = page.evaluate(
            """(lanes) => lanes.map(lane => [...document.querySelector(
                `#transcript .message[data-lane="${lane}"]`).classList].find(c => /^dag-s\\d$/.test(c)))""",
            [A, B],
        )
        assert slots[0] != slots[1]
        # Folding one leaves the other.
        _toggle(page, A)
        assert _visible_count(page, A) == 0 and _visible_count(page, B) > 0

    def test_async_result_lands_at_its_arrival_time(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, B)
        note = _lane_card_id(page, B, "data-lane-to")
        tops = page.evaluate(
            """([lane, note]) => {
                const top = el => el.getBoundingClientRect().top;
                const cards = [...document.querySelectorAll(`#transcript .message[data-lane="${lane}"]`)];
                return [Math.max(...cards.map(top)), top(document.getElementById(note))]; }""",
            [B, note],
        )
        assert tops[1] > tops[0]  # after the lane's last row
        rail = _rail(page, B)
        merge = _numbers(rail["parts"]["merge"]["d"])
        assert merge[-2] == pytest.approx(page.evaluate(DOT_Y, note), abs=1.5)
        # A synchronous spawn's result (its merge) is split from its call by
        # the lane's rows and shows as a row of its own.
        _toggle(page, C)
        result = _lane_card_id(page, C, "data-lane-to")
        call = _lane_card_id(page, C, "data-lane-from")
        assert "dag-split" in (page.locator(f"#{result}").get_attribute("class") or "")
        assert "dag-split" in (page.locator(f"#{call}").get_attribute("class") or "")
        expect(page.locator(f"#{result} > .header > .header-info")).to_be_visible()

    def test_rows_are_stable_when_a_lane_toggles(
        self, clean: Page, pages: dict[str, Path]
    ):
        """Rows are packed as if every lane were open: toggling one lane
        rewrites no other card's row."""
        page = clean
        _open(page, pages["demo"])
        rows = """() => Object.fromEntries([...document.querySelectorAll('#transcript .message')]
            .map(el => [el.id, el.style.gridRow]))"""
        before = page.evaluate(rows)
        assert all(before.values())
        _toggle(page, A)
        assert page.evaluate(rows) == before


# ------------------------------------------------------------- the cap


class TestPerTurnCap:
    def test_fourth_lane_folds_the_least_recently_selected(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["nested"])
        turn = page.evaluate(
            "[...new Set([...document.querySelectorAll('[data-lane-id]')].map(e => e.dataset.laneTurn))]"
        )
        assert len(turn) == 1  # every lane of this fixture is in one turn
        first = ["agent-nsmid001", "agent-nsmid002", "agent-nschain1"]
        for lane in first:
            _toggle(page, lane)
        assert [_mode(page, x) for x in first] == ["interleaved"] * 3
        # The turn's fourth visible lane sits behind "+1 more branch" (P7).
        expect(page.locator("[data-lane-ref='agent-nsintr01']")).to_be_hidden()
        page.locator(".mn-bmore").click()
        _toggle(page, "agent-nsintr01")
        assert _mode(page, "agent-nsintr01") == "interleaved"
        assert _mode(page, "agent-nsmid001") == "folded"  # least recently selected
        assert _visible_count(page, "agent-nsmid001") == 0
        assert [_mode(page, x) for x in first[1:]] == ["interleaved"] * 2
        # Re-selecting the evicted one now folds nsmid002.
        _toggle(page, "agent-nsmid001")
        assert _mode(page, "agent-nsmid002") == "folded"
        assert (
            sum(
                _mode(page, lane) == "interleaved"
                for lane in page.evaluate(
                    "[...document.querySelectorAll('[data-lane-id]')].map(e => e.dataset.laneId)"
                )
            )
            == 3
        )

    def test_turns_are_capped_separately(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        fork = _fork_lane(page)
        for lane in (A, B, fork, C, D):  # three in turn 1, two in turn 2
            if _mode(page, lane) == "folded":
                _toggle(page, lane)
        assert all(_mode(page, x) == "interleaved" for x in (A, B, fork, C, D))


# ------------------------------------------------------- global control


class TestGlobalControl:
    def test_switches_every_lane_and_persists(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        seg = page.locator(".mn-branches")
        expect(seg).to_be_visible()
        main_btn = seg.locator("[data-mn-branches='main']")
        inter_btn = seg.locator("[data-mn-branches='interleaved']")
        expect(main_btn).to_have_attribute("aria-pressed", "true")
        inter_btn.click()
        lanes = page.evaluate(
            "[...document.querySelectorAll('[data-lane-id]')].map(e => e.dataset.laneId)"
        )
        assert len(lanes) == 5
        assert all(_mode(page, x) == "interleaved" for x in lanes)  # ≤ 3 per turn here
        expect(inter_btn).to_have_attribute("aria-pressed", "true")
        expect(main_btn).to_have_attribute("aria-pressed", "false")
        assert page.evaluate(f"localStorage.getItem('{BRANCHES_KEY}')") == "interleaved"
        # Survives a reload.
        page.reload()
        page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
        assert all(_mode(page, x) == "interleaved" for x in lanes)
        # A per-lane change leaves no segment on.
        _toggle(page, A)
        expect(inter_btn).to_have_attribute("aria-pressed", "false")
        expect(main_btn).to_have_attribute("aria-pressed", "false")
        main_btn.click()
        assert all(_mode(page, x) == "folded" for x in lanes)
        expect(main_btn).to_have_attribute("aria-pressed", "true")

    def test_interleaved_takes_the_first_three_per_turn(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["nested"])
        page.locator("[data-mn-branches='interleaved']").click()
        ranks = page.evaluate(
            """() => [...document.querySelectorAll('[data-lane-id]')]
                .map(e => [e.dataset.laneId, +e.dataset.laneRank])"""
        )
        assert len(ranks) == 6
        for lane, rank in ranks:
            assert _mode(page, lane) == ("interleaved" if rank <= 3 else "folded"), (
                lane,
                rank,
            )

    def test_hidden_without_branches(self, clean: Page, tmp_path: Path):
        page = clean
        out = _render(
            TEST_DATA / "representative_messages.jsonl", tmp_path / "r.html", "R"
        )
        _open(page, out)
        assert page.locator(".mn-branches").is_hidden()


# ------------------------------------------------------- nested + forks


class TestNestedAndForks:
    def test_nested_lane_opens_its_parent_and_forks_from_it(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, C)
        _toggle(page, D)
        assert _mode(page, C) == "interleaved" and _mode(page, D) == "interleaved"
        assert _visible_count(page, D) > 0
        rail_c, rail_d = _rail(page, C), _rail(page, D)
        x_c = _numbers(rail_c["parts"]["lane"]["d"])[0]
        fork_d = _numbers(rail_d["parts"]["fork"]["d"])
        assert fork_d[0] == pytest.approx(x_c, abs=0.5)  # from the parent's slot
        assert _numbers(rail_d["parts"]["lane"]["d"])[0] > x_c
        merge_d = _numbers(rail_d["parts"]["merge"]["d"])
        assert merge_d[-1] == pytest.approx(x_c, abs=0.5)  # back into the parent
        # Folding the parent folds the child.
        _toggle(page, C)
        assert _mode(page, D) == "folded" and _visible_count(page, D) == 0

    def test_nested_selection_opens_the_parent(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["nested"])
        # The nested lane's control is only reachable once its parent is open;
        # search/deep links go through the same path (see TestReveal).
        _toggle(page, "agent-nsmid002")
        _toggle(page, "agent-nsleaf22")
        assert _visible_count(page, "agent-nsleaf22") > 0
        _toggle(page, "agent-nsmid002")
        assert _mode(page, "agent-nsleaf22") == "folded"

    def test_fork_lane(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["fork"])
        fork = _fork_lane(page)
        head = page.locator(f"[data-lane-id='{fork}']")
        assert not head.is_visible()
        # The earliest branch continues main as a slim "rewound" marker.
        cont = page.locator("[data-lane-continues]")
        expect(cont).to_be_visible()
        marker = page.evaluate(
            "getComputedStyle(document.querySelector('[data-lane-continues] > .header'), '::before').content"
        )
        assert "rewound" in marker
        label = page.locator(f"[data-lane-ref='{fork}'] .mn-bfold")
        assert label.get_attribute("data-label").startswith(
            "⑂ " + _head_attr(page, fork, "data-lane-name")
        )
        assert set(_rail(page, fork)["parts"]) == {"stub"}
        label.click()
        expect(head).to_be_visible()
        assert (
            _visible_count(page, fork)
            == page.locator(f"#transcript .message[data-lane='{fork}']").count()
        )
        rail = _rail(page, fork)
        assert set(rail["parts"]) == {"fork", "lane"}  # forks don't merge
        last = page.evaluate(
            """(lane) => [...document.querySelectorAll(`#transcript .message[data-lane="${lane}"]`)]
                .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0].id""",
            fork,
        )
        assert _numbers(rail["parts"]["lane"]["d"])[-1] == pytest.approx(
            page.evaluate(DOT_Y, last), abs=1.5
        )
        stamps = [ms for _lane, ms, _id in page.evaluate(VISUAL_ORDER)]
        assert stamps == sorted(stamps)


# ------------------------------------------------------ parity / triggers


class TestParity:
    def test_filter_hides_interleaved_cards_and_restores_them(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        shown = _visible_count(page, A)
        assert shown > 0
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="sidechain"]').click()
        _settle(page)
        assert _visible_count(page, A) == 0
        # The lane is still drawn spawn → merge (both rows are main cards).
        assert set(_rail(page, A)["parts"]) == {"fork", "lane", "merge"}
        page.locator('.filter-toggle[data-type="sidechain"]').click()
        _settle(page)
        assert _visible_count(page, A) == shown
        stamps = [ms for _lane, ms, _id in page.evaluate(VISUAL_ORDER)]
        assert stamps == sorted(stamps)

    def test_timeline_follows_the_filter(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-timeline", timeout=30000)
        page.wait_for_selector(".vis-item", timeout=10000)
        groups = ".vis-label.timeline-group-sidechain"
        assert page.locator(groups).count() == 1
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="sidechain"]').click()
        page.wait_for_timeout(300)
        assert page.locator(groups).count() == 0

    def test_folding_a_turn_hides_its_lanes_and_rail(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        turn = _head_attr(page, A, "data-lane-turn")
        page.locator(f"#msg-{turn} > .fold-bar .fold-one-level").click()
        _settle(page)
        assert _visible_count(page, A) == 0
        assert _rail(page, A)["paths"] == [] and _rail(page, B)["paths"] == []
        # Unfold all levels (one level re-folds each child, as in classic).
        page.locator(f"#msg-{turn} > .fold-bar .fold-all-levels").click()
        _settle(page)
        assert _visible_count(page, A) > 0
        assert _rail(page, A)["paths"]

    def test_search_opens_the_lane_holding_a_match(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        assert _mode(page, A) == "folded"
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("96 matches in 9 files")
        page.wait_for_function(f"window.claudeLogDag.mode('{A}') === 'interleaved'")
        expect(
            page.locator(f"#transcript .message.search-match[data-lane='{A}']").first
        ).to_be_visible()

    def test_hash_link_into_a_folded_lane(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        target = page.evaluate(
            f"document.querySelector('#transcript .message[data-lane=\"{B}\"]').id"
        )
        page.goto(pages["demo"].as_uri() + "#" + target)
        page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
        assert _mode(page, B) == "interleaved"
        expect(page.locator(f"#{target}")).to_be_in_viewport()
        # And via hashchange on an already open page.
        other = page.evaluate(
            f"document.querySelector('#transcript .message[data-lane=\"{A}\"]').id"
        )
        page.evaluate(f"location.hash = '#{other}'")
        page.wait_for_function(f"window.claudeLogDag.mode('{A}') === 'interleaved'")
        expect(page.locator(f"#{other}")).to_be_visible()

    def test_live_update_relayouts(self, clean: Page, pages: dict[str, Path]):
        page = clean
        _open(page, pages["demo"])
        _toggle(page, A)
        # A wholesale swap (live_update.js's fallback path) with one card more.
        added = page.evaluate(
            """() => {
                const old = document.getElementById('transcript');
                const next = old.cloneNode(true);
                // Engine state is not markup: a server render has none of it.
                next.querySelectorAll('.mn-bctls').forEach(el => el.remove());
                next.querySelectorAll('[style]').forEach(el => {
                    el.style.removeProperty('grid-row'); el.style.removeProperty('--dag-tag');
                });
                next.querySelectorAll('*').forEach(el => [...el.classList]
                    .filter(c => c.startsWith('dag-')).forEach(c => el.classList.remove(c)));
                const mains = next.querySelectorAll('.message[data-lane="main"]:not(.session-header)');
                const last = mains[mains.length - 1].parentElement;
                const copy = last.cloneNode(true);
                copy.querySelectorAll('[id]').forEach(el => { el.id = el.id + '-new'; });
                copy.querySelector('.message').classList.add('live-new');
                last.after(copy);
                old.replaceWith(next);
                window.claudeLogRehydrate(next);
                return copy.querySelector('.message').id;
            }"""
        )
        _settle(page)
        assert page.evaluate(
            "document.querySelector('.mn-stage.dag-on #transcript') !== null"
        )
        # Every card of the new tree has its row; lane A is still interleaved
        # and its control is back.
        missing = page.evaluate(
            "[...document.querySelectorAll('#transcript .message')].filter(el => !el.style.gridRow).map(el => el.id)"
        )
        assert missing == []
        assert _mode(page, A) == "interleaved" and _visible_count(page, A) > 0
        expect(page.locator(f"[data-lane-ref='{A}'] .mn-bfold")).to_have_attribute(
            "aria-expanded", "true"
        )
        expect(page.locator(f"[id='{added}']")).to_be_visible()
        tops = page.evaluate(
            """(id) => [document.getElementById(id).getBoundingClientRect().top,
                document.getElementById(id.replace(/-new$/, '')).getBoundingClientRect().top]""",
            added,
        )
        assert tops[0] > tops[1]  # placed right after the card it was copied from
        # The new tree is observed: folding through the fold bar relayouts.
        assert set(_rail(page, A)["parts"]) == {"fork", "lane", "merge"}

    def test_live_patch_restores_a_replaced_spawn_cards_control(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        spawn = _lane_card_id(page, A, "data-lane-from")
        page.evaluate(
            """(id) => { const card = document.getElementById(id);
                const copy = card.cloneNode(true);
                copy.querySelectorAll('.mn-bctls').forEach(el => el.remove());
                copy.className = copy.className.replace(/\\bdag-\\S+/g, '');
                copy.removeAttribute('style');
                card.replaceWith(copy); window.claudeLogRehydrate(copy); }""",
            spawn,
        )
        _settle(page)
        expect(
            page.locator(f"#{spawn} > .mn-bctls [data-lane-ref='{A}']")
        ).to_have_count(1)
        assert page.locator(f"#{spawn}").evaluate("el => el.style.gridRow") != ""


class TestLayout:
    def test_no_horizontal_scroll_on_a_phone(self, clean: Page, pages: dict[str, Path]):
        page = clean
        page.set_viewport_size({"width": 375, "height": 800})
        _open(page, pages["demo"])
        for mode in ("main", "interleaved"):
            page.locator(f"[data-mn-branches='{mode}']").click()
            _settle(page)
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            ), mode

    def test_no_javascript_renders_nested(self, playwright, pages: dict[str, Path]):
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context(java_script_enabled=False)
            page = context.new_page()
            page.goto(pages["demo"].as_uri())
            assert page.locator(".mn-stage.dag-on").count() == 0
            assert page.locator(".mn-branches").is_hidden()
            assert page.locator(".mn-bctls").count() == 0
            # Sub-agent transcripts render nested under their spawning result.
            card = page.locator(f"#transcript .message[data-lane='{A}']").first
            expect(card).to_be_visible()
            nested = page.evaluate(
                """(lane) => { const card = document.querySelector(`#transcript .message[data-lane="${lane}"]`);
                    const box = card.closest('.children');
                    return [getComputedStyle(box).display, parseFloat(getComputedStyle(box).borderLeftWidth)]; }""",
                A,
            )
            assert nested[0] == "block" and nested[1] == 2
            # The fork branch is there too, as a branch header.
            expect(page.locator("[data-lane-kind='fork']")).to_be_visible()
        finally:
            browser.close()
