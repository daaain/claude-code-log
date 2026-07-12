"""Playwright-based tests for in-page search functionality in the browser.

Covers the search-as-filter behaviour on transcript pages: the `/` shortcut,
strict filtering with highlight + auto-unfold, the "Show context" toggle,
match navigation, Escape-to-clear, option persistence, and timeline sync.
"""

import re
import tempfile
from pathlib import Path
from typing import List

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.models import TranscriptEntry


class TestSearchBrowser:
    """Test search functionality using Playwright in a real browser."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_files: List[Path] = []

    def teardown_method(self):
        """Clean up temporary files."""
        for temp_file in self.temp_files:
            try:
                temp_file.unlink()
            except FileNotFoundError:
                pass

    def _create_temp_html(self, messages: List[TranscriptEntry], title: str) -> Path:
        """Create a temporary HTML file for testing."""
        html_content = generate_html(messages, title)

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            temp_file = Path(f.name)
        temp_file.write_text(html_content, encoding="utf-8")
        self.temp_files.append(temp_file)

        return temp_file

    def _open_transcript(
        self, page: Page, title: str, data_file: str = "representative_messages"
    ) -> Path:
        """Render test data and open it with clean localStorage.

        The browser context is shared across tests (persistent context for
        HTTP caching), so saved search options would otherwise leak between
        tests via localStorage.
        """
        transcript_file = Path(f"test/test_data/{data_file}.jsonl")
        messages = load_transcript(transcript_file)
        temp_file = self._create_temp_html(messages, title)

        page.goto(f"file://{temp_file}")
        page.evaluate("localStorage.clear()")
        page.reload()

        return temp_file

    def _search_for(self, page: Page, query: str):
        """Open the search toolbar via `/` and type a query."""
        page.keyboard.press("/")
        search_input = page.locator("#searchInput")
        expect(search_input).to_be_focused()
        search_input.fill(query)

    @pytest.mark.browser
    def test_slash_opens_search(self, page: Page):
        """`/` opens the filter toolbar and focuses the search input."""
        self._open_transcript(page, "Search Open Test")

        toolbar = page.locator(".filter-toolbar")
        expect(toolbar).not_to_have_class(re.compile(r"\bvisible\b"))

        page.keyboard.press("/")

        expect(toolbar).to_have_class(re.compile(r"\bvisible\b"))
        search_input = page.locator("#searchInput")
        expect(search_input).to_be_focused()
        # The `/` keystroke itself must not end up in the input
        expect(search_input).to_have_value("")

    @pytest.mark.browser
    def test_slash_ignored_while_typing(self, page: Page):
        """`/` typed inside the search input is literal, not a shortcut."""
        self._open_transcript(page, "Search Slash Literal Test")

        page.keyboard.press("/")
        search_input = page.locator("#searchInput")
        expect(search_input).to_be_focused()

        page.keyboard.type("a/b")
        expect(search_input).to_have_value("a/b")

    @pytest.mark.browser
    def test_ctrl_f_left_to_browser(self, page: Page):
        """Ctrl+F is no longer hijacked: toolbar stays closed, input unfocused."""
        self._open_transcript(page, "Search Ctrl+F Test")

        page.keyboard.press("ControlOrMeta+f")

        toolbar = page.locator(".filter-toolbar")
        expect(toolbar).not_to_have_class(re.compile(r"\bvisible\b"))
        expect(page.locator("#searchInput")).not_to_be_focused()

    @pytest.mark.browser
    def test_search_filters_and_highlights(self, page: Page):
        """Matching messages are highlighted; all others are strictly hidden."""
        self._open_transcript(page, "Search Filter Test")

        self._search_for(page, "decorators")

        # Matches get .search-match and contain highlight spans
        matches = page.locator(".message.search-match")
        expect(matches.first).to_be_visible()
        match_count = matches.count()
        assert match_count >= 2, f"Expected several matches, got {match_count}"
        expect(
            page.locator(".message.search-match .search-highlight").first
        ).to_be_visible()

        # Strict mode: every non-matching message is hidden
        total = page.locator(".message:not(.session-header)").count()
        hidden = page.locator(".message.search-hidden").count()
        assert hidden == total - match_count, (
            f"Expected {total - match_count} hidden messages, got {hidden}"
        )
        expect(page.locator(".message.search-hidden").first).not_to_be_visible()

        # No context messages in strict mode
        assert page.locator(".message.search-context").count() == 0

        # Counter reflects the match set
        expect(page.locator("#searchResultCount")).to_have_text(
            re.compile(rf"1 of {match_count} matches")
        )

    @pytest.mark.browser
    def test_search_unfolds_matches_in_folded_subtrees(self, page: Page):
        """A match inside an initially-folded subtree becomes visible."""
        self._open_transcript(page, "Search Unfold Test", data_file="sidechain")

        # Sub-assistant tool interactions are nested under the Task tool_use
        # and folded on initial load.
        nested_match = page.locator(
            ".message.tool_use.sidechain", has_text="test_toggle"
        ).first
        expect(nested_match).not_to_be_visible()

        self._search_for(page, "test_toggle")

        expect(nested_match).to_be_visible()
        expect(nested_match).to_have_class(re.compile(r"\bsearch-match\b"))

    @pytest.mark.browser
    def test_show_context_reveals_ancestors(self, page: Page):
        """The 'Show context' toggle keeps ancestors of matches visible."""
        self._open_transcript(page, "Search Context Test")

        self._search_for(page, "Alice")
        expect(page.locator(".message.search-match").first).to_be_visible()
        hidden_strict = page.locator(".message.search-hidden").count()
        assert page.locator(".message.search-context").count() == 0

        page.locator("#searchShowContext").check()

        context = page.locator(".message.search-context")
        expect(context.first).to_be_visible()
        hidden_with_context = page.locator(".message.search-hidden").count()
        assert hidden_with_context == hidden_strict - context.count()

        # Toggling back off returns to strict filtering
        page.locator("#searchShowContext").uncheck()
        expect(page.locator(".message.search-context")).to_have_count(0)

    @pytest.mark.browser
    def test_escape_clears_search(self, page: Page):
        """Escape clears the query, filter classes, and highlights."""
        self._open_transcript(page, "Search Escape Test")

        self._search_for(page, "decorators")
        expect(page.locator(".message.search-match").first).to_be_visible()

        page.keyboard.press("Escape")

        expect(page.locator("#searchInput")).to_have_value("")
        expect(page.locator(".message.search-hidden")).to_have_count(0)
        expect(page.locator(".message.search-match")).to_have_count(0)
        expect(page.locator(".search-highlight")).to_have_count(0)

    @pytest.mark.browser
    def test_match_navigation(self, page: Page):
        """Next/prev buttons and Enter/F3 cycle through matches."""
        self._open_transcript(page, "Search Navigation Test")

        self._search_for(page, "decorator")
        counter = page.locator("#searchResultCount")
        expect(counter).to_have_text(re.compile(r"1 of \d+ matches"))
        match_count = page.locator(".message.search-match").count()
        assert match_count >= 3, f"Need several matches to navigate, got {match_count}"

        page.locator("#searchNext").click()
        expect(counter).to_have_text(re.compile(r"2 of \d+ matches"))

        # Enter in the input advances too
        page.locator("#searchInput").focus()
        page.keyboard.press("Enter")
        expect(counter).to_have_text(re.compile(r"3 of \d+ matches"))

        # Shift+F3 goes back
        page.keyboard.press("Shift+F3")
        expect(counter).to_have_text(re.compile(r"2 of \d+ matches"))

        # Current match is marked
        expect(page.locator(".search-highlight.current")).to_have_count(1)

    @pytest.mark.browser
    def test_regex_search(self, page: Page):
        """Regex mode matches patterns instead of literals."""
        self._open_transcript(page, "Search Regex Test")

        page.keyboard.press("/")
        page.locator("#searchRegex").check()
        page.locator("#searchInput").fill("dec.rators")

        expect(page.locator(".message.search-match").first).to_be_visible()
        expect(
            page.locator(".search-highlight", has_text="decorators").first
        ).to_be_visible()

    @pytest.mark.browser
    def test_options_persist_across_reload(self, page: Page):
        """Regex and Show-context options are restored from localStorage."""
        self._open_transcript(page, "Search Persistence Test")

        page.keyboard.press("/")
        page.locator("#searchRegex").check()
        page.locator("#searchShowContext").check()

        page.reload()

        expect(page.locator("#searchRegex")).to_be_checked()
        expect(page.locator("#searchShowContext")).to_be_checked()

    @pytest.mark.browser
    def test_timeline_respects_search_filter(self, page: Page):
        """Timeline items for search-hidden messages are hidden, and restored on clear."""
        self._open_transcript(page, "Search Timeline Test")

        self._search_for(page, "Alice")
        expect(page.locator(".message.search-match").first).to_be_visible()
        hidden_messages = page.locator(".message.search-hidden").count()
        assert hidden_messages > 0

        # Open the timeline after searching: it must pick up the active filter
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-timeline", timeout=30000)
        page.wait_for_selector(".vis-item", state="attached", timeout=5000)

        hidden_items = page.locator(".vis-item.timeline-filtered-hidden")
        expect(hidden_items.first).to_be_attached()

        # Clearing the search un-hides the timeline items
        page.locator("#searchClear").click()
        expect(page.locator(".vis-item.timeline-filtered-hidden")).to_have_count(0)
