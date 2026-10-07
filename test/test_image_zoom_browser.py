"""Browser tests for the click-to-zoom image dialog (``image_zoom.js``).

The fixture is synthetic: a user message with an uploaded 2400×1600 PNG
(sized down by both themes) and an assistant answer whose Markdown shows
the same picture from a file beside the page, once on its own, once inside
a link, and a small image that is already at its natural size.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import BrowserType, Page, ViewportSize, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

pytestmark = pytest.mark.browser

BIG = (2400, 1600)
SMALL = (40, 30)
VIEWPORT: ViewportSize = {"width": 1000, "height": 700}
DARK_BG = "rgb(20, 22, 26)"  # minimal --bg in dark

DIALOG = "dialog.cc-zoom"
UPLOADED = "#transcript img.uploaded-image"
MARKDOWN_BIG = "#transcript img[alt='big']"


def _png(width: int, height: int) -> bytes:
    """A checkerboard PNG, so a pan visibly moves the picture."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    def row(phase: int) -> bytes:
        cells = [
            (b"\x30\x60\xa0" if (x // 100 + phase) % 2 else b"\xe0\xe8\xf0")
            for x in range(width)
        ]
        return b"\x00" + b"".join(cells)

    rows = [row(0), row(1)]
    raw = b"".join(rows[(y // 100) % 2] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _entry(uuid: str, parent: str | None, role: str, content: list[Any]) -> dict:
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        message.update(id=f"m-{uuid}", type="message", model="claude")
    return {
        "type": role,
        "timestamp": "2026-01-01T10:00:00.000Z",
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/workspace",
        "sessionId": "s",
        "version": "1.0.0",
        "uuid": uuid,
        "message": message,
    }


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("zoom")
    big = _png(*BIG)
    (tmp / "big.png").write_bytes(big)
    (tmp / "small.png").write_bytes(_png(*SMALL))
    image = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(big).decode("ascii"),
        },
    }
    markdown = (
        "![big](big.png)\n\n"
        "[![linked](big.png)](#zoom-link-target)\n\n"
        "![small](small.png)"
    )
    entries = [
        _entry("u1", None, "user", [{"type": "text", "text": "A screenshot."}, image]),
        _entry("a1", "u1", "assistant", [{"type": "text", "text": markdown}]),
    ]
    source = tmp / "zoom.jsonl"
    source.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    out: dict[str, Path] = {}
    for theme in ("classic", "minimal"):
        html = generate_html(load_transcript(source, silent=True), "Zoom", theme=theme)
        out[theme] = tmp / f"zoom-{theme}.html"
        out[theme].write_text(html, encoding="utf-8")
    return out


def _open(page: Page, path: Path, selector: str = UPLOADED) -> None:
    page.set_viewport_size(VIEWPORT)
    page.goto(path.as_uri())
    image = page.locator(selector).first
    image.scroll_into_view_if_needed()
    expect(image).to_have_js_property("complete", True)
    image.click()
    expect(page.locator(DIALOG)).to_be_visible()


GEOMETRY = """() => {
    const dialog = document.querySelector('dialog.cc-zoom');
    const frame = dialog.querySelector('.cc-zoom-frame').getBoundingClientRect();
    const img = dialog.querySelector('img').getBoundingClientRect();
    return {
        vw: document.documentElement.clientWidth, vh: window.innerHeight,
        frame: {l: frame.left, t: frame.top, r: frame.right, b: frame.bottom,
                w: frame.width, h: frame.height},
        img: {l: img.left, t: img.top, r: img.right, b: img.bottom, w: img.width, h: img.height},
    };
}"""


def _geometry(page: Page) -> dict[str, Any]:
    return page.evaluate(GEOMETRY)


def _assert_framed(g: dict[str, Any]) -> None:
    """The dialog is at most 90% of the viewport and the image covers it."""
    assert g["frame"]["w"] <= 0.9 * g["vw"] + 0.5
    assert g["frame"]["h"] <= 0.9 * g["vh"] + 0.5
    assert g["img"]["l"] <= g["frame"]["l"] + 0.5
    assert g["img"]["t"] <= g["frame"]["t"] + 0.5
    assert g["img"]["r"] >= g["frame"]["r"] - 0.5
    assert g["img"]["b"] >= g["frame"]["b"] - 0.5


REFITTED = """() => {
    const r = document.querySelector('.cc-zoom-frame').getBoundingClientRect();
    return r.width <= 0.9 * document.documentElement.clientWidth + 0.5
        && r.height <= 0.9 * window.innerHeight + 0.5;
}"""


def _resize(page: Page, width: int, height: int) -> None:
    """Resize, then wait for the dialog to refit: `resize` is dispatched
    with the next frame, not by the viewport change itself."""
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_function(REFITTED)


def _wheel(page: Page, delta: int, times: int) -> None:
    g = _geometry(page)
    page.mouse.move(
        g["frame"]["l"] + g["frame"]["w"] / 3, g["frame"]["t"] + g["frame"]["h"] / 3
    )
    for _ in range(times):
        page.mouse.wheel(0, delta)


@pytest.mark.parametrize("theme", ["classic", "minimal"])
class TestOpenAndClose:
    def test_a_click_opens_it_fitted(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        _open(page, pages[theme])
        g = _geometry(page)
        _assert_framed(g)
        # Fit-to-frame: the whole picture shows, at the frame's size.
        assert abs(g["img"]["w"] - g["frame"]["w"]) <= 1
        assert abs(g["img"]["h"] - g["frame"]["h"]) <= 1

    def test_escape_closes_it(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        _open(page, pages[theme])
        page.keyboard.press("Escape")
        expect(page.locator(DIALOG)).to_be_hidden()

    def test_a_click_outside_the_frame_closes_it(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        _open(page, pages[theme])
        page.mouse.click(5, 5)
        expect(page.locator(DIALOG)).to_be_hidden()

    def test_a_click_inside_the_frame_does_not(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        _open(page, pages[theme])
        g = _geometry(page)
        page.mouse.click(g["frame"]["l"] + 20, g["frame"]["b"] - 20)
        expect(page.locator(DIALOG)).to_be_visible()

    def test_the_close_button_closes_it_and_focus_returns(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        _open(page, pages[theme])
        close = page.get_by_role("button", name="Close")
        expect(close).to_be_visible()
        close.click()
        expect(page.locator(DIALOG)).to_be_hidden()
        expect(page.locator(UPLOADED)).to_be_focused()


class TestZoomAndPan:
    def test_the_wheel_zooms_to_natural_size_and_no_further(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        _open(page, pages["minimal"])
        _wheel(page, -100, 3)
        mid = _geometry(page)
        assert mid["frame"]["w"] < mid["img"]["w"] < BIG[0]
        _wheel(page, -400, 20)
        g = _geometry(page)
        assert abs(g["img"]["w"] - BIG[0]) <= 1
        _assert_framed(g)

    def test_the_wheel_zooms_out_to_fit_and_no_further(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        _open(page, pages["minimal"])
        _wheel(page, -400, 20)
        _wheel(page, 400, 20)
        g = _geometry(page)
        assert abs(g["img"]["w"] - g["frame"]["w"]) <= 1
        assert abs(g["img"]["l"] - g["frame"]["l"]) <= 1
        assert abs(g["img"]["t"] - g["frame"]["t"]) <= 1

    def test_a_drag_pans_and_the_image_keeps_covering_the_frame(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        _open(page, pages["minimal"])
        _wheel(page, -400, 20)
        before = _geometry(page)
        cx = before["frame"]["l"] + before["frame"]["w"] / 2
        cy = before["frame"]["t"] + before["frame"]["h"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx - 150, cy - 100, steps=5)
        page.mouse.up()
        moved = _geometry(page)
        assert moved["img"]["l"] == pytest.approx(before["img"]["l"] - 150, abs=1)
        assert moved["img"]["t"] == pytest.approx(before["img"]["t"] - 100, abs=1)
        # Far past the edge: clamped, never uncovering the frame.
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 3000, cy + 3000, steps=5)
        page.mouse.up()
        g = _geometry(page)
        _assert_framed(g)
        assert abs(g["img"]["l"] - g["frame"]["l"]) <= 1
        assert abs(g["img"]["t"] - g["frame"]["t"]) <= 1
        # A drag that ends outside the frame does not close the dialog.
        expect(page.locator(DIALOG)).to_be_visible()

    def test_a_resize_refits_and_keeps_zoom_and_pan_in_bounds(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        _open(page, pages["minimal"])
        _wheel(page, -400, 20)
        _resize(page, 600, 500)
        expect(page.locator(DIALOG)).to_be_visible()
        g = _geometry(page)
        _assert_framed(g)
        assert g["img"]["w"] <= BIG[0] + 1
        # Growing back: the old frame already fits, so wait for the refit
        # itself — the frame regrows to the new 90% bound (1260 × 840 here,
        # the picture being 3:2).
        page.set_viewport_size({"width": 1400, "height": 1000})
        page.wait_for_function(
            "() => document.querySelector('.cc-zoom-frame')"
            ".getBoundingClientRect().width > 1200"
        )
        g = _geometry(page)
        _assert_framed(g)
        assert g["frame"]["w"] == pytest.approx(
            min(0.9 * g["vw"], 0.9 * g["vh"] * 1.5), abs=2
        )


class TestWhatOpens:
    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_a_wide_markdown_image_stays_in_its_card_and_opens(
        self, page: Page, pages: dict[str, Path], theme: str
    ) -> None:
        """A Markdown image has no class of its own; both themes still size
        it down to its message, which is what makes it zoomable."""
        page.set_viewport_size(VIEWPORT)
        page.goto(pages[theme].as_uri())
        inside = page.locator(MARKDOWN_BIG).evaluate(
            """img => {
                const i = img.getBoundingClientRect();
                const m = img.closest('.message').getBoundingClientRect();
                return i.width > 0 && i.left >= m.left - 0.5 && i.right <= m.right + 0.5;
            }"""
        )
        assert inside
        _open(page, pages[theme], MARKDOWN_BIG)
        _assert_framed(_geometry(page))

    def test_an_image_at_its_natural_size_does_not_open(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        page.set_viewport_size(VIEWPORT)
        page.goto(pages["minimal"].as_uri())
        small = page.locator("#transcript img[alt='small']")
        small.hover()
        expect(small).not_to_have_class("cc-zoomable")
        small.click()
        expect(page.locator(DIALOG)).to_have_count(0)

    def test_a_large_image_shows_the_zoom_cursor(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        page.set_viewport_size(VIEWPORT)
        page.goto(pages["minimal"].as_uri())
        image = page.locator(UPLOADED)
        image.hover()
        expect(image).to_have_css("cursor", "zoom-in")

    def test_an_image_inside_a_link_follows_the_link(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        page.set_viewport_size(VIEWPORT)
        page.goto(pages["minimal"].as_uri())
        page.locator("#transcript img[alt='linked']").click()
        expect(page).to_have_url(pages["minimal"].as_uri() + "#zoom-link-target")
        expect(page.locator(DIALOG)).to_have_count(0)

    def test_an_image_added_later_is_zoomable(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        """What a live-update patch does to the DOM: the handlers are
        delegated, so a message inserted after load needs no wiring."""
        page.set_viewport_size(VIEWPORT)
        page.goto(pages["minimal"].as_uri())
        page.evaluate(
            """() => {
                const node = document.createElement('div');
                node.className = 'message assistant';
                node.innerHTML = '<div class="content"><img alt="late" src="big.png"></div>';
                document.getElementById('transcript').appendChild(node);
            }"""
        )
        late = page.locator("#transcript img[alt='late']")
        late.scroll_into_view_if_needed()
        expect(late).to_have_js_property("complete", True)
        late.click()
        expect(page.locator(DIALOG)).to_be_visible()


def test_the_dialog_follows_the_dark_scheme(page: Page, pages: dict[str, Path]) -> None:
    _open(page, pages["minimal"])
    page.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
    expect(page.locator(".cc-zoom-frame")).to_have_css("background-color", DARK_BG)


def test_focus_opens_on_the_frame_and_tab_reaches_the_close_button(
    page: Page, pages: dict[str, Path]
) -> None:
    """No focus ring on the × after a mouse open, yet keyboard-reachable."""
    _open(page, pages["minimal"])
    expect(page.locator(".cc-zoom-frame")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Close")).to_be_focused()


def test_the_close_button_stays_in_the_viewport_corner(
    browser_type: BrowserType,
    browser_type_launch_args: dict[str, Any],
    pages: dict[str, Path],
) -> None:
    """The × belongs to the page, not to the picture: in the corner the 90%
    frame leaves free, wherever a zoom or a pan has moved the image, and
    clear of the page's scrollbar.

    Playwright starts Chromium with ``--hide-scrollbars``, which leaves the
    page no scrollbar to sit under; this test launches one with a real
    scrollbar, as desktop browsers have."""
    browser = browser_type.launch(
        **browser_type_launch_args, ignore_default_args=["--hide-scrollbars"]
    )
    try:
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(pages["minimal"].as_uri())
        assert (
            page.evaluate("() => innerWidth - document.documentElement.clientWidth") > 0
        )
        image = page.locator(UPLOADED)
        image.scroll_into_view_if_needed()
        expect(image).to_have_js_property("complete", True)
        image.click()
        expect(page.locator(DIALOG)).to_be_visible()
        close = page.get_by_role("button", name="Close")

        def corner() -> tuple[float, float, float, float]:
            box = close.bounding_box()
            assert box is not None
            return box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]

        before = corner()
        left, top, right, bottom = before
        g = _geometry(page)
        # In the visible corner: near the right edge, yet not under the page's
        # vertical scrollbar (``vw`` is the width the scrollbar leaves).
        assert g["vw"] - 30 < right <= g["vw"]
        assert top < 30
        # Outside the frame: it never covers the picture.
        assert left >= g["frame"]["r"] or bottom <= g["frame"]["t"]
        _wheel(page, -400, 20)
        page.mouse.move(g["frame"]["l"] + 300, g["frame"]["t"] + 300)
        page.mouse.down()
        page.mouse.move(g["frame"]["l"] + 100, g["frame"]["t"] + 100, steps=5)
        page.mouse.up()
        assert corner() == before
        close.click()
        expect(page.locator(DIALOG)).to_be_hidden()
    finally:
        browser.close()


WHEEL = """([deltaY, deltaMode]) => {
    const frame = document.querySelector('.cc-zoom-frame');
    const r = frame.getBoundingClientRect();
    frame.dispatchEvent(new WheelEvent('wheel', {
        deltaY, deltaMode, bubbles: true, cancelable: true,
        clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
    }));
    return document.querySelector('dialog.cc-zoom img').getBoundingClientRect().width;
}"""
LINE, PAGE = 1, 2  # WheelEvent.DOM_DELTA_LINE / DOM_DELTA_PAGE


class TestWheelDeltaModes:
    """Firefox reports wheel steps in lines (deltaMode 1) on Linux and
    Windows, where Chromium reports pixels; a page step (mode 2) also
    exists. Each must zoom as much as the pixels it stands for."""

    def _zoomed_width(
        self, page: Page, pages: dict[str, Path], delta: float, mode: int
    ) -> float:
        _open(page, pages["minimal"])
        return page.evaluate(WHEEL, [delta, mode])

    def test_a_line_step_zooms_like_its_pixels(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        # One Firefox notch: three lines, of 16px each.
        pixels = self._zoomed_width(page, pages, -48, 0)
        lines = self._zoomed_width(page, pages, -3, LINE)
        assert pixels > _geometry(page)["frame"]["w"] + 10
        assert lines == pytest.approx(pixels, abs=1)

    def test_a_page_step_zooms_like_a_frame_height(
        self, page: Page, pages: dict[str, Path]
    ) -> None:
        _open(page, pages["minimal"])
        frame_h = _geometry(page)["frame"]["h"]
        pixels = self._zoomed_width(page, pages, -0.5 * frame_h, 0)
        pages_ = self._zoomed_width(page, pages, -0.5, PAGE)
        assert pages_ == pytest.approx(pixels, abs=1)

    @pytest.mark.parametrize("mode", [LINE, PAGE])
    def test_the_caps_hold_in_every_mode(
        self, page: Page, pages: dict[str, Path], mode: int
    ) -> None:
        _open(page, pages["minimal"])
        fit = _geometry(page)["frame"]["w"]
        assert page.evaluate(WHEEL, [-1000, mode]) == pytest.approx(BIG[0], abs=1)
        assert page.evaluate(WHEEL, [1000, mode]) == pytest.approx(fit, abs=1)
