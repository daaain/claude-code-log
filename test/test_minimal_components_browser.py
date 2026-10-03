"""Browser tests for the minimal theme's components in light and dark (P3b).

Covers work/minimal-theme-dag.md P3b: the generated github-dark Pygments
sheet (legible on the dark code box, different from the light colours),
the timeline's colours following the scheme, teammate tints following the
in-page toggle rather than only ``prefers-color-scheme``, the call line
(the gutter names the tool, the row shows its arguments), the compact
session header, and the gutter error pill. Also the classic page's
md/raw preference read, which must not abort the page's scripts when
storage is blocked.

Dark mode is forced by setting ``data-theme`` directly (what the toggle
does), so no test leaves a stored choice behind in the shared browser
context.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_directory_transcripts, load_transcript
from claude_code_log.html.renderer import generate_html

TEST_DATA = Path(__file__).parent / "test_data"
REPRESENTATIVE = TEST_DATA / "representative_messages.jsonl"
EDGE_CASES = TEST_DATA / "edge_cases.jsonl"
TEAMMATES = TEST_DATA / "teammates"

DARK_BG = (20, 22, 26)  # --bg #14161a
DARK_CODE = (29, 32, 37)  # --code #1d2025
LIGHT_CODE = (243, 244, 246)  # --code #f3f4f6

BLOCK_STORAGE = """
Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() { throw new DOMException('blocked', 'SecurityError'); },
});
"""


def _render(tmp_path: Path, source: Path, theme: str = "minimal") -> Path:
    if source.is_dir():
        messages, tree = load_directory_transcripts(source, silent=True)
        html = generate_html(messages, "Components", session_tree=tree, theme=theme)
    else:
        html = generate_html(
            load_transcript(source, silent=True), "Components", theme=theme
        )
    out = tmp_path / f"{source.stem}-{theme}.html"
    out.write_text(html, encoding="utf-8")
    return out


def _rgb(value: str) -> tuple[int, int, int]:
    """``rgb(1, 2, 3)`` / ``rgba(…)`` / ``color(srgb r g b)`` → 0–255 ints."""
    numbers = [float(n) for n in re.findall(r"[\d.]+", value)]
    if value.startswith("color("):
        return (
            round(numbers[0] * 255),
            round(numbers[1] * 255),
            round(numbers[2] * 255),
        )
    return (round(numbers[0]), round(numbers[1]), round(numbers[2]))


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _force(page: Page, scheme: str) -> None:
    page.evaluate(f"document.documentElement.setAttribute('data-theme', '{scheme}')")


def _styles(page: Page, selector: str, *props: str) -> list[str]:
    return page.evaluate(
        """([selector, props]) => {
            const el = document.querySelector(selector);
            if (!el) return null;
            const style = getComputedStyle(el);
            return props.map(p => style.getPropertyValue(p));
        }""",
        [selector, list(props)],
    )


# Pygments tokens present in the representative transcript's Python block.
TOKENS = (".k", ".nf", ".s2", ".nd")


@pytest.mark.browser
class TestPygmentsDark:
    def _token_colours(self, page: Page) -> dict[str, Any]:
        colours: dict[str, Any] = {}
        for token in TOKENS:
            styles = _styles(page, f"#transcript .highlight {token}", "color")
            assert styles is not None, f"no {token} token in the fixture"
            colours[token] = _rgb(styles[0])
        return colours

    @pytest.mark.parametrize("how", ["toggle", "system"])
    def test_tokens_change_and_stay_legible_in_dark(
        self, page: Page, tmp_path: Path, how: str
    ) -> None:
        page.emulate_media(color_scheme="light")
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        light = self._token_colours(page)
        assert _rgb(_styles(page, "#transcript .highlight", "background-color")[0]) == (
            LIGHT_CODE
        )

        if how == "toggle":
            _force(page, "dark")
        else:
            page.emulate_media(color_scheme="dark")
        code_bg = _rgb(_styles(page, "#transcript .highlight", "background-color")[0])
        assert code_bg == DARK_CODE
        dark = self._token_colours(page)
        for token in TOKENS:
            assert dark[token] != light[token], token
            assert _contrast(dark[token], code_bg) >= 4.5, (token, dark[token])
        # github-dark keywords are not bold (the classic light sheet's are).
        assert _styles(page, "#transcript .highlight .k", "font-weight")[0] == "400"

    def test_light_choice_beats_a_dark_system(self, page: Page, tmp_path: Path) -> None:
        page.emulate_media(color_scheme="light")
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        light = self._token_colours(page)
        page.emulate_media(color_scheme="dark")
        _force(page, "light")
        assert self._token_colours(page) == light
        page.emulate_media(color_scheme="light")


@pytest.mark.browser
class TestDarkSamples:
    """The P3b acceptance sample: computed colours of key components in dark."""

    def test_code_diff_and_filter_chip(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        _force(page, "dark")
        assert _rgb(
            _styles(page, "#transcript .content pre", "background-color")[0]
        ) == (DARK_CODE)
        added = _styles(page, "#transcript .diff-added", "color", "background-color")
        assert _rgb(added[0]) == (134, 224, 162)  # --add
        assert _rgb(added[1]) == (20, 41, 28)  # --addbg
        page.locator("#filterMessages").click()
        chip = _styles(
            page,
            '.filter-toggle.active[data-type="user"]',
            "color",
            "background-color",
            "border-top-color",
        )
        assert _rgb(chip[0]) == (228, 230, 233)  # --fg
        assert chip[1] in ("rgba(0, 0, 0, 0)", "transparent")
        assert _luminance(_rgb(chip[2])) < 0.4
        # The type dot carries the role colour.
        dot = page.evaluate(
            """getComputedStyle(document.querySelector(
                '.filter-toggle.active[data-type="user"]'), '::before').backgroundColor"""
        )
        assert _rgb(dot) == (240, 163, 92)  # dark --user


@pytest.mark.browser
class TestTimelineColours:
    def _open(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-timeline .vis-item", timeout=30000)
        page.wait_for_timeout(300)

    def test_timeline_follows_dark(self, page: Page, tmp_path: Path) -> None:
        page.emulate_media(color_scheme="light")
        self._open(page, tmp_path)
        light_label = _rgb(
            _styles(page, ".vis-label.timeline-group-user", "background-color")[0]
        )
        assert _luminance(light_label) > 0.6

        _force(page, "dark")
        container = _styles(page, "#timeline-container", "background-color")
        assert _rgb(container[0]) == DARK_BG
        label = _styles(
            page, ".vis-label.timeline-group-user", "background-color", "color"
        )
        assert _luminance(_rgb(label[0])) < 0.05
        assert _contrast(_rgb(label[1]), _rgb(label[0])) >= 7
        axis = _styles(page, ".vis-time-axis .vis-text", "color")
        assert _contrast(_rgb(axis[0]), DARK_BG) >= 4.5
        item = _styles(
            page, ".vis-item.timeline-item-user", "background-color", "color"
        )
        assert _luminance(_rgb(item[0])) < 0.05
        assert _contrast(_rgb(item[1]), _rgb(item[0])) >= 7
        # Every item, whatever its type, sits on a dark background.
        backgrounds = page.evaluate(
            "Array.from(document.querySelectorAll('.vis-item')).map("
            "i => getComputedStyle(i).backgroundColor)"
        )
        assert backgrounds and all(_luminance(_rgb(bg)) < 0.08 for bg in backgrounds)


@pytest.mark.browser
class TestTeammateTints:
    def test_tints_follow_the_in_page_toggle(self, page: Page, tmp_path: Path) -> None:
        """Classic tints switch on prefers-color-scheme only; minimal follows data-theme."""
        page.emulate_media(color_scheme="light")
        page.goto(_render(tmp_path, TEAMMATES).as_uri())
        selector = "#transcript .teammate-message"
        light = _rgb(_styles(page, selector, "background-color")[0])
        assert _luminance(light) > 0.6
        _force(page, "dark")
        dark = _styles(page, selector, "background-color", "color")
        assert _luminance(_rgb(dark[0])) < 0.05
        assert _contrast(_rgb(dark[1]), _rgb(dark[0])) >= 7

        page.emulate_media(color_scheme="dark")
        _force(page, "light")
        assert _rgb(_styles(page, selector, "background-color")[0]) == light
        page.emulate_media(color_scheme="light")


@pytest.mark.browser
class TestCallLine:
    def test_tool_name_is_not_repeated(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        card = page.locator("#transcript .message.tool_use").filter(
            has=page.locator(".mn-role", has_text="Edit")
        )
        expect(card.locator(".mn-role")).to_have_text("Edit")
        title = card.locator(".header > .mn-title")
        expect(title).to_be_visible()
        expect(title).to_have_text(re.compile(r"Edit /tmp/decorator_example\.py"))
        assert title.inner_text().strip() == "/tmp/decorator_example.py"
        expect(title.locator(".mn-tn")).to_be_hidden()
        expect(title).to_have_attribute("title", re.compile(r"Edit /tmp/decorator"))

    def test_search_still_matches_the_hidden_name(
        self, page: Page, tmp_path: Path
    ) -> None:
        """Search indexes the title's text, the hidden prefix included."""
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        header = page.evaluate(
            "document.querySelector('#transcript .message.tool_use .header span').textContent"
        )
        assert "Edit" in header
        page.locator("#filterMessages").click()
        page.locator("#searchInput").fill("Edit /tmp/decorator_example")
        page.wait_for_timeout(600)
        visible = page.evaluate(
            """Array.from(document.querySelectorAll('#transcript .message'))
                .filter(m => getComputedStyle(m).display !== 'none'
                    && !m.classList.contains('session-header'))
                .map(m => m.querySelector('.mn-role') && m.querySelector('.mn-role').textContent)"""
        )
        assert "Edit" in visible

    def test_error_pill_in_the_gutter(self, page: Page, tmp_path: Path) -> None:
        page.goto(_render(tmp_path, EDGE_CASES).as_uri())
        page.evaluate(
            "document.querySelectorAll('#transcript .children')"
            ".forEach(c => c.style.display = '')"
        )
        _force(page, "dark")
        pill = page.locator("#transcript .message.tool_result.error .mn-role").first
        expect(pill).to_be_visible()
        styles = page.evaluate(
            """(() => { const s = getComputedStyle(document.querySelector(
                '#transcript .message.tool_result.error .mn-role'));
                return [s.color, s.backgroundColor, s.textTransform]; })()"""
        )
        assert _rgb(styles[0]) == (255, 139, 132)  # --err
        assert _rgb(styles[1]) == (58, 27, 27)  # --errbg
        assert styles[2] == "lowercase"


@pytest.mark.browser
class TestSessionHeader:
    def test_summary_is_clamped_and_expands(self, page: Page, tmp_path: Path) -> None:
        page.set_viewport_size({"width": 420, "height": 800})
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        summary = page.locator("#transcript .mn-sh-sum").first
        expect(summary).to_have_attribute("title", re.compile("^User learned about"))
        line = float(_styles(page, "#transcript .mn-sh-sum", "line-height")[0][:-2])
        closed = summary.bounding_box()
        assert closed and closed["height"] <= 2 * line + 1
        toggle = page.locator("#transcript .mn-sh-more").first
        expect(toggle).to_have_text("+ more")
        toggle.click()
        expect(toggle).to_have_text("− less")
        opened = summary.bounding_box()
        assert opened and opened["height"] > closed["height"]
        toggle.click()
        expect(toggle).to_have_text("+ more")
        # Id and model are dim mono metadata, not part of the bold title.
        meta = _styles(page, "#transcript .mn-sh-id", "font-weight", "font-family")
        assert meta[0] == "400" and "mono" in meta[1].lower()

    def test_short_summary_has_no_toggle(self, page: Page, tmp_path: Path) -> None:
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(_render(tmp_path, REPRESENTATIVE).as_uri())
        expect(page.locator("#transcript .mn-sh-sum").first).to_be_visible()
        expect(page.locator("#transcript .mn-sh-more")).to_have_count(0)


@pytest.mark.browser
class TestBlockedStorage:
    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_page_scripts_initialise_without_storage(
        self, page: Page, tmp_path: Path, theme: str
    ) -> None:
        """The md/raw preference read used to throw and abort the handler."""
        page.add_init_script(BLOCK_STORAGE)
        page.goto(_render(tmp_path, REPRESENTATIVE, theme=theme).as_uri())
        # Filter counts and fold bars are set up by the same handler.
        expect(
            page.locator('.filter-toggle[data-type="user"] .count')
        ).not_to_have_text("(0)")
        if theme == "classic":
            assert page.locator(".fold-bar-section.folded").count() > 0
        else:
            # Minimal's default depth (Steps) leaves this fixture with
            # nothing folded; the handler reaching the fold-depth hooks (and
            # applying the default without storage) is the same signal.
            assert page.evaluate("typeof window.claudeLogSyncFoldBar") == "function"
            expect(page.locator("[data-mn-depth='steps']")).to_have_attribute(
                "aria-pressed", "true"
            )
        button = page.locator("#toggleUserView")
        if theme == "minimal":
            page.locator(".mn-more > summary").click()
        errors: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        button.click()
        expect(page.locator("body")).to_have_class(re.compile(r"\bshow-raw-user\b"))
        expect(button).to_have_attribute("title", "Show user messages as Markdown")
        assert errors == []
