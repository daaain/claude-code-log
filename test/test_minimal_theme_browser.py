"""Browser tests for the minimal theme's look, toolbar and colour scheme (P3a).

Covers work/minimal-theme-dag.md § 1.3 and § 1.5: the Auto / Light / Dark
toggle (``data-theme`` on ``<html>``, persisted in localStorage, following
``prefers-color-scheme`` in Auto), and that every classic floating control
is reachable — and still works — from the minimal toolbar. Also guards the
two places the minimal row layout could silently break classic behaviour:
cards hidden by the filter or search must stay hidden although the theme
makes every card a grid, and a phone-width page must not scroll sideways.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

TEST_DATA = Path(__file__).parent / "test_data"
REPRESENTATIVE = TEST_DATA / "representative_messages.jsonl"
EDGE_CASES = TEST_DATA / "edge_cases.jsonl"
SIDECHAIN = TEST_DATA / "sidechain.jsonl"

THEME_KEY = "claude-code-log:theme"
DARK_BG = "rgb(20, 22, 26)"  # --bg #14161a
LIGHT_BG = "rgb(255, 255, 255)"  # --bg #ffffff

# Every classic floating control, by the id the page's scripts bind to.
TOOLBAR_IDS = (
    "filterMessages",
    "toggleTimeline",
    "followUpdates",
    "resumeSession",
    "toggleDetails",
    "toggleUserView",
    "toggleDebug",
)


def _render(tmp_path: Path, source: Path, name: str = "minimal.html") -> Path:
    html = generate_html(
        load_transcript(source, silent=True), "Minimal", theme="minimal"
    )
    out = tmp_path / name
    out.write_text(html, encoding="utf-8")
    return out


def _open(page: Page, path: Path) -> None:
    """Load a page with no stored colour-scheme choice.

    The browser context is shared by every browser test (persistent, for
    the HTTP cache), and so is file:// localStorage — clear the key before
    and after each visit so no test inherits another's choice.
    """
    page.goto(path.as_uri())
    page.evaluate(f"localStorage.removeItem('{THEME_KEY}')")
    page.reload()


def _body_bg(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


@pytest.fixture
def clean_theme(page: Page):
    yield page
    try:
        page.evaluate(f"localStorage.removeItem('{THEME_KEY}')")
    except Exception:  # page already closed or navigated away
        pass
    page.emulate_media(color_scheme="light")


@pytest.mark.browser
class TestColourScheme:
    def test_toggle_sets_data_theme_and_survives_reload(
        self, clean_theme: Page, tmp_path: Path
    ) -> None:
        page = clean_theme
        page.emulate_media(color_scheme="light")
        _open(page, _render(tmp_path, REPRESENTATIVE))
        html = page.locator("html")
        auto = page.locator("[data-mn-theme='auto']")
        light = page.locator("[data-mn-theme='light']")
        dark = page.locator("[data-mn-theme='dark']")

        # Auto by default: no attribute, the Auto segment pressed.
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        expect(auto).to_have_attribute("aria-pressed", "true")
        assert _body_bg(page) == LIGHT_BG

        dark.click()
        expect(html).to_have_attribute("data-theme", "dark")
        expect(dark).to_have_attribute("aria-pressed", "true")
        expect(auto).to_have_attribute("aria-pressed", "false")
        assert _body_bg(page) == DARK_BG
        assert page.evaluate(f"localStorage.getItem('{THEME_KEY}')") == "dark"

        page.reload()
        expect(html).to_have_attribute("data-theme", "dark")
        expect(dark).to_have_attribute("aria-pressed", "true")
        assert _body_bg(page) == DARK_BG

        light.click()
        expect(html).to_have_attribute("data-theme", "light")
        assert _body_bg(page) == LIGHT_BG
        page.reload()
        expect(html).to_have_attribute("data-theme", "light")

        # Back to Auto forgets the choice rather than storing "auto".
        auto.click()
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        assert page.evaluate(f"localStorage.getItem('{THEME_KEY}')") is None

    def test_auto_follows_prefers_color_scheme(
        self, clean_theme: Page, tmp_path: Path
    ) -> None:
        page = clean_theme
        page.emulate_media(color_scheme="dark")
        _open(page, _render(tmp_path, REPRESENTATIVE))
        expect(page.locator("html")).not_to_have_attribute(
            "data-theme", re.compile(".*")
        )
        assert _body_bg(page) == DARK_BG
        assert page.evaluate(
            "getComputedStyle(document.documentElement).colorScheme"
        ) == ("light dark")

        page.emulate_media(color_scheme="light")
        assert _body_bg(page) == LIGHT_BG

    def test_explicit_choice_beats_the_system_setting(
        self, clean_theme: Page, tmp_path: Path
    ) -> None:
        page = clean_theme
        page.emulate_media(color_scheme="dark")
        _open(page, _render(tmp_path, REPRESENTATIVE))
        page.locator("[data-mn-theme='light']").click()
        assert _body_bg(page) == LIGHT_BG
        assert page.evaluate(
            "getComputedStyle(document.documentElement).colorScheme"
        ) == ("light")

        page.emulate_media(color_scheme="light")
        page.locator("[data-mn-theme='dark']").click()
        assert _body_bg(page) == DARK_BG
        assert page.evaluate(
            "getComputedStyle(document.documentElement).colorScheme"
        ) == ("dark")

    def test_stored_choice_applies_before_the_body_paints(
        self, clean_theme: Page, tmp_path: Path
    ) -> None:
        """theme_init.js runs in <head>: data-theme is set before <body> exists."""
        page = clean_theme
        path = _render(tmp_path, REPRESENTATIVE)
        page.goto(path.as_uri())
        page.evaluate(f"localStorage.setItem('{THEME_KEY}', 'dark')")
        page.add_init_script(
            """
            document.addEventListener('readystatechange', () => {
                if (!window.__themeAtBody && document.body) {
                    window.__themeAtBody = document.documentElement.getAttribute('data-theme');
                }
            });
            new MutationObserver((_, obs) => {
                if (document.body) {
                    window.__themeAtBody = document.documentElement.getAttribute('data-theme');
                    obs.disconnect();
                }
            }).observe(document, {childList: true, subtree: true});
            """
        )
        page.reload()
        assert page.evaluate("window.__themeAtBody") == "dark"

    def test_unavailable_storage_degrades_to_auto(
        self, page: Page, tmp_path: Path
    ) -> None:
        """Blocked storage (private mode, sandboxed file://) must not break the page."""
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.add_init_script(
            """
            Object.defineProperty(window, 'localStorage', {
                configurable: true,
                get() { throw new DOMException('blocked', 'SecurityError'); },
            });
            """
        )
        page.emulate_media(color_scheme="light")
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        html = page.locator("html")
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        page.locator("[data-mn-theme='dark']").click()
        expect(html).to_have_attribute("data-theme", "dark")
        assert _body_bg(page) == DARK_BG
        # Nothing on the page may throw — the search panel's saved-state
        # restore (search.html) included, since P4 guards it too.
        assert errors == []


@pytest.mark.browser
class TestToolbarReachability:
    def test_every_classic_control_lives_in_the_toolbar(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        for control_id in TOOLBAR_IDS:
            # Exactly one element per id, and it is inside the toolbar.
            assert page.locator(f"#{control_id}").count() == 1, control_id
            assert page.locator(f".mn-toolbar #{control_id}").count() == 1, control_id
        assert page.locator(".mn-toolbar a.scroll-top[href='#title']").count() == 1
        # No classic floating stack is left outside the toolbar.
        assert page.locator(".floating-btn:not(.mn-toolbar *)").count() == 0

    def test_toolbar_is_sticky(self, page: Page, tmp_path: Path) -> None:
        page.set_viewport_size({"width": 1100, "height": 500})
        page.goto(_render(tmp_path, EDGE_CASES).as_uri())
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(100)
        top = page.evaluate(
            "document.querySelector('.mn-toolbar').getBoundingClientRect().top"
        )
        assert top == 0

    def test_search_and_filter_button_opens_the_panel(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        button = page.locator("#filterMessages")
        expect(button).to_be_visible()
        button.click()
        expect(page.locator(".filter-toolbar")).to_be_visible()
        expect(button).to_have_class(re.compile(r"\bactive\b"))
        expect(page.locator("#searchInput")).to_be_focused()
        # The panel stacks under the sticky toolbar, not behind it.
        offsets = page.evaluate(
            """() => [getComputedStyle(document.querySelector('.filter-toolbar')).top,
                      document.querySelector('.mn-toolbar').offsetHeight + 'px']"""
        )
        assert offsets[0] == offsets[1]
        button.click()
        expect(page.locator(".filter-toolbar")).to_be_hidden()

    def test_timeline_button_shows_the_timeline(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path, SIDECHAIN).as_uri())
        button = page.locator("#toggleTimeline")
        expect(button).to_be_visible()
        button.click()
        page.wait_for_selector(".vis-timeline", timeout=30000)
        expect(page.locator("#timeline-container")).to_be_visible()
        expect(button).to_have_class(re.compile(r"\bactive\b"))
        # The script rewrote the button's text; the icon (a ::before mask)
        # is untouched and the emoji text stays invisible.
        assert button.inner_text() == "🗓️"
        assert (
            page.evaluate(
                "getComputedStyle(document.getElementById('toggleTimeline')).fontSize"
            )
            == "0px"
        )
        assert (
            page.evaluate(
                "getComputedStyle(document.getElementById('toggleTimeline'), '::before').content"
            )
            == '""'
        )

    def test_overflow_menu_controls_work(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, EDGE_CASES).as_uri())
        menu = page.locator(".mn-menu")
        expect(menu).to_be_hidden()
        page.locator(".mn-more > summary").click()
        expect(menu).to_be_visible()

        page.locator("#toggleDebug").click()
        expect(page.locator("body")).to_have_class(re.compile(r"\bshow-debug-info\b"))
        expect(page.locator(".debug-info").first).to_be_visible()

        # md/raw is a stored preference shared by every test page: assert
        # that it flips, then flip it back.
        raw_before = page.evaluate("document.body.classList.contains('show-raw-user')")
        page.locator("#toggleUserView").click()
        assert (
            page.evaluate("document.body.classList.contains('show-raw-user')")
            is not raw_before
        )
        page.locator("#toggleUserView").click()
        assert (
            page.evaluate("document.body.classList.contains('show-raw-user')")
            is raw_before
        )

        details = "details.collapsible-details, details.collapsible-code, details.tool-param-collapsible"
        before = page.evaluate(f"document.querySelectorAll('{details}').length")
        assert before > 0
        page.locator("#toggleDetails").click()
        opened = page.evaluate(
            f"Array.from(document.querySelectorAll('{details}')).filter(d => d.open).length"
        )
        assert opened == before
        # The item labels itself from the title the page's script keeps current.
        label = page.evaluate(
            "getComputedStyle(document.getElementById('toggleDetails'), '::before').content"
        )
        assert label == '"Close all details"'

        # A click outside closes the menu; Escape does too.
        page.locator("#title").click()
        expect(menu).to_be_hidden()
        page.locator(".mn-more > summary").click()
        expect(menu).to_be_visible()
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()

    def test_follow_stays_hidden_until_live_updates_start(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        follow = page.locator("#followUpdates")
        expect(follow).to_be_hidden()  # file:// cannot poll
        page.evaluate(
            "document.getElementById('followUpdates').classList.add('live-active')"
        )
        expect(follow).to_be_visible()

    def test_resume_button_copies_the_command(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        page.evaluate(
            """() => {
                window.__copied = null;
                Object.defineProperty(navigator, 'clipboard', {
                    configurable: true,
                    value: { writeText: (text) => { window.__copied = text; return Promise.resolve(); } },
                });
            }"""
        )
        button = page.locator("#resumeSession")
        expect(button).to_be_visible()
        button.click()
        expect(page.locator("#resumeToast")).to_have_class(re.compile(r"\bvisible\b"))
        assert page.evaluate("window.__copied") == button.get_attribute("data-command")
        # The toast sits below the toolbar, not at the classic stack's bottom.
        toast_top = page.evaluate(
            "document.getElementById('resumeToast').getBoundingClientRect().top"
        )
        bar_bottom = page.evaluate(
            "document.querySelector('.mn-toolbar').getBoundingClientRect().bottom"
        )
        assert toast_top >= bar_bottom

    def test_scroll_to_top(self, page: Page, tmp_path: Path) -> None:
        page.set_viewport_size({"width": 1100, "height": 500})
        page.goto(_render(tmp_path, EDGE_CASES).as_uri())
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.locator(".mn-toolbar a.scroll-top").click()
        page.wait_for_function("window.scrollY < 50")


@pytest.mark.browser
class TestRowLayout:
    def test_filter_and_search_still_hide_grid_cards(
        self, page: Page, tmp_path: Path
    ) -> None:
        """Cards are grids in this theme; the classic hide rules must still win."""
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        displays = page.evaluate(
            "Array.from(document.querySelectorAll('#transcript .message.assistant'))"
            ".map(m => getComputedStyle(m).display)"
        )
        assert displays and set(displays) == {"grid"}

        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="assistant"]').click()
        hidden = page.evaluate(
            "Array.from(document.querySelectorAll('#transcript .message.assistant'))"
            ".map(m => [m.classList.contains('filtered-hidden'), getComputedStyle(m).display])"
        )
        assert hidden and all(flag and disp == "none" for flag, disp in hidden)

        page.evaluate(
            """() => {
                const card = document.querySelector('#transcript .message.user');
                card.classList.add('search-hidden');
                window.__searchDisplay = getComputedStyle(card).display;
            }"""
        )
        assert page.evaluate("window.__searchDisplay") == "none"

    def test_timeline_follows_the_filter(self, page: Page, tmp_path: Path) -> None:
        """Filter/timeline parity (CLAUDE.md) holds in the minimal theme."""
        page.goto(_render(tmp_path, SIDECHAIN).as_uri())
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-item", timeout=30000)
        page.wait_for_timeout(500)
        before = page.locator(".vis-item").count()
        page.locator("#filterMessages").click()
        page.locator('.filter-toggle[data-type="sidechain"]').click()
        page.wait_for_timeout(500)
        after = page.locator(".vis-item").count()
        assert 0 < after < before
        sidechain_cards = page.evaluate(
            "Array.from(document.querySelectorAll('#transcript .message.sidechain'))"
            ".map(m => getComputedStyle(m).display)"
        )
        assert sidechain_cards and set(sidechain_cards) == {"none"}

    def test_tool_call_and_output_read_as_one_unit(
        self, page: Page, tmp_path: Path
    ) -> None:
        """P5: the call half's two-line gutter no longer sizes its row — it
        hangs into the output half's empty gutter — so the output box sits
        right under the call, the gutter stays readable and the dot stays on
        the call line."""
        sample = (
            TEST_DATA
            / "real_projects"
            / "-Users-dain-workspace-claude-code-log-sample"
            / "fe869ecb-c176-478f-9734-7e4b8ef12cff.jsonl"
        )
        page.set_viewport_size({"width": 1100, "height": 900})
        page.goto(_render(tmp_path, sample).as_uri())
        geometry = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                    '#transcript .message.tool_use.pair_first')).map(call => {
                const out = call.closest('.message-node').nextElementSibling
                    .querySelector(':scope > .message.pair_last');
                const title = call.querySelector('.mn-title').getBoundingClientRect();
                const time = call.querySelector('.mn-time').getBoundingClientRect();
                const role = call.querySelector('.mn-role').getBoundingClientRect();
                const dot = getComputedStyle(call, '::before');
                const box = call.getBoundingClientRect();
                const body = call.querySelector('.content').getBoundingClientRect();
                return {
                    gap: out.querySelector('.content').getBoundingClientRect().top
                        - (body.height ? body.bottom : title.bottom),
                    timeH: time.height, roleH: role.height,
                    // A one-line call is shorter than its gutter: the role
                    // label then hangs below the call, beside the output.
                    oneLine: !body.height,
                    roleBelowCall: role.bottom > box.bottom,
                    dotTop: parseFloat(dot.marginTop),
                    // A generic title is hidden: the call starts with its body.
                    titleTop: (title.height ? title.top : body.top) - box.top,
                };
            })"""
        )
        assert any(row["oneLine"] for row in geometry)
        for row in geometry:
            assert 0 <= row["gap"] <= 4, row
            assert row["timeH"] > 0 and row["roleH"] > 0, row
            if row["oneLine"]:
                assert row["roleBelowCall"], row  # hangs beside the output
            # The dot's centre sits on the call line's first line.
            assert row["titleTop"] <= row["dotTop"] + 3.5 <= row["titleTop"] + 18

    def test_gutter_shows_role_time_and_tokens(
        self, page: Page, tmp_path: Path
    ) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        card = page.locator("#transcript .message.assistant").first
        expect(card.locator(".mn-role")).to_have_text("Assistant")
        expect(card.locator(".mn-time")).to_have_text(
            re.compile(r"^\d{2}:\d{2}:\d{2}$")
        )
        expect(card.locator(".mn-tok")).to_have_text("25 · 120")
        # The generic "🤖 Assistant" title is redundant with the gutter.
        expect(card.locator(".header > .mn-title")).to_be_hidden()
        # The gutter sits left of the content, the dot between them.
        boxes = page.evaluate(
            """() => {
                const card = document.querySelector('#transcript .message.assistant');
                const gut = card.querySelector('.header-info').getBoundingClientRect();
                const body = card.querySelector('.content').getBoundingClientRect();
                return [gut.right, body.left];
            }"""
        )
        assert boxes[0] <= boxes[1]

    @pytest.mark.parametrize("source", [REPRESENTATIVE, EDGE_CASES, SIDECHAIN])
    def test_no_horizontal_scroll_at_phone_width(
        self, page: Page, tmp_path: Path, source: Path
    ) -> None:
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(_render(tmp_path, source).as_uri())
        # Unfold everything so the widest content is laid out.
        page.evaluate(
            "document.querySelectorAll('#transcript .children').forEach(c => c.style.display = '')"
        )
        widths = page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert widths[0] <= widths[1], widths
