"""Playwright-based tests for query parameter functionality in the browser."""

import tempfile
import re
from pathlib import Path
from typing import List
import pytest
from playwright.sync_api import Page, expect

from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html
from claude_code_log.models import TranscriptEntry


class TestQueryParamsBrowser:
    """Test query parameter functionality using Playwright in a real browser."""

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

        # Create temporary file securely
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html_content)
            temp_file = Path(f.name)
        self.temp_files.append(temp_file)

        return temp_file

    @pytest.mark.browser
    def test_filter_query_param_shows_toolbar(self, page: Page):
        """Test that filter query parameter makes toolbar visible."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Query Param Test")

        # Load page with filter query param
        page.goto(f"file://{temp_file}?filter=user,assistant")

        # Filter toolbar should be visible
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Filter button should have active class
        filter_button = page.locator("#filterMessages")
        expect(filter_button).to_have_class(re.compile(r".*active.*"))

    @pytest.mark.browser
    def test_filter_query_param_sets_active_toggles(self, page: Page):
        """Test that filter query parameter activates only specified message types."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Query Param Toggle Test")

        # Load page with only user and assistant filters
        page.goto(f"file://{temp_file}?filter=user,assistant")

        # User and assistant toggles should be active
        user_toggle = page.locator('[data-type="user"]')
        assistant_toggle = page.locator('[data-type="assistant"]')
        expect(user_toggle).to_have_class(re.compile(r".*active.*"))
        expect(assistant_toggle).to_have_class(re.compile(r".*active.*"))

        # Other toggles should not be active
        system_toggle = page.locator('[data-type="system"]')
        if system_toggle.count() > 0:
            expect(system_toggle).not_to_have_class(re.compile(r".*active.*"))

        sidechain_toggle = page.locator('[data-type="sidechain"]')
        if sidechain_toggle.count() > 0:
            expect(sidechain_toggle).not_to_have_class(re.compile(r".*active.*"))

    @pytest.mark.browser
    def test_filter_query_param_filters_messages(self, page: Page):
        """Test that filter query parameter actually hides/shows messages.

        Note: sidechain.jsonl only has sidechain user messages which are now skipped
        (they duplicate Task tool input). Test with sidechain+assistant filters instead.
        """
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Query Param Filtering Test")

        # Load page with sidechain and assistant filters active
        page.goto(f"file://{temp_file}?filter=sidechain,assistant")

        # Wait for page to load and filters to apply
        page.wait_for_load_state("networkidle")
        # Wait for filter application by checking for filtered-hidden class to be applied
        page.wait_for_selector(
            ".message.tool_use.filtered-hidden", state="attached", timeout=5000
        )

        # Sidechain assistant messages should be visible
        visible_sidechain_messages = page.locator(
            ".message.sidechain.assistant:not(.filtered-hidden)"
        )
        sidechain_count = visible_sidechain_messages.count()
        assert sidechain_count > 0, "Sidechain assistant messages should be visible"

        # Tool use messages should be hidden (not in filter)
        visible_tool_messages = page.locator(".message.tool_use:not(.filtered-hidden)")
        tool_count = visible_tool_messages.count()
        assert tool_count == 0, "Tool use messages should be hidden"

    @pytest.mark.browser
    def test_no_query_params_toolbar_hidden(self, page: Page):
        """Test that toolbar is hidden by default when no query params."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "No Query Params Test")

        # Load page without query params
        page.goto(f"file://{temp_file}")

        # Filter toolbar should not be visible
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).not_to_be_visible()

        # All messages should be visible by default
        all_messages = page.locator(".message:not(.session-header)")
        visible_messages = page.locator(
            ".message:not(.session-header):not(.filtered-hidden)"
        )

        assert all_messages.count() == visible_messages.count(), (
            "All messages should be visible without filters"
        )

    @pytest.mark.browser
    def test_invalid_filter_types_ignored(self, page: Page):
        """Test that invalid filter types are ignored gracefully."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Invalid Filter Test")

        # Load page with mix of valid and invalid filter types
        page.goto(f"file://{temp_file}?filter=user,invalid_type,assistant")

        # Filter toolbar should still be visible
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Valid toggles should be active
        user_toggle = page.locator('[data-type="user"]')
        assistant_toggle = page.locator('[data-type="assistant"]')
        expect(user_toggle).to_have_class(re.compile(r".*active.*"))
        expect(assistant_toggle).to_have_class(re.compile(r".*active.*"))

    @pytest.mark.browser
    def test_query_params_with_timeline(self, page: Page):
        """Test that query parameters work correctly with timeline enabled."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Query Params Timeline Test")

        # Load page with filter query param including sidechain
        # (sidechain messages require both sidechain AND their type filter)
        page.goto(f"file://{temp_file}?filter=user,assistant,sidechain")

        # Filter toolbar should be visible
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Enable timeline
        page.locator("#toggleTimeline").click()
        page.wait_for_selector(".vis-timeline", timeout=10000)

        # Filters should still be applied
        user_toggle = page.locator('[data-type="user"]')
        assistant_toggle = page.locator('[data-type="assistant"]')
        sidechain_toggle = page.locator('[data-type="sidechain"]')
        expect(user_toggle).to_have_class(re.compile(r".*active.*"))
        expect(assistant_toggle).to_have_class(re.compile(r".*active.*"))
        expect(sidechain_toggle).to_have_class(re.compile(r".*active.*"))

        # Timeline should show filtered content
        timeline_items = page.locator(".vis-item")
        assert timeline_items.count() > 0, "Timeline should have items"
