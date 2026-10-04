"""Browser tests for the minimal theme's visual polish (P7c).

work/minimal-theme-dag.md P7c; dev-docs/minimal-theme.md, dev-docs/css-classes.md
§ Minimal theme. Five items agreed with the user, each checked by what a
reader sees:

1. compact spawn rows — a ``Task`` / ``Agent`` spawn is its call line, a
   2-line prompt preview and the branch control; the async launch
   acknowledgement and ``Result ↓`` fold into the control, and nothing
   becomes unreachable (prompt, ids, acknowledgement; no-JS unchanged);
2. fold bars dimmed at rest (≥ 3:1), full strength on hover / keyboard
   focus, the prompt's bar undimmed;
3. the fork-point box and the session navigation's fork group on one line;
4. columns: main wider than branch columns, a readable head, one column per
   screen with snapping on a phone;
5. density: interleaved gutters whose lines never overlap.

The browser context (and ``file://`` localStorage) is shared by every
browser test: stored choices are cleared before and after.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from test.dag_demo_fixture import write_dag_demo
from test.test_minimal_dag_browser import _render

pytestmark = pytest.mark.browser

TEST_DATA = Path(__file__).parent / "test_data"
KEYS = (
    "claude-code-log:branches",
    "claude-code-log:fold-depth",
    "claude-code-log:theme",
)
A = "agent-a000audit"  # async, its notification is the merge row
SPAWN_A = "msg-d-3"


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("polish")
    return {
        "demo": _render(write_dag_demo(tmp / "demo"), tmp / "demo.html", "Demo"),
        "fork": _render(TEST_DATA / "dag_within_fork.jsonl", tmp / "fork.html", "Fork"),
        "real": _render(
            TEST_DATA / "real_projects" / "-experiments-worktrees",
            tmp / "real.html",
            "Worktrees",
        ),
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


def _open(page: Page, path: Path, branches: str = "main", theme: str = "light") -> None:
    page.goto(path.as_uri())
    _clear(page)
    page.evaluate(
        """([b, t]) => { localStorage.setItem('claude-code-log:branches', b);
            localStorage.setItem('claude-code-log:theme', t); }""",
        [branches, theme],
    )
    page.reload()
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")


def _box(locator: Any) -> dict[str, float]:
    box = locator.bounding_box()
    assert box is not None
    return box


def _settle(page: Page) -> None:
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def _line_height(page: Page, selector: str) -> float:
    return page.evaluate(
        """(sel) => { const el = document.querySelector(sel);
            const probe = document.createElement('span');
            probe.textContent = 'x';
            el.appendChild(probe);
            const h = probe.getBoundingClientRect().height;
            probe.remove();
            return h; }""",
        selector,
    )


# WCAG relative luminance contrast of an element's text colour against the
# page background (both computed, so it follows the theme).
CONTRAST = """(el) => {
    const parse = (c) => {
        const m = c.match(/[\\d.]+/g).map(Number);
        return m.slice(0, 3).map(v => v / 255);
    };
    const lum = (rgb) => {
        const [r, g, b] = rgb.map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const fg = lum(parse(getComputedStyle(el).color));
    const bg = lum(parse(getComputedStyle(document.body).backgroundColor));
    const [hi, lo] = fg > bg ? [fg, bg] : [bg, fg];
    return (hi + 0.05) / (lo + 0.05);
}"""


def _token(page: Page, name: str) -> str:
    """A theme token as the browser resolves a colour (rgb(...))."""
    return page.evaluate(
        """(name) => { const probe = document.createElement('span');
            probe.style.color = `var(${name})`;
            document.body.appendChild(probe);
            const c = getComputedStyle(probe).color;
            probe.remove();
            return c; }""",
        name,
    )


# ------------------------------------------------------- 1. spawn rows


class TestCompactSpawnRows:
    def test_an_async_spawn_is_three_lines(self, clean: Page, pages: dict[str, Path]):
        """Call line, prompt, branch control: the launch acknowledgement and
        its Result line are folded into the control."""
        page = clean
        _open(page, pages["demo"])
        rows = page.evaluate(
            """(id) => { const call = document.getElementById(id);
                const result = call.closest('.message-node').nextElementSibling
                    .querySelector(':scope > .message');
                const r = call.getBoundingClientRect();
                return {height: r.height, resultShown: result.checkVisibility(),
                        resultCls: result.className,
                        title: call.querySelector('.mn-title').textContent,
                        text: call.textContent}; }""",
            SPAWN_A,
        )
        line = _line_height(page, f"#{SPAWN_A} > .content")
        # Three lines — the mono call line, the prompt, the control with its
        # 22px buttons — plus the card's padding: under 4.5 prose lines.
        # Before P7c the spawn and its result card took six (≈ 170px).
        assert rows["height"] <= 4.5 * line, (rows, line)
        assert not rows["resultShown"] and "dag-ack" in rows["resultCls"]
        # "Run background" folded into the call line's [async #id].
        assert "[async #a000audit]" in rows["title"]
        assert "Run" not in page.locator(f"#{SPAWN_A} > .content").inner_text()
        # The prompt is still there.
        assert "Find every hard-coded colour" in rows["text"]

    def test_the_control_carries_result_and_acknowledgement(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        ctl = page.locator(f"[data-lane-ref='{A}']")
        note = page.locator(f"[data-lane-id='{A}']").get_attribute("data-lane-to")
        res = ctl.locator(".mn-bres")
        expect(res).to_have_attribute("href", f"#msg-{note}")
        # One line: stats, Result ↓, launched ▸ and Column ⇥ share a top.
        tops = ctl.evaluate(
            "(el) => [...el.children].filter(c => c.checkVisibility()).map(c => Math.round(c.getBoundingClientRect().top + c.getBoundingClientRect().height / 2))"
        )
        assert max(tops) - min(tops) <= 3, tops
        # The acknowledgement is one click away, and back.
        ack = ctl.locator(".mn-back")
        expect(ack).to_have_attribute("aria-expanded", "false")
        ack.click()
        expect(ack).to_have_attribute("aria-expanded", "true")
        card = page.locator(f"#{SPAWN_A}").locator(
            "xpath=ancestor::div[contains(@class,'message-node')][1]/following-sibling::div[1]/div[contains(@class,'message')]"
        )
        expect(card).to_be_visible()
        expect(card).to_contain_text("Async agent launched successfully")
        ack.click()
        expect(card).to_be_hidden()
        # Result ↓ goes to the answer, on the notification card.
        res.click()
        expect(page.locator(f"#msg-{note}")).to_be_in_viewport()
        expect(page.locator(f"#msg-{note}")).to_contain_text("14 files, 412 literals")

    def test_a_long_prompt_is_a_two_line_preview(
        self, clean: Page, pages: dict[str, Path]
    ):
        """Real data: a 39-line teammate prompt shows two lines, and the
        whole prompt is one click away."""
        page = clean
        _open(page, pages["real"])
        details = page.locator(
            "#transcript .message.tool_use > .content > .task-prompt > details"
        ).first
        summary = details.locator(":scope > summary")
        preview = summary.locator(".preview-content")
        line = page.evaluate(
            "(el) => parseFloat(getComputedStyle(el).lineHeight) || 1.4 * parseFloat(getComputedStyle(el).fontSize)",
            preview.element_handle(),
        )
        assert _box(preview)["height"] <= 2 * line + 2
        # The "+N lines" label sits beside the preview, not on a line below.
        label_bottom = page.evaluate(
            "(el) => { const r = el.getBoundingClientRect(); return r.bottom; }",
            summary.element_handle(),
        )
        assert label_bottom - _box(preview)["y"] <= 2 * line + 4
        assert re.match(r"\+ \d+ lines", summary.get_attribute("data-more") or "")
        summary.click()
        expect(details.locator(":scope > .code-full")).to_be_visible()
        expect(details.locator(":scope > .code-full")).to_contain_text("worktree")

    def test_without_javascript_the_result_card_reads_as_before(
        self, playwright: Any, pages: dict[str, Path]
    ):
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(java_script_enabled=False).new_page()
            page.goto(pages["demo"].as_uri())
            ack = page.locator("#transcript .mn-ack").first
            expect(ack).to_be_visible()
            expect(ack).to_contain_text("Async agent launched successfully")
            expect(page.locator("#transcript .mn-async-jump a").first).to_be_visible()
        finally:
            browser.close()


# ---------------------------------------------------------- 2. fold bars


FOLD = """(id) => { const s = document.querySelector(`#${id} > .fold-bar > .fold-bar-section`);
    return s ? getComputedStyle(s).color : null; }"""


class TestFoldBars:
    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_dimmed_at_rest_and_readable(
        self, clean: Page, pages: dict[str, Path], theme: str
    ):
        page = clean
        _open(page, pages["demo"], theme=theme)
        page.mouse.move(0, 0)
        dim, muted = _token(page, "--dim"), _token(page, "--muted")
        assert dim != muted
        # An assistant step's bar: dimmed, yet ≥ 3:1 against the page.
        section = page.locator("#msg-d-2 > .fold-bar > .fold-bar-section").first
        assert page.evaluate(FOLD, "msg-d-2") == dim
        assert section.evaluate(CONTRAST) >= 3.0
        # The prompt's bar keeps its strength.
        assert page.evaluate(FOLD, "msg-d-1") == muted

    def test_full_strength_on_hover_and_keyboard_focus(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        _open(page, pages["demo"])
        dim = _token(page, "--dim")
        lifted = "([fn, id, dim]) => eval(fn)(id) !== dim"
        page.locator("#msg-d-2 > .content").hover()
        # (A short colour transition: wait for it.)
        page.wait_for_function(
            "([fn, id, muted]) => eval(fn)(id) === muted",
            arg=[FOLD, "msg-d-2", _token(page, "--muted")],
        )
        page.mouse.move(0, 0)
        _settle(page)
        page.wait_for_function(
            "([fn, id, dim]) => eval(fn)(id) === dim", arg=[FOLD, "msg-d-2", dim]
        )
        # Keyboard: the sections are focusable buttons; focus lifts the bar
        # and Enter folds like a click.
        section = page.locator("#msg-d-2 > .fold-bar > .fold-bar-section").first
        expect(section).to_have_attribute("tabindex", "0")
        expect(section).to_have_attribute("role", "button")
        section.focus()
        page.wait_for_function(lifted, arg=[FOLD, "msg-d-2", dim])
        children = page.locator("#msg-d-2").locator(
            "xpath=following-sibling::div[contains(@class,'children')][1]"
        )
        expect(children).to_be_visible()
        page.keyboard.press("Enter")
        expect(section).to_have_class(re.compile(r"\bfolded\b"))


# --------------------------------------------------------------- 3. forks


class TestForkDuplication:
    @pytest.mark.parametrize("which", ["fork", "demo"])
    def test_fork_point_box_is_one_line(
        self, clean: Page, pages: dict[str, Path], which: str
    ):
        page = clean
        page.set_viewport_size({"width": 1100, "height": 800})
        _open(page, pages[which])
        box = page.locator("#transcript .fork-point").first
        links = box.locator(".fork-point-branch")
        expect(links).to_have_count(2)
        line = _line_height(page, "#transcript .fork-point")
        assert _box(box)["height"] <= 1.5 * line
        for i in range(2):
            expect(links.nth(i)).to_be_visible()
        # The preview repeating the fork's own card is hidden, still in DOM.
        assert box.locator(".fork-point-preview").count() == 1
        expect(box.locator(".fork-point-preview")).to_be_hidden()
        # Following a link opens the branch it names.
        links.nth(1).click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('[data-lane-kind=\"fork\"]')].some(h => window.claudeLogDag.mode(h.getAttribute('data-lane-id')) !== 'folded')"
        )

    def test_session_nav_fork_group_is_one_line(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        page.set_viewport_size({"width": 1100, "height": 800})
        _open(page, pages["demo"])
        tops = page.evaluate(
            """() => [...document.querySelectorAll('.session-nav > :is(.session-fork-point, .session-branch)')]
                .map(el => Math.round(el.getBoundingClientRect().top))"""
        )
        assert len(tops) == 3 and len(set(tops)) == 1, tops
        # Navigation kept: every branch entry is still a link to its header.
        hrefs = page.evaluate(
            "() => [...document.querySelectorAll('.session-nav .branch-link')].map(a => a.getAttribute('href'))"
        )
        assert hrefs and all(page.locator(h).count() == 1 for h in hrefs)


# ------------------------------------------------------------- 4. columns


CHROME = """() => [...document.querySelectorAll('#transcript > .dag-chrome')].map(el => ({
    col: el.getAttribute('data-col'), strip: el.classList.contains('dag-stripbg'),
    left: el.getBoundingClientRect().left + window.scrollX,
    width: el.getBoundingClientRect().width}))"""


class TestColumns:
    def test_main_is_wider_than_branch_columns(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches="columns")
        chrome = page.evaluate(CHROME)
        main = next(c for c in chrome if c["col"] == "main")
        others = [c for c in chrome if c["col"] != "main" and not c["strip"]]
        assert others and main["width"] >= 520
        assert all(main["width"] > c["width"] for c in others), chrome
        assert all(c["width"] >= 300 for c in others), chrome

    def test_column_head_gives_the_name_its_own_line(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        page.set_viewport_size({"width": 1600, "height": 900})
        _open(page, pages["demo"], branches="columns")
        heads = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .dag-colhead:not(.dag-mainhead)')].map(h => {
                const name = h.querySelector('.dag-colname'), meta = h.querySelector('.dag-colmeta');
                const n = name.getBoundingClientRect(), m = meta.getBoundingClientRect(), r = h.getBoundingClientRect();
                return {label: name.getAttribute('data-label'), title: h.title,
                        nameBottom: n.bottom, metaTop: m.top, nameH: n.height,
                        inside: n.top >= r.top - 0.5 && m.bottom <= r.bottom + 0.5,
                        nameW: n.width, headW: r.width}; })"""
        )
        assert heads
        for head in heads:
            assert head["metaTop"] >= head["nameBottom"] - 0.5, head  # two lines
            assert head["inside"], head
            assert head["nameH"] < 24, head  # one line, truncated if long
            # The full name stays reachable.
            assert head["label"].lstrip("⑂ ") in head["title"], head

    def test_phone_shows_one_column_at_a_time(
        self, clean: Page, pages: dict[str, Path]
    ):
        page = clean
        page.set_viewport_size({"width": 390, "height": 844})
        _open(page, pages["demo"], branches="columns")
        view = page.evaluate("document.documentElement.clientWidth")
        chrome = [c for c in page.evaluate(CHROME) if not c["strip"]]
        assert len(chrome) >= 3
        for c in chrome:
            assert c["width"] == pytest.approx(view - 20, abs=1.5), (c, view)
        assert page.evaluate(
            "getComputedStyle(document.documentElement).scrollSnapType"
        ).startswith("x mandatory")
        # A short swipe past the second column's start snaps to it.
        second = chrome[1]["left"] - 10
        page.evaluate("(x) => window.scrollTo({left: x + 40, top: 0})", second)
        page.wait_for_function(
            "(x) => Math.abs(window.scrollX - x) <= 1.5", arg=second, timeout=5000
        )
        # Back to Main only: the snapping goes with the columns.
        page.locator("[data-mn-branches='main']").click()
        page.wait_for_function(
            "() => !document.querySelector('.mn-stage.dag-panes')"
            " && getComputedStyle(document.documentElement).scrollSnapType === 'none'"
        )


# ------------------------------------------------------------- 5. density


class TestGutters:
    def test_interleaved_gutter_lines_never_overlap(
        self, clean: Page, pages: dict[str, Path]
    ):
        """A call's gutter (time, role) hangs into its result's; the lane tag
        and an error pill now start below it instead of on top of it."""
        page = clean
        _open(page, pages["demo"], branches="interleaved")
        boxes = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('#transcript .message.dag-in').forEach(el => {
                    if (!el.checkVisibility()) return;
                    const info = el.querySelector(':scope > .header > .header-info');
                    if (!info || !info.checkVisibility()) return;
                    [...info.children].forEach(kid => {
                        if (!kid.checkVisibility()) return;
                        const r = kid.getBoundingClientRect();
                        if (r.height) out.push([el.id, kid.className, r.left, r.top, r.right, r.bottom]);
                    });
                    const tag = getComputedStyle(info, '::after');
                    if (tag.content !== 'none' && tag.display !== 'none') {
                        // The pseudo element follows the last child line.
                        const r = info.getBoundingClientRect();
                        out.push([el.id, 'tag', r.left, r.bottom - parseFloat(tag.height), r.right, r.bottom]);
                    }
                });
                return out; }"""
        )
        assert any(b[1] == "tag" for b in boxes)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                if a[0] == b[0]:
                    continue
                overlap_x = min(a[4], b[4]) - max(a[2], b[2])
                overlap_y = min(a[5], b[5]) - max(a[3], b[3])
                assert not (overlap_x > 1 and overlap_y > 1), (a, b)
