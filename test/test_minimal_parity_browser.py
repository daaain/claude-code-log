"""Parity sweep and smoke test for the minimal theme (P8).

work/minimal-theme-dag.md P8; dev-docs/minimal-theme.md § 10. CLAUDE.md asks
that whatever the filter hides in the transcript it hides in the timeline;
the minimal theme adds two more axes — fold depth and the branch mode — so
the sweep walks every filter toggle × every fold depth × every branch mode
with the timeline open, and checks the same invariants each time:

- no card the filter or the search hides is visible;
- the timeline shows exactly the cards the filter and search leave (an item
  is shown iff its group is visible and it is not individually hidden);
- with the DAG engine on, every card has its grid row, and the filter
  changed no lane's mode;
- turning the toggle back on restores exactly the cards shown before.

Then a search into a folded lane (it opens, per branch mode) and a live
update with a filter set (the swapped-in markup is filtered too).

The smoke test renders real projects, clicks through every branch mode,
fold depth and colour scheme, opens a lane each way and fails on any page
error or console error.

The browser context (and ``file://`` localStorage) is shared by every
browser test: stored choices are cleared before and after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from test.dag_demo_fixture import write_dag_demo
from test.test_minimal_dag_browser import _render

pytestmark = pytest.mark.browser

TEST_DATA = Path(__file__).parent / "test_data"
REAL = TEST_DATA / "real_projects"
KEYS = (
    "claude-code-log:branches",
    "claude-code-log:fold-depth",
    "claude-code-log:theme",
)
MODES = ("main", "interleaved", "columns")
DEPTHS = ("prompts", "steps", "all")

# The invariants above, evaluated in the page; returns a list of problems.
CHECK = """() => {
    const problems = [];
    const cards = [...document.querySelectorAll('.message:not(.session-header)')];
    const hiddenBy = el => el.classList.contains('filtered-hidden')
        || el.classList.contains('search-hidden');
    cards.forEach(el => {
        if (hiddenBy(el) && el.checkVisibility()) problems.push('shown although hidden: ' + el.id);
    });
    if (document.querySelector('.mn-stage.dag-on')) {
        document.querySelectorAll('#transcript .message').forEach(el => {
            if (!el.style.gridRow) problems.push('no grid row: ' + el.id);
        });
    }
    const tl = window.timeline;
    if (tl && tl.itemsData) {
        const groups = new Map(tl.groupsData.get().map(g => [g.id, g.visible !== false]));
        tl.itemsData.get().forEach(item => {
            const el = cards[item.id];
            if (!el) { problems.push('timeline item without a card: ' + item.id); return; }
            const shown = groups.get(item.group) !== false
                && !(item.className || '').split(/\\s+/).includes('timeline-filtered-hidden');
            if (shown === hiddenBy(el)) {
                problems.push('timeline ' + (shown ? 'shows ' : 'hides ') + el.id + ' (' + item.group + ')');
            }
        });
    }
    return problems;
}"""

SHOWN = """() => [...document.querySelectorAll('#transcript .message')]
    .filter(el => el.checkVisibility()).map(el => el.id)"""

MODES_OF = """() => [...document.querySelectorAll('[data-lane-id]')]
    .map(el => el.getAttribute('data-lane-id'))
    .map(id => [id, window.claudeLogDag.mode(id)])"""


# A wholesale live swap (live_update.js's fallback path): the server's
# markup — no engine state, no filter or search classes, no highlights.
SWAP = """() => {
    const old = document.getElementById('transcript');
    const next = old.cloneNode(true);
    next.querySelectorAll('.mn-bctls, .dag-chrome').forEach(el => el.remove());
    next.querySelectorAll('[style]').forEach(el => {
        el.style.removeProperty('grid-row'); el.style.removeProperty('grid-column');
        el.style.removeProperty('--dag-tag');
    });
    next.querySelectorAll('*').forEach(el => [...el.classList]
        .filter(c => c.startsWith('dag-') || c === 'filtered-hidden' || c.startsWith('search-'))
        .forEach(c => el.classList.remove(c)));
    next.querySelectorAll('.search-highlight').forEach(m => m.replaceWith(m.textContent));
    old.replaceWith(next);
    window.claudeLogRehydrate(next);
}"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("parity")
    return {
        "demo": _render(write_dag_demo(tmp / "demo"), tmp / "demo.html", "Demo"),
        "worktrees": _render(
            REAL / "-experiments-worktrees", tmp / "worktrees.html", "Worktrees"
        ),
        "coderabbit": _render(
            REAL / "-Users-dain-workspace-coderabbit-review-helper",
            tmp / "coderabbit.html",
            "Coderabbit",
        ),
        "ideas": _render(REAL / "-experiments-ideas", tmp / "ideas.html", "Ideas"),
    }


def _clear(page: Page) -> None:
    page.evaluate("(keys) => keys.forEach(k => localStorage.removeItem(k))", list(KEYS))


@pytest.fixture
def clean(page: Page):
    yield page
    try:
        _clear(page)
    except Exception:  # page already closed
        pass


def _open(page: Page, path: Path, branches: str = "main") -> None:
    page.goto(path.as_uri())
    _clear(page)
    page.evaluate(
        "(b) => localStorage.setItem('claude-code-log:branches', b)", branches
    )
    page.reload()
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")


def _settle(page: Page) -> None:
    """Let the filter's, the timeline's (50ms) and the engine's work land."""
    page.wait_for_timeout(80)
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def _check(page: Page, where: object) -> None:
    problems = page.evaluate(CHECK)
    assert problems == [], (where, problems[:10])


def _open_timeline(page: Page) -> None:
    page.locator("#toggleTimeline").click()
    page.wait_for_selector(".vis-timeline", timeout=30000)
    page.wait_for_function("window.timeline && window.timeline.itemsData")


def _toggles(page: Page) -> list[str]:
    return page.evaluate(
        "[...document.querySelectorAll('.filter-toggle')]"
        ".filter(t => t.checkVisibility()).map(t => t.dataset.type)"
    )


def _depth(page: Page, depth: str) -> None:
    page.locator(f"[data-mn-depth='{depth}']").click()
    _settle(page)


class TestParitySweep:
    @pytest.mark.parametrize("mode", MODES)
    def test_filter_x_depth_x_timeline(
        self, clean: Page, pages: dict[str, Path], mode: str
    ):
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches=mode)
        _open_timeline(page)
        page.locator("#filterMessages").click()
        _settle(page)
        _check(page, (mode, "initial"))
        lanes = page.evaluate(MODES_OF)
        toggles = _toggles(page)
        assert {"user", "assistant", "tool", "sidechain"} <= set(toggles)
        for depth in DEPTHS:
            _depth(page, depth)
            _check(page, (mode, depth))
            baseline = page.evaluate(SHOWN)
            for toggle in toggles:
                button = page.locator(f'.filter-toggle[data-type="{toggle}"]')
                button.click()
                _settle(page)
                _check(page, (mode, depth, "off", toggle))
                # Filters write classes, never lane modes.
                assert page.evaluate(MODES_OF) == lanes, (mode, depth, toggle)
                button.click()
                _settle(page)
                _check(page, (mode, depth, "on", toggle))
                assert page.evaluate(SHOWN) == baseline, (mode, depth, toggle)
        # And with several toggles off at once, across the depths.
        for toggle in ("sidechain", "tool"):
            page.locator(f'.filter-toggle[data-type="{toggle}"]').click()
        for depth in DEPTHS:
            _depth(page, depth)
            _check(page, (mode, depth, "two off"))

    @pytest.mark.parametrize("mode", MODES)
    def test_search_reveals_a_match_in_a_folded_lane(
        self, clean: Page, pages: dict[str, Path], mode: str
    ):
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches=mode)
        lane = "agent-a000audit"
        if mode != "main":
            # Fold it by hand first: a search must open it again.
            page.locator("[data-mn-branches='main']").click()
            _settle(page)
        assert page.evaluate(f"window.claudeLogDag.mode('{lane}')") == "folded"
        _open_timeline(page)
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("96 matches in 9 files")
        page.wait_for_function(
            f"window.claudeLogDag.mode('{lane}') !== 'folded'", timeout=10000
        )
        _settle(page)
        match = page.locator(f"#transcript .message.search-match[data-lane='{lane}']")
        assert match.first.evaluate("el => el.checkVisibility()")
        _check(page, (mode, "search"))
        page.locator("#searchInput").fill("")
        _settle(page)
        _check(page, (mode, "search cleared"))

    @pytest.mark.parametrize("mode", MODES)
    def test_a_live_update_keeps_the_filter(
        self, clean: Page, pages: dict[str, Path], mode: str
    ):
        """A wholesale swap brings the server's markup — no ``filtered-hidden``
        anywhere. The page must filter it again (P8 fix: classic re-applies
        nothing; the minimal theme does, on rehydrate)."""
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches=mode)
        _open_timeline(page)
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="tool"]').click()
        _settle(page)
        _check(page, (mode, "before"))
        page.evaluate(SWAP)
        page.wait_for_function(
            "() => document.querySelector('.message.tool_use.filtered-hidden') !== null"
        )
        _settle(page)
        _check(page, (mode, "after a swap"))

    @pytest.mark.parametrize("mode", MODES)
    def test_a_live_update_keeps_the_search_quietly(
        self, clean: Page, pages: dict[str, Path], mode: str
    ):
        """An active search is re-run on the swapped-in markup — the same
        matches, the rest hidden again — without scrolling the reader away,
        also with a filter set (whose re-application re-runs the search)."""
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches=mode)
        _open_timeline(page)
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="tool"]').click()
        page.locator("#searchInput").fill("palette")
        page.wait_for_function(
            "() => document.querySelectorAll('.message.search-match').length > 0"
        )
        page.wait_for_timeout(400)  # the filter observer's re-search settles
        _settle(page)
        _check(page, (mode, "searched"))
        matches = page.evaluate(
            "[...document.querySelectorAll('.message.search-match')].map(el => el.id)"
        )
        page.evaluate("window.scrollTo(0, 0)")
        _settle(page)
        page.evaluate(SWAP)
        page.wait_for_function(
            "() => document.querySelectorAll('.message.search-match').length > 0"
        )
        page.wait_for_timeout(400)
        _settle(page)
        _check(page, (mode, "after a swap"))
        assert (
            page.evaluate(
                "[...document.querySelectorAll('.message.search-match')].map(el => el.id)"
            )
            == matches
        )
        assert page.evaluate("window.scrollY") == 0  # nothing navigated


# Console messages that are not the page's fault: the timeline's library is
# fetched from unpkg (spec § 1.1), which a sandbox may block.
BENIGN = ("unpkg.com", "net::ERR_", "Failed to load resource")


class TestSmoke:
    @pytest.mark.parametrize("name", ["worktrees", "coderabbit", "ideas", "demo"])
    def test_click_through_without_errors(
        self, clean: Page, pages: dict[str, Path], name: str
    ):
        page = clean
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

        def on_console(msg: Any) -> None:
            if msg.type == "error" and not any(b in msg.text for b in BENIGN):
                errors.append(f"console: {msg.text}")

        page.on("console", on_console)
        page.set_viewport_size({"width": 1400, "height": 900})
        _open(page, pages[name])
        lanes = page.evaluate(
            "[...document.querySelectorAll('[data-lane-id]')].map(el => el.getAttribute('data-lane-id'))"
        )
        has_branches = page.locator(".mn-branches:not([hidden])").count() == 1
        assert has_branches == bool(lanes)
        for theme in ("light", "dark", "auto"):
            page.locator(f"[data-mn-theme='{theme}']").click()
            for depth in DEPTHS:
                _depth(page, depth)
                for mode in MODES if lanes else ():
                    page.locator(f"[data-mn-branches='{mode}']").click()
                    _settle(page)
                    _check(page, (name, theme, depth, mode))
                    page.mouse.wheel(0, 2500)
                _check(page, (name, theme, depth))
        if lanes:
            # One lane through every per-lane mode from its own control.
            page.locator("[data-mn-branches='main']").click()
            _settle(page)
            lane = lanes[0]
            ctl = page.locator(f"[data-lane-ref='{lane}']")
            ctl.locator(".mn-bfold").click()  # interleave
            _settle(page)
            assert page.evaluate(f"window.claudeLogDag.mode('{lane}')") == "interleaved"
            ctl.locator(".mn-bcol").click()  # to a column
            _settle(page)
            assert page.evaluate(f"window.claudeLogDag.mode('{lane}')") == "column"
            page.locator(
                f".dag-chrome[data-col='{lane}'] [data-col-act='collapse']"
            ).click()
            _settle(page)
            assert page.evaluate(f"window.claudeLogDag.mode('{lane}')") == "strip"
            page.locator(f".dag-chrome[data-col='{lane}'] .dag-strip").click()
            _settle(page)
            page.locator(
                f".dag-chrome[data-col='{lane}'] [data-col-act='interleave']"
            ).click()
            _settle(page)
            assert page.evaluate(f"window.claudeLogDag.mode('{lane}')") == "interleaved"
            _check(page, (name, "per lane"))
        # The timeline opens over every mode without an error.
        _open_timeline(page)
        _settle(page)
        _check(page, (name, "timeline"))
        assert errors == [], errors
