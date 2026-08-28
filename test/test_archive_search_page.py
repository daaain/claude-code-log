"""Tests for the archive-search page and its deep links.

The page is generated on every HTML run even though it only *works* when
served, so that the index page's link never dangles and someone who finds
the file gets an explanation rather than a broken search box.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.html.renderer import generate_archive_search_html


@pytest.fixture
def generated_archive(tmp_path: Path) -> Path:
    """Run the real conversion over a small copy of the test data."""
    projects = tmp_path / "projects"
    project = projects / "-home-u-testproj"
    project.mkdir(parents=True)
    shutil.copy(
        Path("test/test_data/representative_messages.jsonl"),
        project / "session.jsonl",
    )
    process_projects_hierarchy(projects, silent=True)
    return projects


def test_conversion_writes_the_search_page(generated_archive: Path) -> None:
    search_page = generated_archive / "search.html"
    assert search_page.exists()
    assert "api/search" in search_page.read_text(encoding="utf-8")


def test_index_page_links_to_the_search_page(generated_archive: Path) -> None:
    index = (generated_archive / "index.html").read_text(encoding="utf-8")
    assert "href='search.html'" in index or 'href="search.html"' in index


def test_combined_transcript_links_to_the_search_page(
    generated_archive: Path,
) -> None:
    """A project page is one level below the index, where `search.html` is,
    and pre-selects the project the reader came from."""
    combined = (
        generated_archive / "-home-u-testproj" / "combined_transcripts.html"
    ).read_text(encoding="utf-8")
    assert "href='../search.html?project=-home-u-testproj'" in combined
    assert "Search inside all transcripts" in combined


def test_single_file_conversion_has_no_dangling_search_link(tmp_path: Path) -> None:
    """No index, no `search.html` — so no link to one."""
    from claude_code_log.converter import convert_jsonl_to_html

    source = tmp_path / "session.jsonl"
    shutil.copy(Path("test/test_data/representative_messages.jsonl"), source)
    output = convert_jsonl_to_html(source)
    assert "search.html" not in output.read_text(encoding="utf-8")


def test_index_page_keeps_its_own_search_box(generated_archive: Path) -> None:
    """The two searches answer different questions and stay separate.

    The index box searches session titles and previews (0.17% of a real
    archive) to find a conversation; the search page searches every message
    body. Merging them behind one input would make the same keystrokes mean
    different things depending on whether a server is running.
    """
    index = (generated_archive / "index.html").read_text(encoding="utf-8")
    assert "searchInput" in index
    assert "buildSearchIndex" in index
    # ...and it must not have been quietly rewired to the API.
    assert "api/search" not in index


def test_search_page_is_self_contained(generated_archive: Path) -> None:
    """No external assets — it has to work over plain loopback."""
    html = (generated_archive / "search.html").read_text(encoding="utf-8")
    assert "<script src=" not in html
    assert "<link rel='stylesheet'" not in html
    assert '<link rel="stylesheet"' not in html


def test_search_page_explains_itself_without_the_api() -> None:
    html = generate_archive_search_html()
    assert "claude-code-log serve" in html
    assert "id='setup'" in html


def test_every_field_group_gets_a_toggle() -> None:
    """The toggles are rendered from `SEARCH_FIELDS`, so adding a group to
    the extractor can't leave the UI a field behind."""
    from claude_code_log.search import SEARCH_FIELD_LABELS, SEARCH_FIELDS

    html = generate_archive_search_html()
    for name in SEARCH_FIELDS:
        assert f"data-field='{name}'" in html, f"no toggle for {name}"
        assert SEARCH_FIELD_LABELS[name] in html
    assert len(re.findall(r"data-field='", html)) == len(SEARCH_FIELDS)


def test_toggles_start_unchecked_in_the_static_page() -> None:
    """Which groups are on is a `serve --search-fields` decision, but the
    page is written at conversion time — so the checked state has to come
    from /api/ping, not from baked-in `checked` attributes."""
    html = generate_archive_search_html()
    fields_block = html[html.index("id='fields'") : html.index("id='field-note'")]
    assert "checked" not in fields_block


def test_session_pages_carry_the_uuid_deep_link_handler(
    generated_archive: Path,
) -> None:
    session_pages = list(generated_archive.glob("*/session-*.html"))
    assert session_pages, "expected at least one session page"
    html = session_pages[0].read_text(encoding="utf-8")
    assert "claudeLogRevealMessageByUuid" in html
    assert "data-uuid" in html


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------


def _seed_index(projects: Path) -> None:
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.search import ensure_index

    conn = sqlite3.connect(get_cache_db_path(projects))
    ensure_index(conn)
    conn.close()


@pytest.mark.browser
class TestArchiveSearchBrowser:
    """Drive the real page against a real server."""

    def _serve(self, projects: Path):
        from claude_code_log.api import SearchApi
        from claude_code_log.cache import get_cache_db_path
        from claude_code_log.server import ArchiveServer

        api = SearchApi(get_cache_db_path(projects))
        return ArchiveServer(projects, api_routes=api.routes(), port=0)

    def test_search_page_shows_setup_instructions_without_a_server(
        self, page: Any, generated_archive: Path
    ) -> None:
        """Opened over file://, the API fetch is refused outright."""
        page.goto(f"file://{generated_archive / 'search.html'}")
        page.wait_for_selector("#setup:not([hidden])", timeout=10000)
        assert page.is_hidden("#app")
        assert "claude-code-log serve" in page.inner_text("#setup")

    def test_search_finds_results_and_updates_the_url(
        self, page: Any, generated_archive: Path
    ) -> None:
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html")
            _wait_ready(page)
            page.fill("#q", "Bash")
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item').length > 0",
                timeout=10000,
            )
            assert "q=Bash" in page.evaluate("location.search")
            assert page.locator(".search-result-excerpt .search-highlight").count() > 0

    def test_partial_words_match_without_a_trailing_star(
        self, page: Any, generated_archive: Path
    ) -> None:
        """A half-typed word finds the whole one, and the highlight covers
        the word that actually matched rather than only the typed prefix."""
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html")
            _wait_ready(page)
            page.fill("#q", "Ba")  # below the prefix floor: whole word only
            page.wait_for_timeout(800)
            assert page.locator(".search-result-item").count() == 0

            page.fill("#q", "Bas")
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item').length > 0",
                timeout=10000,
            )
            highlighted = page.locator(
                ".search-result-excerpt .search-highlight"
            ).first.inner_text()
            assert highlighted.lower().startswith("bas")
            assert len(highlighted) > len("Bas"), (
                f"highlight {highlighted!r} stopped at the typed prefix"
            )

    def test_field_toggles_reflect_the_server_default(
        self, page: Any, generated_archive: Path
    ) -> None:
        """The page is static but the default scope is a `serve` flag, so
        the boxes are set from /api/ping."""
        from claude_code_log.search import DEFAULT_SEARCH_FIELDS

        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html")
            _wait_ready(page)
            assert _checked_fields(page) == list(DEFAULT_SEARCH_FIELDS)
            # Unchanged from the default, so nothing to reset and nothing to
            # spell out in the URL.
            assert page.is_hidden("#reset")
            assert "fields=" not in page.evaluate("location.search")

    def test_unchecking_a_field_narrows_the_search_and_the_url(
        self, page: Any, generated_archive: Path
    ) -> None:
        """`Bash` lives only in tool_input in the fixture, so turning that
        group off is the difference between one hit and none — and the
        narrowed scope survives in the URL and can be reset."""
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html")
            _wait_ready(page)
            page.fill("#q", "Bash")
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item').length > 0",
                timeout=10000,
            )

            page.uncheck("#field-tool_input")
            page.wait_for_selector(".search-no-results", timeout=10000)
            assert "tool_input" not in _checked_fields(page)
            fields = page.evaluate("new URLSearchParams(location.search).get('fields')")
            assert fields and "tool_input" not in fields.split(",")
            assert page.is_visible("#reset")

            page.click("#reset")
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item').length > 0",
                timeout=10000,
            )
            assert page.is_hidden("#reset")
            assert "fields=" not in page.evaluate("location.search")

    def test_a_field_spec_in_the_url_sets_the_toggles(
        self, page: Any, generated_archive: Path
    ) -> None:
        """The `fields` param speaks the same language as `--search-fields`,
        so a delta from the docs sets the boxes rather than being ignored."""
        from claude_code_log.search import SEARCH_FIELDS

        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html?fields=%2Btool_result")
            _wait_ready(page)
            assert _checked_fields(page) == list(SEARCH_FIELDS)

            page.goto(f"{server.url}/search.html?fields=text,meta")
            _wait_ready(page)
            assert _checked_fields(page) == ["text", "meta"]

    def test_turning_every_field_off_explains_itself(
        self, page: Any, generated_archive: Path
    ) -> None:
        """An empty field set is a well-formed request the API answers with
        zero rows — which would read as "no match" rather than "you have
        nothing switched on"."""
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html?q=the&fields=none")
            _wait_ready(page)
            assert _checked_fields(page) == []
            page.wait_for_selector(".search-no-results", timeout=10000)
            assert "at least one field" in page.inner_text(".search-no-results")

    def test_deep_link_reveals_and_highlights_the_match(
        self, page: Any, generated_archive: Path
    ) -> None:
        """`?uuid=` anchors the card, `&q=` opens and highlights the match.

        Both are needed: in a sample of real hits, 5 of 8 matched text that
        sat inside a collapsed <details> which only the search component
        opens.
        """
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html")
            _wait_ready(page)
            page.fill("#q", "Bash")
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item a').length > 0",
                timeout=10000,
            )
            href = page.locator(".search-result-item a").first.get_attribute("href")
            assert "uuid=" in href and "q=" in href

            page.goto(f"{server.url}/{href}")
            page.wait_for_function(
                "document.querySelectorAll('.search-highlight').length > 0",
                timeout=10000,
            )
            state = page.evaluate(
                """() => {
                    const uuid = new URLSearchParams(location.search).get('uuid');
                    const card = document.querySelector('[data-uuid="' + uuid + '"]');
                    const rect = card && card.getBoundingClientRect();
                    return {
                        found: !!card,
                        onScreen: !!(rect && rect.height > 0
                            && rect.top < window.innerHeight
                            && rect.top > -rect.height),
                        query: (document.getElementById('searchInput') || {}).value,
                    };
                }"""
            )
            assert state["found"], "deep-linked message card not in the page"
            assert state["onScreen"], "deep-linked card was not scrolled into view"
            assert state["query"] == "Bash"

    def test_deep_link_with_non_contiguous_terms_still_reveals(
        self, page: Any, generated_archive: Path
    ) -> None:
        """FTS matches `hello decorators` as two AND-ed words sitting apart
        in the message, and the deep link carries the query verbatim — so
        the session page's search has to speak the same language. Treated
        as one contiguous string it matches nothing, and search-as-filter
        hides every message: the page arrives blank."""
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            page.goto(f"{server.url}/search.html?q=hello+decorators")
            _wait_ready(page)
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item a').length > 0",
                timeout=10000,
            )
            href = page.locator(".search-result-item a").first.get_attribute("href")
            assert "uuid=" in href and "q=hello%20decorators" in href

            page.goto(f"{server.url}/{href}")
            page.wait_for_function(
                "document.querySelectorAll('.search-highlight').length > 0",
                timeout=10000,
            )
            state = page.evaluate(
                """() => {
                    const uuid = new URLSearchParams(location.search).get('uuid');
                    const card = document.querySelector('[data-uuid="' + uuid + '"]');
                    const words = new Set(
                        [...document.querySelectorAll('.search-highlight')]
                            .map(h => h.textContent.toLowerCase())
                    );
                    return {
                        cardVisible: !!(card && card.offsetParent !== null
                            && !card.classList.contains('search-hidden')),
                        visibleMessages: [...document.querySelectorAll(
                            '.message:not(.session-header)'
                        )].filter(m => m.offsetParent !== null).length,
                        words: [...words],
                    };
                }"""
            )
            assert state["cardVisible"], "matched card hidden by its own query"
            assert state["visibleMessages"] > 0, "page filtered down to nothing"
            assert "hello" in state["words"] and "decorators" in state["words"]

    def test_deep_link_target_the_local_scan_misses_stays_visible(
        self, page: Any, tmp_path: Path
    ) -> None:
        """The FTS tokenizer folds diacritics (`cafe` finds `café`); the
        in-page literal scan doesn't. When another message matches
        literally, search-as-filter used to hide exactly the card the deep
        link targeted — it must be forced visible as context instead."""
        projects = tmp_path / "projects"
        project = projects / "-home-u-testproj"
        project.mkdir(parents=True)
        source = Path("test/test_data/representative_messages.jsonl").read_text(
            encoding="utf-8"
        )

        def entry(uuid: str, text: str, ts: str) -> str:
            return json.dumps(
                {
                    "type": "user",
                    "timestamp": ts,
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "human",
                    "cwd": "/tmp",
                    "sessionId": "test_session",
                    "version": "1.0.0",
                    "uuid": uuid,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )

        (project / "session.jsonl").write_text(
            source
            + entry(
                "msg_cafe_accent",
                "Met at the café on Rue Cler.",
                "2025-07-03T17:00:00Z",
            )
            + "\n"
            + entry(
                "msg_cafe_plain",
                "We should open a cafe someday.",
                "2025-07-03T17:01:00Z",
            )
            + "\n",
            encoding="utf-8",
        )
        process_projects_hierarchy(projects, silent=True)
        _seed_index(projects)

        with self._serve(projects) as server:
            page.goto(f"{server.url}/search.html?q=cafe")
            _wait_ready(page)
            page.wait_for_function(
                "document.querySelectorAll('.search-result-item a').length > 1",
                timeout=10000,
            )
            hrefs = page.eval_on_selector_all(
                ".search-result-item a",
                "links => links.map(a => a.getAttribute('href'))",
            )
            target = next(h for h in hrefs if "uuid=msg_cafe_accent" in h)

            page.goto(f"{server.url}/{target}")
            # The literal match ("cafe someday") highlights; the deep-link
            # target can't, but must still be on the page.
            page.wait_for_function(
                "document.querySelectorAll('.search-highlight').length > 0",
                timeout=10000,
            )
            state = page.evaluate(
                """() => {
                    const card = document.querySelector(
                        '[data-uuid="msg_cafe_accent"]');
                    return {
                        found: !!card,
                        visible: !!(card && card.offsetParent !== null
                            && !card.classList.contains('search-hidden')),
                    };
                }"""
            )
            assert state["found"], "deep-linked café card missing from the page"
            assert state["visible"], (
                "café card hidden by the very filter its deep link ran"
            )

    def test_uuid_without_a_query_still_scrolls(
        self, page: Any, generated_archive: Path
    ) -> None:
        _seed_index(generated_archive)
        with self._serve(generated_archive) as server:
            session_page = next(generated_archive.glob("*/session-*.html"))
            uuid = _first_uuid(session_page)
            rel = f"{session_page.parent.name}/{session_page.name}"
            page.goto(f"{server.url}/{rel}?uuid={uuid}")
            page.wait_for_timeout(1500)
            state = page.evaluate(
                """(uuid) => {
                    const card = document.querySelector('[data-uuid="' + uuid + '"]');
                    return {
                        found: !!card,
                        searchRan: !!(document.getElementById('searchInput') || {}).value,
                    };
                }""",
                uuid,
            )
            assert state["found"]
            # No `q`, so the in-page search must stay untouched.
            assert not state["searchRan"]


def _wait_ready(page: Any) -> None:
    """Block until the page has finished booting, not merely unhidden.

    ``#app`` is revealed before the project list is fetched, so waiting on
    ``#app:not([hidden])`` returns while the field checkboxes are still in
    their unchecked initial state and the URL's ``fields`` spec has yet to
    be applied — a window a slow machine can land in (observed on Windows
    CI). ``data-ready`` is set on the last line of ``start()``.
    """
    page.wait_for_selector("#app[data-ready]", timeout=10000)


def _checked_fields(page: Any) -> list[str]:
    """The field groups currently switched on, in the page's own order."""
    return page.eval_on_selector_all(
        "#fields input[data-field]",
        "boxes => boxes.filter(b => b.checked).map(b => b.dataset.field)",
    )


def _first_uuid(session_page: Path) -> str:
    """Pull a message uuid out of a generated page.

    Deliberately not a UUID-shaped pattern: transcript uuids are opaque
    identifiers, and the test fixtures use `msg_001`-style ids.
    """
    match = re.search(r"data-uuid='([^']+)'", session_page.read_text(encoding="utf-8"))
    assert match, "no data-uuid in the generated session page"
    return match.group(1)
