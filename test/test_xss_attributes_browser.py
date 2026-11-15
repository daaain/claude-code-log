"""The search pages' result rows keep transcript values out of attributes.

The index page's finder reads each session link back with
``getAttribute('href')`` (which decodes ``&#34;`` to ``"``) and writes it
into result rows as markup; ``serve``'s archive search does the same with
the API's ``link``. A session id holding a quote must stay inside the
``href`` there, and a project directory named ``javascript:…`` must not
become a ``javascript:`` URL. Both themes, both pages.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import process_projects_hierarchy
from test.test_xss_attributes import write_hostile_archive

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        sys.platform == "win32", reason="quotes and colons in file names"
    ),
]

THEMES = ("classic", "minimal")


@pytest.fixture(scope="module")
def archives(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.search import ensure_index

    built: dict[str, Path] = {}
    for theme in THEMES:
        projects = write_hostile_archive(
            tmp_path_factory.mktemp(f"attr-xss-{theme}") / "projects"
        )
        process_projects_hierarchy(projects, silent=True, theme=theme)
        conn = sqlite3.connect(get_cache_db_path(projects))
        ensure_index(conn)
        conn.close()
        built[theme] = projects
    return built


def _assert_inert(page: Page, rows: str) -> None:
    """No result anchor grew a handler, and hovering ran nothing."""
    anchors = page.locator(rows)
    expect(anchors.first).to_be_attached()
    found = page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel)).map(a => ({
            href: a.getAttribute('href'),
            attrs: a.getAttributeNames(),
        }))""",
        rows,
    )
    for anchor in found:
        assert anchor["attrs"] == ["href"], found
        href = anchor["href"]
        assert not href.lower().lstrip().startswith("javascript:"), found
        assert not href.startswith(("/", "\\")), found
    for i in range(anchors.count()):
        anchors.nth(i).hover()
    page.mouse.move(5, 5)
    page.mouse.move(200, 200)
    assert page.evaluate("window.__pwn") is None


@pytest.mark.parametrize("theme", THEMES)
def test_index_finder_rows(page: Page, archives: dict[str, Path], theme: str) -> None:
    page.goto((archives[theme] / "index.html").as_uri())
    page.fill("#searchInput", "findme")
    expect(page.locator("#searchResultsPanel")).to_be_visible()
    # Both quoted-id sessions are among the hits (their previews match);
    # ingestion may have quarantined their ids, so match the project only.
    expect(
        page.locator("#searchResultsContent .search-result-item a")
    ).not_to_have_count(0)
    hrefs = page.eval_on_selector_all(
        "#searchResultsContent .search-result-item a",
        "els => els.map(a => a.getAttribute('href'))",
    )
    assert sum("-tmp-quoted/session-" in h for h in hrefs) == 2, hrefs
    _assert_inert(page, "#searchResultsContent .search-result-item a")


@pytest.fixture
def servers(archives: dict[str, Path]) -> Iterator[dict[str, Any]]:
    from contextlib import ExitStack

    from claude_code_log.api import SearchApi
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.server import ArchiveServer

    with ExitStack() as stack:
        running: dict[str, Any] = {}
        for theme, projects in archives.items():
            api = SearchApi(get_cache_db_path(projects))
            running[theme] = stack.enter_context(
                ArchiveServer(projects, api_routes=api.routes(), port=0)
            )
        yield running


@pytest.mark.parametrize("theme", THEMES)
def test_archive_search_rows(page: Page, servers: dict[str, Any], theme: str) -> None:
    page.goto(f"{servers[theme].url}/search.html?q=findme")
    expect(page.locator("#app[data-ready]")).to_be_attached()
    rows = ".search-result-item a"
    expect(page.locator(rows)).to_have_count(4)
    _assert_inert(page, rows)
