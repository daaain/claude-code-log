"""Browser tests for the minimal theme's project index and archive search page.

The two pages share the transcript's Auto / Light / Dark toggle and its
stored choice (`claude-code-log:theme`), so a choice made on any page holds
on the others of the same origin — every page opened from disk, or every
page a `claude-code-log serve` serves. Also: the index's rows keep the
session finder working, dates follow the viewer's time zone, search hits
are drawn as transcript rows, and neither page scrolls sideways on a phone.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.html.renderer import generate_projects_index_html

TEST_DATA = Path(__file__).parent / "test_data"
THEME_KEY = "claude-code-log:theme"
DARK_BG = "rgb(20, 22, 26)"  # --bg #14161a
LIGHT_BG = "rgb(255, 255, 255)"  # --bg #ffffff
DARK_FG = "rgb(228, 230, 233)"  # --fg #e4e6e9
PROJECT = "-home-u-testproj"


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two projects converted in the minimal theme, with a search index."""
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.search import ensure_index

    projects = tmp_path_factory.mktemp("minimal-pages") / "projects"
    for name, source in (
        (PROJECT, "representative_messages.jsonl"),
        ("-home-u-edgecases", "edge_cases.jsonl"),
    ):
        project = projects / name
        project.mkdir(parents=True)
        shutil.copy(TEST_DATA / source, project / "session.jsonl")
    process_projects_hierarchy(projects, silent=True, theme="minimal")
    conn = sqlite3.connect(get_cache_db_path(projects))
    ensure_index(conn)
    conn.close()
    return projects


@pytest.fixture
def server(archive: Path) -> Iterator[Any]:
    from claude_code_log.api import SearchApi
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.server import ArchiveServer

    api = SearchApi(get_cache_db_path(archive))
    with ArchiveServer(archive, api_routes=api.routes(), port=0) as running:
        yield running


@pytest.fixture
def clean(page: Page) -> Iterator[Page]:
    """The persistent context shares localStorage between tests: start and
    end every test with no stored choice (file:// and the served origin)."""
    yield page
    try:
        page.evaluate(f"localStorage.removeItem('{THEME_KEY}')")
    except Exception:  # page closed or on about:blank
        pass
    page.emulate_media(color_scheme="light")


def _open(page: Page, url: str) -> None:
    page.goto(url)
    page.evaluate(f"localStorage.removeItem('{THEME_KEY}')")
    page.reload()


def _bg(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


def _no_sideways_scroll(page: Page) -> None:
    widths = page.evaluate(
        "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    assert widths[0] <= widths[1], widths


def _search(page: Page, server: Any, query: str) -> None:
    page.goto(f"{server.url}/search.html?q={query}")
    page.wait_for_selector("#app[data-ready]", timeout=10000)
    page.wait_for_selector(".search-result-item.mn-hit", timeout=10000)


@pytest.mark.browser
class TestToggle:
    def test_index_toggle_sets_stores_and_survives_reload(
        self, clean: Page, archive: Path
    ) -> None:
        page = clean
        page.emulate_media(color_scheme="light")
        _open(page, (archive / "index.html").as_uri())
        html = page.locator("html")
        auto = page.locator("[data-mn-theme='auto']")
        dark = page.locator("[data-mn-theme='dark']")
        light = page.locator("[data-mn-theme='light']")

        expect(auto).to_have_attribute("aria-pressed", "true")
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        assert _bg(page) == LIGHT_BG

        dark.click()
        expect(html).to_have_attribute("data-theme", "dark")
        expect(dark).to_have_attribute("aria-pressed", "true")
        assert _bg(page) == DARK_BG
        assert page.evaluate(f"localStorage.getItem('{THEME_KEY}')") == "dark"
        page.reload()
        expect(html).to_have_attribute("data-theme", "dark")
        assert _bg(page) == DARK_BG

        light.click()
        expect(html).to_have_attribute("data-theme", "light")
        assert _bg(page) == LIGHT_BG
        auto.click()
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        assert page.evaluate(f"localStorage.getItem('{THEME_KEY}')") is None

    def test_auto_follows_the_system(self, clean: Page, archive: Path) -> None:
        page = clean
        page.emulate_media(color_scheme="dark")
        _open(page, (archive / "index.html").as_uri())
        assert _bg(page) == DARK_BG
        page.emulate_media(color_scheme="light")
        assert _bg(page) == LIGHT_BG

    def test_search_page_toggle_with_results(
        self, clean: Page, archive: Path, server: Any
    ) -> None:
        page = clean
        page.emulate_media(color_scheme="light")
        _open(page, f"{server.url}/search.html")
        _search(page, server, "Bash")
        assert _bg(page) == LIGHT_BG
        page.locator("[data-mn-theme='dark']").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        assert _bg(page) == DARK_BG
        excerpt = page.locator(".mn-hit .search-result-excerpt").first
        assert excerpt.evaluate("e => getComputedStyle(e).color") == DARK_FG

    def test_search_page_toggle_without_a_server(
        self, clean: Page, archive: Path
    ) -> None:
        """Opened from disk the page shows its setup panel — and the toggle,
        which lives in the header, not in the hidden search form."""
        page = clean
        _open(page, (archive / "search.html").as_uri())
        page.wait_for_selector("#setup:not([hidden])", timeout=10000)
        page.locator("[data-mn-theme='dark']").click()
        assert _bg(page) == DARK_BG


@pytest.mark.browser
class TestChoiceCarriesOver:
    def test_index_to_transcript_and_back_from_disk(
        self, clean: Page, archive: Path
    ) -> None:
        page = clean
        page.emulate_media(color_scheme="light")
        _open(page, (archive / "index.html").as_uri())
        page.locator("[data-mn-theme='dark']").click()

        # The project's name links to its combined transcript.
        page.locator(f".project-name a[href^='{PROJECT}/']").click()
        page.wait_for_load_state()
        assert page.url.endswith("combined_transcripts.html")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(page.locator("[data-mn-theme='dark']")).to_have_attribute(
            "aria-pressed", "true"
        )
        assert _bg(page) == DARK_BG

        page.locator("[data-mn-theme='light']").click()
        page.goto((archive / "index.html").as_uri())
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        expect(page.locator("[data-mn-theme='light']")).to_have_attribute(
            "aria-pressed", "true"
        )

    def test_index_to_session_and_search_when_served(
        self, clean: Page, archive: Path, server: Any
    ) -> None:
        page = clean
        page.emulate_media(color_scheme="light")
        _open(page, f"{server.url}/index.html")
        page.locator("[data-mn-theme='dark']").click()

        # A session row (inside the project's <details>) opens its page.
        project = page.locator(".mn-project").filter(
            has=page.locator(f"a[href^='{PROJECT}/']")
        )
        project.locator("summary.mn-prow").click(position={"x": 4, "y": 8})
        project.locator(".mn-srow").first.click()
        page.wait_for_load_state()
        assert re.search(r"/session-[^/]+\.html$", page.url), page.url
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

        page.goto(f"{server.url}/index.html")
        page.locator(".mn-archive-link").click()
        page.wait_for_load_state()
        assert page.url.endswith("/search.html")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        assert _bg(page) == DARK_BG


@pytest.mark.browser
class TestPhoneWidth:
    def test_index_without_sideways_scroll(self, clean: Page, archive: Path) -> None:
        page = clean
        page.set_viewport_size({"width": 375, "height": 800})
        _open(page, (archive / "index.html").as_uri())
        page.evaluate(
            "document.querySelectorAll('details.mn-pdetails').forEach(d => d.open = true)"
        )
        _no_sideways_scroll(page)
        # With the finder's results open too.
        page.fill("#searchInput", "test")
        expect(page.locator("#searchResultsPanel")).to_be_visible()
        _no_sideways_scroll(page)

    def test_search_page_without_sideways_scroll(
        self, clean: Page, archive: Path, server: Any
    ) -> None:
        page = clean
        page.set_viewport_size({"width": 375, "height": 800})
        _search(page, server, "Bash")
        _no_sideways_scroll(page)
        # Gutter above the text: the row's date sits left of its snippet's
        # right edge, inside the viewport.
        box = page.locator(".mn-hit .mn-hgut").first.bounding_box()
        assert box is not None and box["x"] + box["width"] <= 375
        page.goto((archive / "search.html").as_uri())
        page.wait_for_selector("#setup:not([hidden])", timeout=10000)
        _no_sideways_scroll(page)


@pytest.mark.browser
class TestRows:
    def test_session_finder_still_finds(self, clean: Page, archive: Path) -> None:
        """The index's finder indexes the minimal rows (a session's first
        prompt here) and lists its results under the bar."""
        page = clean
        _open(page, (archive / "index.html").as_uri())
        page.fill("#searchInput", "decorators")
        panel = page.locator("#searchResultsPanel")
        expect(panel).to_be_visible()
        expect(panel.locator(".search-result-item").first).to_be_visible()
        # The archive link stays on the finder's line, above the results.
        link = page.locator(".mn-archive-link").bounding_box()
        results = panel.bounding_box()
        assert link is not None and results is not None
        assert link["y"] < results["y"]

    def test_sessions_are_rows_in_a_details(self, clean: Page, archive: Path) -> None:
        page = clean
        _open(page, (archive / "index.html").as_uri())
        project = page.locator(".mn-project").first
        rows = project.locator(".mn-srow")
        expect(rows.first).to_be_hidden()
        project.locator("summary.mn-prow").click(position={"x": 4, "y": 8})
        expect(rows.first).to_be_visible()
        dot = rows.first.evaluate(
            "r => getComputedStyle(r, '::before').backgroundColor"
        )
        user = page.evaluate(
            "getComputedStyle(document.body).getPropertyValue('--user').trim()"
        )
        assert dot == _rgb(user)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", rows.first.locator(".mn-sd").inner_text()
        )

    def test_search_hits_are_role_coloured_rows(
        self, clean: Page, archive: Path, server: Any
    ) -> None:
        page = clean
        _open(page, f"{server.url}/search.html")
        _search(page, server, "Bash")
        hits = page.locator(".search-result-item.mn-hit")
        assert hits.count() > 0
        for i in range(hits.count()):
            hit = hits.nth(i)
            colours = hit.evaluate(
                """h => [getComputedStyle(h.querySelector('a'), '::before').backgroundColor,
                         getComputedStyle(h.querySelector('.mn-hrole')).color]"""
            )
            if "mn-k-thinking" not in (hit.get_attribute("class") or ""):
                assert colours[0] == colours[1], colours
            assert re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", hit.locator(".mn-hd").inner_text()
            )
            assert re.fullmatch(r"\d{2}:\d{2}", hit.locator(".mn-ht").inner_text())
        # The classic deep-link hook: every row is one link to its message.
        href = page.locator(".search-result-item a").first.get_attribute("href")
        assert href and "uuid=" in href

    def test_dates_follow_the_viewer_time_zone(
        self, playwright, tmp_path: Path
    ) -> None:
        """UTC on the server; pages.js rewrites them in local time."""
        summaries = [
            {
                "name": "-tz",
                "html_file": "-tz/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 2,
                "last_modified": 1700000000.0,
                "earliest_timestamp": "2025-01-01T20:00:00Z",
                "latest_timestamp": "2025-01-02T20:30:00Z",
                "sessions": [
                    {
                        "id": "tz000000",
                        "first_timestamp": "2025-01-02T20:30:00Z",
                        "message_count": 2,
                        "first_user_message": "late evening in UTC",
                    }
                ],
            }
        ]
        page_file = tmp_path / "index.html"
        page_file.write_text(
            generate_projects_index_html(summaries, theme="minimal"), encoding="utf-8"
        )
        assert "2025-01-01 – 2025-01-02" in page_file.read_text(encoding="utf-8")

        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(timezone_id="Asia/Tokyo").new_page()
            page.goto(page_file.as_uri())
            when = page.locator(".mn-pmeta .mn-when")
            expect(when).to_have_text("2025-01-02 – 2025-01-03")
            assert "GMT+9" in (when.get_attribute("title") or "")
            page.locator("summary.mn-prow").click(position={"x": 4, "y": 8})
            expect(page.locator(".mn-sd")).to_have_text("2025-01-03")
            expect(page.locator(".mn-st")).to_have_text("05:30")
        finally:
            browser.close()


def _rgb(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgb({r}, {g}, {b})"
