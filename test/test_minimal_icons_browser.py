"""Browser tests for the minimal theme's gutter icons (``html/minimal_icons.py``).

Each row's role label carries a stroke icon from the page's one sprite: it
must actually draw (its ``<use>`` resolves to a symbol: non-zero box), in
the row's role colour (``currentColor`` = ``--rc``) in light and dark, form
one column beside the rail on a wide page, lead the label on a phone and in
a column, show without JavaScript, and survive a live update's wholesale
``#transcript`` swap (the sprite is outside it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.html.renderer import HtmlRenderer
from test.dag_demo_fixture import write_dag_demo

pytestmark = pytest.mark.browser

THEME_KEY = "claude-code-log:theme"
BRANCHES_KEY = "claude-code-log:branches"
DEPTH_KEY = "claude-code-log:fold-depth"

# Every visible gutter icon: its box, the box its <use> draws (the symbol's
# content — zero when the reference does not resolve), its colour and stroke,
# the role label's colour (--rc) and box, and the card's id.
_ICONS = """() => [...document.querySelectorAll('#transcript .mn-role > .mn-ic')]
    .filter(svg => svg.checkVisibility())
    .map(svg => {
        const box = svg.getBoundingClientRect();
        const use = svg.querySelector('use');
        const drawn = use.getBBox();
        const role = svg.parentElement;
        const roleBox = role.getBoundingClientRect();
        const label = document.createRange();
        label.selectNodeContents(role);
        label.setStartAfter(svg);
        const text = label.getBoundingClientRect();
        const style = getComputedStyle(svg);
        return {
            id: svg.closest('.message').id,
            href: use.getAttribute('href'),
            width: box.width, height: box.height,
            left: box.left, right: box.right, top: box.top, bottom: box.bottom,
            drawnW: drawn.width, drawnH: drawn.height,
            color: style.color, stroke: style.stroke, fill: style.fill,
            roleColor: getComputedStyle(role).color,
            roleTop: roleBox.top, roleBottom: roleBox.bottom,
            textLeft: text.left, textRight: text.right,
        };
    })"""


def _render(source: Path, out: Path) -> Path:
    entries, tree = load_directory_transcripts(source, silent=True)
    renderer = HtmlRenderer()
    renderer.theme = "minimal"
    out.write_text(
        renderer.generate(entries, "Icons", session_tree=tree), encoding="utf-8"
    )
    return out


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp = tmp_path_factory.mktemp("icons")
    return _render(write_dag_demo(tmp / "demo"), tmp / "demo.html")


@pytest.fixture
def clean(page: Page):
    yield page
    try:
        page.evaluate(
            f"['{THEME_KEY}', '{BRANCHES_KEY}', '{DEPTH_KEY}']"
            ".forEach(k => localStorage.removeItem(k))"
        )
    except Exception:  # page already closed
        pass
    page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": 1280, "height": 720})


def _open(page: Page, path: Path) -> None:
    page.goto(path.as_uri())
    page.evaluate(
        f"['{THEME_KEY}', '{BRANCHES_KEY}', '{DEPTH_KEY}']"
        ".forEach(k => localStorage.removeItem(k))"
    )
    page.reload()
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")


def _settle(page: Page) -> None:
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def _assert_drawn(icons: list[dict[str, Any]]) -> None:
    assert icons, "no visible icons"
    for icon in icons:
        assert 11 <= icon["width"] <= 14 and icon["width"] == icon["height"], icon
        assert icon["drawnW"] > 4 and icon["drawnH"] > 4, icon  # <use> resolved
        assert icon["fill"] == "none", icon
        assert icon["stroke"] == icon["color"] == icon["roleColor"], icon
        # Vertically within its role line.
        assert icon["top"] >= icon["roleTop"] - 1, icon
        assert icon["bottom"] <= icon["roleBottom"] + 1, icon


class TestIcons:
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_icons_draw_in_the_role_colour(
        self, clean: Page, demo: Path, scheme: str
    ) -> None:
        page = clean
        page.emulate_media(color_scheme="dark" if scheme == "dark" else "light")
        _open(page, demo)
        icons = page.evaluate(_ICONS)
        _assert_drawn(icons)
        # Role colours differ by kind: user, assistant and tool rows at least.
        assert len({icon["color"] for icon in icons}) >= 3
        by_href = {icon["href"]: icon["color"] for icon in icons}
        user = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--user').trim()"
        )
        expected = page.evaluate(
            """(c) => { const s = document.createElement('span'); s.style.color = c;
                document.body.append(s); const v = getComputedStyle(s).color; s.remove(); return v; }""",
            user,
        )
        assert by_href["#mi-user"] == expected

    def test_light_and_dark_differ(self, clean: Page, demo: Path) -> None:
        page = clean
        _open(page, demo)
        light = page.evaluate(_ICONS)[0]["color"]
        page.emulate_media(color_scheme="dark")
        _settle(page)
        dark = page.evaluate(_ICONS)[0]["color"]
        assert light != dark

    def test_icons_form_a_column_beside_the_rail(self, clean: Page, demo: Path) -> None:
        page = clean
        _open(page, demo)
        icons = [
            icon
            for icon in page.evaluate(_ICONS)
            if page.evaluate(
                "(id) => document.getElementById(id).dataset.lane === 'main'",
                icon["id"],
            )
        ]
        assert len(icons) > 5
        rights = {round(icon["right"]) for icon in icons}
        assert len(rights) == 1, rights
        for icon in icons:
            assert icon["textRight"] <= icon["left"] + 0.5, icon  # label before it
        # …inside the gutter column, so left of the rail and its dot.
        gutter_edge = page.evaluate(
            """(id) => { const card = document.getElementById(id);
                return card.getBoundingClientRect().left
                    + parseFloat(getComputedStyle(card).gridTemplateColumns); }""",
            icons[0]["id"],
        )
        assert icons[0]["right"] <= gutter_edge + 0.5

    def test_phone_leads_the_label(self, clean: Page, demo: Path) -> None:
        page = clean
        page.set_viewport_size({"width": 390, "height": 800})
        _open(page, demo)
        icons = page.evaluate(_ICONS)
        _assert_drawn(icons)
        for icon in icons:
            assert icon["right"] <= icon["textLeft"] + 0.5, icon
            assert icon["right"] <= 390
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

    def test_columns_lead_the_label(self, clean: Page, demo: Path) -> None:
        page = clean
        page.set_viewport_size({"width": 1600, "height": 820})
        _open(page, demo)
        page.locator("[data-mn-branches='columns']").click()
        _settle(page)
        in_columns = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message.dag-col .mn-role > .mn-ic')]
                .filter(svg => svg.checkVisibility()).map(svg => {
                    const r = document.createRange(); r.selectNodeContents(svg.parentElement);
                    r.setStartAfter(svg);
                    return [svg.getBoundingClientRect().right, r.getBoundingClientRect().left,
                            svg.querySelector('use').getBBox().width]; })"""
        )
        assert in_columns
        for right, text_left, drawn in in_columns:
            assert right <= text_left + 0.5 and drawn > 4
        # Hairlines: none on a column's first card, right under its head.
        firsts = page.evaluate(
            """() => [...document.querySelectorAll('#transcript .message.dag-cfirst')]
                .map(c => [c.classList.contains('dag-col'), getComputedStyle(c, '::after').content])"""
        )
        assert any(in_col for in_col, _ in firsts)
        assert all(content == "none" for _, content in firsts)

    def test_error_pill_keeps_its_icon(self, clean: Page, demo: Path) -> None:
        page = clean
        _open(page, demo)
        page.locator("[data-mn-branches='interleaved']").click()
        _settle(page)
        pill = page.evaluate(
            """() => { const role = [...document.querySelectorAll('#transcript .message.tool_result.error .mn-role')]
                    .find(el => el.checkVisibility());
                const svg = role.querySelector('.mn-ic');
                const a = role.getBoundingClientRect(), b = svg.getBoundingClientRect();
                return {href: svg.querySelector('use').getAttribute('href'),
                    inside: b.left >= a.left && b.right <= a.right,
                    color: getComputedStyle(svg).color, err: getComputedStyle(role).color}; }"""
        )
        assert pill["href"] == "#mi-tool-error"
        assert pill["inside"] and pill["color"] == pill["err"]

    def test_icons_survive_a_live_swap(self, clean: Page, demo: Path) -> None:
        page = clean
        _open(page, demo)
        before = len(page.evaluate(_ICONS))
        # live_update.js's wholesale path: the server's markup, parsed, its
        # #transcript swapped in, then the rehydrate hooks.
        page.evaluate(
            """(html) => {
                const doc = new DOMParser().parseFromString(html, 'text/html');
                const next = doc.getElementById('transcript');
                document.getElementById('transcript').replaceWith(next);
                window.claudeLogRehydrate(next);
            }""",
            demo.read_text(encoding="utf-8"),
        )
        _settle(page)
        assert page.locator(".mn-sprite").count() == 1
        icons = page.evaluate(_ICONS)
        _assert_drawn(icons)
        assert len(icons) == before

    def test_icons_without_javascript(self, playwright, demo: Path) -> None:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(java_script_enabled=False).new_page()
            page.goto(demo.as_uri())
            icons = page.evaluate(_ICONS)
            _assert_drawn(icons)
            # Nested sub-agent rows (no engine) carry theirs too.
            nested = [
                icon
                for icon in icons
                if page.evaluate(
                    "(id) => document.getElementById(id).dataset.lane !== 'main'",
                    icon["id"],
                )
            ]
            assert nested
        finally:
            browser.close()

    def test_search_and_filter_ignore_the_icons(self, clean: Page, demo: Path) -> None:
        page = clean
        _open(page, demo)
        # The icon adds no text: the label is all a card's role contributes.
        assert page.evaluate(
            """() => [...document.querySelectorAll('#transcript .mn-role')]
                .every(role => role.textContent === role.textContent.trim()
                    && role.querySelector('.mn-ic').textContent === '')"""
        )
