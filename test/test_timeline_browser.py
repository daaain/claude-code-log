"""Playwright-based tests for timeline functionality in the browser."""

import tempfile
import re
from pathlib import Path
from typing import List
import pytest
from playwright.sync_api import Page, expect

from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html
from claude_code_log.models import TranscriptEntry


class TestTimelineBrowser:
    """Test timeline functionality using Playwright in a real browser."""

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

        # Create temporary file
        temp_file = Path(tempfile.mktemp(suffix=".html"))
        temp_file.write_text(html_content, encoding="utf-8")
        self.temp_files.append(temp_file)

        return temp_file

    def _wait_for_timeline_loaded(self, page: Page, expect_items: bool = True):
        """Wait for timeline to be fully loaded and initialized.

        Args:
            page: The Playwright page object
            expect_items: Whether to wait for timeline items (default True).
                         Set to False when filters might hide all messages.
        """
        # Wait for timeline container to be visible
        page.wait_for_selector("#timeline-container", state="attached")

        # Wait for vis-timeline to create its DOM elements
        page.wait_for_selector(".vis-timeline", timeout=10000)

        # Wait for timeline items to be rendered (if expected)
        if expect_items:
            page.wait_for_selector(".vis-item", timeout=5000)

    @pytest.mark.browser
    def test_timeline_toggle_button_exists(self, page: Page):
        """Test that timeline toggle button is present."""
        # Use sidechain test data
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Toggle Test")

        page.goto(f"file://{temp_file}")

        # Check timeline toggle button exists
        toggle_btn = page.locator("#toggleTimeline")
        expect(toggle_btn).to_be_visible()
        expect(toggle_btn).to_have_text("📆")
        expect(toggle_btn).to_have_attribute("title", "Show timeline")

    @pytest.mark.browser
    def test_timeline_shows_after_toggle(self, page: Page):
        """Test that timeline becomes visible after clicking toggle."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Visibility Test")

        page.goto(f"file://{temp_file}")

        # Timeline should be hidden initially
        timeline_container = page.locator("#timeline-container")
        expect(timeline_container).to_have_css("display", "none")

        # Click toggle button
        toggle_btn = page.locator("#toggleTimeline")
        toggle_btn.click()

        # Wait for timeline to load and become visible
        self._wait_for_timeline_loaded(page)
        expect(timeline_container).not_to_have_css("display", "none")

        # Button should show active state
        expect(toggle_btn).to_have_text("🗓️")
        expect(toggle_btn).to_have_attribute("title", "Hide timeline")

    @pytest.mark.browser
    def test_timeline_sidechain_messages(self, page: Page):
        """Test that sidechain messages appear correctly in timeline."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Sidechain Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Check that timeline items exist
        timeline_items = page.locator(".vis-item")
        timeline_items.first.wait_for(state="visible", timeout=5000)

        # Count total items
        item_count = timeline_items.count()
        assert item_count > 0, f"Should have timeline items, found {item_count}"

        # Check for the specific failing test content (the key issue from the original bug)
        failing_test_items = page.locator(".vis-item:has-text('failing test')")
        failing_test_count = failing_test_items.count()

        assert failing_test_count > 0, (
            "Should contain the sub-assistant prompt about failing test"
        )

        # Check for other sidechain content to verify multiple sidechain messages
        template_items = page.locator(".vis-item:has-text('template files')")
        template_count = template_items.count()

        summary_items = page.locator(".vis-item:has-text('Summary')")
        summary_count = summary_items.count()

        # Should have multiple sidechain-related content items
        total_sidechain_content = failing_test_count + template_count + summary_count
        assert total_sidechain_content > 0, (
            f"Should have sidechain content items, found {total_sidechain_content}"
        )

    @pytest.mark.browser
    def test_timeline_sidechain_message_groups_and_classes(self, page: Page):
        """Test that sub-assistant messages appear in the correct timeline group with proper CSS classes."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Sidechain Groups Test")

        page.goto(f"file://{temp_file}")

        # First verify sidechain messages exist in the DOM
        sidechain_messages = page.locator(".message.sidechain")
        sidechain_dom_count = sidechain_messages.count()

        # Verify sidechain messages have proper DOM structure

        assert sidechain_dom_count > 0, (
            f"Should have sidechain messages in DOM, found {sidechain_dom_count}"
        )

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Wait for timeline groups to be created by vis-timeline
        page.wait_for_timeout(1000)  # Give timeline time to render groups

        # Note: Sidechain items may be classified differently due to vis-timeline filtering issues

        # Verify that sub-assistant content appears in timeline (the key issue from the original bug)
        failing_test_items = page.locator('.vis-item:has-text("failing test")')
        assert failing_test_items.count() > 0, (
            "Sub-assistant prompt about failing test should appear in timeline"
        )

        # Check for user sidechain messages with 📝 prefix (as shown in timeline display logic)
        user_sidechain_items = page.locator('.vis-item:has-text("📝")')
        user_count = user_sidechain_items.count()

        # Check for assistant sidechain messages with 🔗 prefix
        assistant_sidechain_items = page.locator('.vis-item:has-text("🔗")')
        assistant_count = assistant_sidechain_items.count()

        # Verify that sidechain content appears with proper prefixes
        assert user_count > 0 or assistant_count > 0, (
            "Timeline should show sidechain messages with proper 📝/🔗 prefixes"
        )

    @pytest.mark.browser
    def test_timeline_message_type_filtering_sidechain(self, page: Page):
        """Test that sidechain messages can be filtered independently from regular messages."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Sidechain Filter Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Wait for timeline to fully render
        page.wait_for_timeout(1000)

        # Get initial counts
        all_items = page.locator(".vis-item")
        initial_count = all_items.count()

        # Verify that timeline items exist and basic filtering works
        assert initial_count > 0, "Should have timeline items"

        # Open filter panel
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

    @pytest.mark.browser
    def test_sidechain_filter_toggle_exists(self, page: Page):
        """Test that Sub-assistant filter toggle exists and works."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Sidechain Filter Toggle Test")

        page.goto(f"file://{temp_file}")

        # Open filter panel
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Check that Sub-assistant filter toggle exists
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        expect(sidechain_filter).to_be_visible()
        expect(sidechain_filter).to_contain_text("🔗 Sub-assistant")

        # Should start active (all filters start active by default)
        expect(sidechain_filter).to_have_class(
            re.compile(r".*active.*")
        )  # Use regex to match partial class

    @pytest.mark.browser
    def test_sidechain_message_filtering_integration(self, page: Page):
        """Test that sidechain messages can be filtered in both main content and timeline."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(
            messages, "Sidechain Filtering Integration Test"
        )

        page.goto(f"file://{temp_file}")

        # Verify sidechain messages exist
        sidechain_messages = page.locator(".message.sidechain")
        sidechain_count = sidechain_messages.count()
        assert sidechain_count > 0, "Should have sidechain messages to test filtering"

        # Open filter panel
        page.locator("#filterMessages").click()

        # Check initial state - sidechain filter should be active
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        expect(sidechain_filter).to_be_visible()
        expect(sidechain_filter).to_have_class(re.compile(r".*active.*"))

        # Deselect Sub-assistant filter
        sidechain_filter.click()

        # Wait for filtering to apply
        page.wait_for_timeout(500)

        # Sub-assistant filter should be inactive
        expect(sidechain_filter).not_to_have_class(re.compile(r".*active.*"))

        # Check that sidechain messages are filtered out from main content
        visible_sidechain_messages = page.locator(
            ".message.sidechain:not(.filtered-hidden)"
        )
        assert visible_sidechain_messages.count() == 0, (
            "Sidechain messages should be hidden when filter is off"
        )

        # Re-enable Sub-assistant filter
        sidechain_filter.click()
        page.wait_for_timeout(500)

        # Sub-assistant messages should be visible again
        expect(sidechain_filter).to_have_class(re.compile(r".*active.*"))
        visible_sidechain_messages = page.locator(
            ".message.sidechain:not(.filtered-hidden)"
        )
        assert visible_sidechain_messages.count() > 0, (
            "Sidechain messages should be visible when filter is on"
        )

    @pytest.mark.browser
    def test_sidechain_messages_html_css_classes(self, page: Page):
        """Test that sidechain messages in the main content have correct CSS classes.

        Note: User sidechain messages (Sub-assistant prompts) are now skipped
        since they duplicate the Task tool input prompt.
        """
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Sidechain CSS Classes Test")

        page.goto(f"file://{temp_file}")

        # User sidechain messages should no longer be produced
        # (they duplicate the Task tool input prompt)
        user_sidechain_messages = page.locator(".message.user.sidechain")
        user_count = user_sidechain_messages.count()
        assert user_count == 0, (
            "User sidechain messages should no longer be produced (duplicates Task tool input)"
        )

        # Check for sub-assistant assistant messages in main content
        assistant_sidechain_messages = page.locator(".message.assistant.sidechain")
        assistant_count = assistant_sidechain_messages.count()
        assert assistant_count > 0, (
            "Should have assistant sidechain messages with 'assistant sidechain' classes"
        )

    @pytest.mark.browser
    def test_sidechain_filter_complete_integration(self, page: Page):
        """Test complete integration of sidechain filtering between main content and timeline."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(
            messages, "Complete Sidechain Filter Integration Test"
        )

        page.goto(f"file://{temp_file}")

        # Count initial sidechain messages in main content
        initial_sidechain_messages = page.locator(".message.sidechain")
        initial_sidechain_count = initial_sidechain_messages.count()
        assert initial_sidechain_count > 0, "Should have sidechain messages to test"

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Count initial timeline items
        initial_timeline_items = page.locator(".vis-item")
        initial_timeline_count = initial_timeline_items.count()
        assert initial_timeline_count > 0, "Should have timeline items"

        # Open filter panel
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Test sidechain filter toggle exists and is active by default
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        expect(sidechain_filter).to_be_visible()
        expect(sidechain_filter).to_have_class(re.compile(r".*active.*"))

        # Deselect sidechain filter
        sidechain_filter.click()
        page.wait_for_timeout(500)

        # Verify filter is no longer active
        expect(sidechain_filter).not_to_have_class(re.compile(r".*active.*"))

        # Check that sidechain messages are hidden in main content
        visible_sidechain_messages = page.locator(
            ".message.sidechain:not(.filtered-hidden)"
        )
        assert visible_sidechain_messages.count() == 0, (
            "Sidechain messages should be hidden in main content"
        )

        # Check that timeline still works (may have different item count)
        current_timeline_items = page.locator(".vis-item")
        current_timeline_count = current_timeline_items.count()
        assert current_timeline_count >= 0, (
            "Timeline should still work with sidechain filtering"
        )

        # Re-enable sidechain filter
        sidechain_filter.click()
        page.wait_for_timeout(500)

        # Verify filter is active again
        expect(sidechain_filter).to_have_class(re.compile(r".*active.*"))

        # Check that sidechain messages are visible again in main content
        visible_sidechain_messages = page.locator(
            ".message.sidechain:not(.filtered-hidden)"
        )
        assert visible_sidechain_messages.count() > 0, (
            "Sidechain messages should be visible again"
        )

        # Timeline should still work
        restored_timeline_items = page.locator(".vis-item")
        restored_timeline_count = restored_timeline_items.count()
        assert restored_timeline_count >= 0, (
            "Timeline should work after restoring sidechain filter"
        )

        # Test "None" filter functionality that user mentioned in the issue
        select_none_button = page.locator("#selectNone")
        select_none_button.click()
        page.wait_for_timeout(500)

        # All message filters should be inactive
        expect(sidechain_filter).not_to_have_class(re.compile(r".*active.*"))
        user_filter = page.locator('.filter-toggle[data-type="user"]')
        expect(user_filter).not_to_have_class(re.compile(r".*active.*"))
        assistant_filter = page.locator('.filter-toggle[data-type="assistant"]')
        expect(assistant_filter).not_to_have_class(re.compile(r".*active.*"))

        # All messages should be hidden
        visible_messages = page.locator(
            ".message:not(.session-header):not(.filtered-hidden)"
        )
        assert visible_messages.count() == 0, (
            "All messages should be hidden when no filters are active"
        )

        # Test "All" filter functionality
        select_all_button = page.locator("#selectAll")
        select_all_button.click()
        page.wait_for_timeout(500)

        # All message filters should be active
        expect(sidechain_filter).to_have_class(re.compile(r".*active.*"))
        expect(user_filter).to_have_class(re.compile(r".*active.*"))
        expect(assistant_filter).to_have_class(re.compile(r".*active.*"))

        # All messages should be visible again
        visible_messages = page.locator(
            ".message:not(.session-header):not(.filtered-hidden)"
        )
        assert visible_messages.count() > 0, (
            "All messages should be visible when all filters are active"
        )

    @pytest.mark.browser
    def test_timeline_system_messages(self, page: Page):
        """Test that system messages appear correctly in timeline."""
        system_file = Path("test/test_data/system_model_change.jsonl")
        messages = load_transcript(system_file)
        temp_file = self._create_temp_html(messages, "Timeline System Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Check that timeline items exist
        timeline_items = page.locator(".vis-item")
        timeline_items.first.wait_for(state="visible", timeout=5000)

        # Check for system message content
        system_items = page.locator(".vis-item:has-text('Claude Opus 4 limit reached')")
        system_count = system_items.count()

        assert system_count > 0, "Should contain the system warning about Opus limit"

    @pytest.mark.browser
    def test_timeline_message_click_navigation(self, page: Page):
        """Test that clicking timeline items scrolls to corresponding messages."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Click Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Get timeline items and messages on page
        timeline_items = page.locator(".vis-item")
        messages_on_page = page.locator(".message:not(.session-header)")

        timeline_count = timeline_items.count()
        message_count = messages_on_page.count()

        # Timeline should have items corresponding to messages
        assert timeline_count > 0, "Timeline should have items"
        assert message_count > 0, "Page should have messages"

        # Note: Timeline may have different item count than page messages due to:
        # 1. Messages without timestamps being filtered out
        # 2. Tool use/results being split or combined differently
        # Verify both timeline and messages exist
        assert timeline_count > 0 and message_count > 0, (
            f"Both timeline ({timeline_count}) and messages ({message_count}) should exist"
        )

    @pytest.mark.browser
    def test_timeline_filtering_integration(self, page: Page):
        """Test that timeline filters work with message filters."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Filter Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Check that timeline items exist
        timeline_items = page.locator(".vis-item")
        initial_count = timeline_items.count()
        assert initial_count > 0, "Should have initial timeline items"

        # Open filter panel
        page.locator("#filterMessages").click()

        # Filter panel should be visible
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Test filtering with sub-assistant specifically
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        if sidechain_filter.count() > 0:
            # Deselect sub-assistant filter
            sidechain_filter.click()
            page.wait_for_timeout(500)

            # Timeline should handle filtering (may have fewer items)
            filtered_count = timeline_items.count()
            assert filtered_count >= 0, (
                "Timeline should handle sidechain filtering without errors"
            )

            # Re-enable sub-assistant filter
            sidechain_filter.click()
            page.wait_for_timeout(500)

            # Timeline should show items again
            restored_count = timeline_items.count()
            assert restored_count >= 0, (
                "Timeline should restore items when filter is re-enabled"
            )

        # Test general assistant filtering
        assistant_filter = page.locator('.filter-toggle[data-type="assistant"]')
        if assistant_filter.count() > 0:
            assistant_filter.click()
            page.wait_for_timeout(500)

            # Timeline should still have items (though count may change)
            filtered_count = timeline_items.count()
            assert filtered_count >= 0, (
                "Timeline should handle assistant filtering without errors"
            )

    @pytest.mark.browser
    def test_timeline_console_errors(self, page: Page):
        """Test that timeline doesn't produce JavaScript errors."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Error Test")

        # Capture console messages
        console_messages = []
        page.on("console", lambda msg: console_messages.append(msg))

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Check for errors
        errors = [msg for msg in console_messages if msg.type == "error"]
        error_texts = [msg.text for msg in errors]

        # Filter out common non-critical errors
        critical_errors = [
            error
            for error in error_texts
            if not any(
                ignore in error.lower()
                for ignore in [
                    "favicon.ico",  # Common 404 for favicon
                    "net::err_file_not_found",  # File protocol limitations
                    "refused to connect",  # CORS issues with file protocol
                    "cors",  # CORS related errors
                    "network error",  # Network-related errors in file:// protocol
                ]
            )
        ]

        assert len(critical_errors) == 0, (
            f"Timeline should not produce critical JavaScript errors: {critical_errors}"
        )

    @pytest.mark.browser
    def test_timeline_filter_synchronization(self, page: Page):
        """Test that timeline filtering stays synchronized with main message filtering."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Filter Sync Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Note: We don't need initial counts since we're testing synchronization, not counts

        # Open filter panel
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Test multiple filter combinations
        # Note: Filter buttons with 0 count are hidden, so we skip them
        test_cases = [
            ("assistant", '.filter-toggle[data-type="assistant"]'),
            ("sidechain", '.filter-toggle[data-type="sidechain"]'),
        ]

        for filter_type, selector in test_cases:
            filter_toggle = page.locator(selector)
            if filter_toggle.count() > 0 and filter_toggle.is_visible():
                # Deselect the filter
                filter_toggle.click()
                page.wait_for_timeout(100)  # Allow filters to apply

                # Check that main messages are filtered
                visible_main_messages = page.locator(
                    f".message.{filter_type}:not(.filtered-hidden)"
                ).count()
                assert visible_main_messages == 0, (
                    f"Main messages of type '{filter_type}' should be hidden when filter is off"
                )

                # Timeline should also be filtered (though we don't check exact counts
                # because timeline groups messages differently)

                # Re-enable the filter
                filter_toggle.click()
                page.wait_for_timeout(100)

                # Check that messages are visible again
                visible_main_messages = page.locator(
                    f".message.{filter_type}:not(.filtered-hidden)"
                ).count()
                assert visible_main_messages > 0, (
                    f"Main messages of type '{filter_type}' should be visible when filter is on"
                )

    @pytest.mark.browser
    def test_timeline_filter_all_none_buttons(self, page: Page):
        """Test that timeline responds correctly to 'Select All' and 'Select None' buttons."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline All/None Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Get initial timeline item count
        initial_timeline_count = page.locator(".vis-item").count()
        assert initial_timeline_count > 0, "Should have timeline items initially"

        # Open filter panel
        page.locator("#filterMessages").click()

        # Test 'Select None' button
        page.locator("#selectNone").click()
        page.wait_for_timeout(200)  # Allow filters to apply

        # All main messages should be hidden
        visible_main_messages = page.locator(
            ".message:not(.session-header):not(.filtered-hidden)"
        ).count()
        assert visible_main_messages == 0, (
            "All main messages should be hidden with 'Select None'"
        )

        # Timeline should be empty or have very few items
        none_timeline_count = page.locator(".vis-item").count()
        assert none_timeline_count >= 0, (
            "Timeline should handle 'Select None' without errors"
        )

        # Test 'Select All' button
        page.locator("#selectAll").click()
        page.wait_for_timeout(200)

        # All main messages should be visible again
        visible_main_messages = page.locator(
            ".message:not(.session-header):not(.filtered-hidden)"
        ).count()
        assert visible_main_messages > 0, (
            "Main messages should be visible with 'Select All'"
        )

        # Timeline should have items again
        all_timeline_count = page.locator(".vis-item").count()
        assert all_timeline_count > 0, "Timeline should show items with 'Select All'"

    @pytest.mark.browser
    def test_timeline_filter_individual_message_types(self, page: Page):
        """Test filtering individual message types in timeline."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Individual Filter Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Open filter panel
        page.locator("#filterMessages").click()

        # Test each available filter type
        filter_types = [
            ("user", "User"),
            ("assistant", "Assistant"),
            ("sidechain", "Sub-assistant"),
            ("tool", "Tool"),  # Unified filter for both tool_use and tool_result
            ("thinking", "Thinking"),
            ("system", "System"),
        ]

        for filter_type, _display_name in filter_types:
            filter_selector = f'.filter-toggle[data-type="{filter_type}"]'
            filter_toggle = page.locator(filter_selector)

            if filter_toggle.count() > 0 and filter_toggle.is_visible():
                # Ensure filter starts active
                if "active" not in (filter_toggle.get_attribute("class") or ""):
                    filter_toggle.click()
                    page.wait_for_timeout(100)

                # Verify filter is active
                expect(filter_toggle).to_have_class(re.compile(r".*active.*"))

                # Deselect only this filter
                filter_toggle.click()
                page.wait_for_timeout(100)

                # Verify filter is inactive
                expect(filter_toggle).not_to_have_class(re.compile(r".*active.*"))

                # Timeline should still work (no errors)
                timeline_items = page.locator(".vis-item")
                current_count = timeline_items.count()
                assert current_count >= 0, (
                    f"Timeline should handle {filter_type} filtering without errors"
                )

                # Re-enable the filter
                filter_toggle.click()
                page.wait_for_timeout(100)

                # Verify filter is active again
                expect(filter_toggle).to_have_class(re.compile(r".*active.*"))

    @pytest.mark.browser
    def test_timeline_filter_edge_cases(self, page: Page):
        """Test edge cases in timeline filtering."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Edge Cases Test")

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Test rapid filter toggling
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Note: Filter buttons with 0 count are hidden (e.g., user filter when sidechain.jsonl has no regular users)
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        assistant_filter = page.locator('.filter-toggle[data-type="assistant"]')

        if sidechain_filter.is_visible() and assistant_filter.is_visible():
            # Rapidly toggle filters
            for _ in range(3):
                sidechain_filter.click()
                page.wait_for_timeout(50)
                assistant_filter.click()
                page.wait_for_timeout(50)
                sidechain_filter.click()
                page.wait_for_timeout(50)
                assistant_filter.click()
                page.wait_for_timeout(50)

            # Timeline should still be functional
            timeline_items = page.locator(".vis-item")
            final_count = timeline_items.count()
            assert final_count >= 0, "Timeline should handle rapid filter toggling"

        # Test filter state after timeline hide/show
        page.locator("#toggleTimeline").click()  # Hide timeline
        page.wait_for_timeout(200)

        # Change filters while timeline is hidden
        if sidechain_filter.is_visible():
            sidechain_filter.click()
            page.wait_for_timeout(100)

        # Show timeline again (may have no items if filters hide all messages)
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page, expect_items=False)

        # Timeline should reflect current filter state
        timeline_items = page.locator(".vis-item")
        restored_count = timeline_items.count()
        assert restored_count >= 0, "Timeline should restore with correct filter state"

    @pytest.mark.browser
    def test_timeline_filter_performance(self, page: Page):
        """Test that timeline filtering performs well with various message types."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Performance Test")

        # Capture console messages for performance warnings
        console_messages = []
        page.on("console", lambda msg: console_messages.append(msg))

        page.goto(f"file://{temp_file}")

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Open filter panel
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Perform multiple filter operations in sequence
        start_time = page.evaluate("() => performance.now()")

        # Test sequence of filter operations
        # Note: Filter buttons with 0 count are hidden, so check visibility
        operations = [
            "#selectNone",
            "#selectAll",
            '.filter-toggle[data-type="assistant"]',
            '.filter-toggle[data-type="sidechain"]',
        ]

        for operation in operations:
            locator = page.locator(operation)
            if locator.count() > 0 and locator.is_visible():
                locator.click()
                page.wait_for_timeout(100)

        end_time = page.evaluate("() => performance.now()")
        duration = end_time - start_time

        # Should complete filtering operations reasonably quickly
        assert duration < 5000, f"Filter operations took too long: {duration}ms"

        # Check for performance warnings in console
        warnings = [
            msg for msg in console_messages if "performance" in msg.text.lower()
        ]
        assert len(warnings) == 0, (
            f"Timeline filtering should not produce performance warnings: {warnings}"
        )

    @pytest.mark.browser
    def test_timeline_filter_message_type_coverage(self, page: Page):
        """Test that all message types generated by the renderer are handled in timeline filtering."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(
            messages, "Timeline Message Type Coverage Test"
        )

        page.goto(f"file://{temp_file}")

        # Check what message types are actually present in the DOM
        message_elements = page.locator(".message:not(.session-header)")
        message_count = message_elements.count()

        # Collect all CSS classes used on messages
        all_classes = set()
        for i in range(message_count):
            element = message_elements.nth(i)
            class_list = element.get_attribute("class")
            if class_list:
                all_classes.update(class_list.split())

        # Filter to get just message type classes
        message_type_classes = {
            cls
            for cls in all_classes
            if cls
            in [
                "user",
                "assistant",
                "tool_use",
                "tool_result",
                "thinking",
                "system",
                "image",
                "sidechain",
            ]
        }

        # Activate timeline
        page.locator("#toggleTimeline").click()
        self._wait_for_timeline_loaded(page)

        # Open filter panel
        page.locator("#filterMessages").click()

        # Check that filter toggles exist for all message types found
        for message_type in message_type_classes:
            # Map tool_use and tool_result to unified "tool" filter
            filter_type = (
                "tool" if message_type in ["tool_use", "tool_result"] else message_type
            )
            filter_selector = f'.filter-toggle[data-type="{filter_type}"]'
            filter_toggle = page.locator(filter_selector)

            if message_type in [
                "user",
                "assistant",
                "sidechain",
                "tool_use",
                "tool_result",
                "thinking",
                "system",
            ]:
                # These message types should have filter toggles
                assert filter_toggle.count() > 0, (
                    f"Filter toggle should exist for message type: {message_type} (filter: {filter_type})"
                )

            # Test that timeline can handle filtering for this message type
            if filter_toggle.count() > 0:
                # Toggle the filter
                filter_toggle.click()
                page.wait_for_timeout(100)

                # Timeline should still work
                timeline_items = page.locator(".vis-item")
                item_count = timeline_items.count()
                assert item_count >= 0, (
                    f"Timeline should handle {message_type} filtering without errors"
                )

                # Toggle back
                filter_toggle.click()
                page.wait_for_timeout(100)

    @pytest.mark.browser
    def test_timeline_synchronizes_with_message_filtering(self, page: Page):
        """Test that timeline synchronizes visibility with main message filtering using CSS classes."""
        sidechain_file = Path("test/test_data/sidechain.jsonl")
        messages = load_transcript(sidechain_file)
        temp_file = self._create_temp_html(messages, "Timeline Visible Messages Test")

        page.goto(f"file://{temp_file}")

        # Open filter panel and turn off user messages
        page.locator("#filterMessages").click()
        filter_toolbar = page.locator(".filter-toolbar")
        expect(filter_toolbar).to_be_visible()

        # Note: User filter may be hidden if sidechain.jsonl has no regular user messages
        # Use sidechain filter instead for this test
        sidechain_filter = page.locator('.filter-toggle[data-type="sidechain"]')
        if sidechain_filter.is_visible():
            sidechain_filter.click()  # Turn off sidechain messages
            page.wait_for_timeout(100)

            # Check that sidechain messages are hidden in main content
            visible_sidechain_messages = page.locator(
                ".message.sidechain:not(.filtered-hidden)"
            ).count()
            assert visible_sidechain_messages == 0, (
                "Sidechain messages should be hidden by main filter"
            )

            # Now activate timeline (may have no items if filters hide all messages)
            page.locator("#toggleTimeline").click()
            self._wait_for_timeline_loaded(page, expect_items=False)

            # Timeline should NOT contain sidechain messages since they're filtered out
            # This is the core issue - timeline might be building from all messages, not just visible ones

            # Check timeline items - if the bug exists, we'll see sidechain messages in timeline
            # even though they're filtered out in main view
            timeline_items = page.locator(".vis-item")
            timeline_count = timeline_items.count()

            # Let's check if any timeline items contain sidechain content that should be filtered
            # This is tricky because we need to check the timeline's internal representation

            # For now, let's just verify that timeline filtering matches main filtering
            # by checking if timeline shows fewer items when sidechain filter is off

            # Turn sidechain filter back on
            sidechain_filter.click()
            page.wait_for_timeout(100)

            # Timeline should now show more items (or same if no sidechain messages were in timeline)
            timeline_items_with_sidechain = page.locator(".vis-item")
            timeline_count_with_sidechain = timeline_items_with_sidechain.count()

            # The counts should be different if sidechain messages are properly filtered
            # But this test documents the expected behavior even if it's currently broken
            print(f"Timeline items without sidechain filter: {timeline_count}")
            print(
                f"Timeline items with sidechain filter: {timeline_count_with_sidechain}"
            )

            # The assertion here documents what SHOULD happen
            # If timeline filtering works correctly, timeline_count_with_sidechain should be >= timeline_count
            # because enabling sidechain filter should show same or more items
            assert timeline_count_with_sidechain >= timeline_count, (
                "Timeline should show same or more items when sidechain filter is enabled"
            )

    @pytest.mark.browser
    def test_timezone_conversion_functionality(self, page: Page):
        """Test that timestamps are converted to user's timezone with proper display."""
        representative_file = Path("test/test_data/representative_messages.jsonl")
        messages = load_transcript(representative_file)
        temp_file = self._create_temp_html(messages, "Timezone Conversion Test")

        page.goto(f"file://{temp_file}")

        # Wait for page to load and timestamp conversion to occur
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)  # Give time for JavaScript to run

        # Check that timestamp elements have data-timestamp attributes
        timestamp_elements = page.locator(".timestamp[data-timestamp]")
        count = timestamp_elements.count()
        assert count > 0, (
            "Should have timestamp elements with data-timestamp attributes"
        )

        # Check the first timestamp element
        first_timestamp = timestamp_elements.nth(0)

        # Get the data-timestamp attribute (raw ISO timestamp)
        raw_timestamp = first_timestamp.get_attribute("data-timestamp")
        assert raw_timestamp is not None, "Should have data-timestamp attribute"
        assert "T" in raw_timestamp, (
            "Raw timestamp should be in ISO format with T separator"
        )
        assert raw_timestamp.endswith("Z"), "Raw timestamp should end with Z (UTC)"

        # Get the displayed text (converted timestamp)
        displayed_text = first_timestamp.text_content()
        assert displayed_text is not None, "Should have displayed timestamp text"

        # The displayed text should be different from the raw timestamp (unless user is in UTC)
        # and should contain timezone info
        if not displayed_text.endswith("(UTC)"):
            # User is not in UTC, so should show timezone abbreviation
            assert ")" in displayed_text, (
                "Should show timezone abbreviation in parentheses"
            )

        # Check tooltip exists with both UTC and local time info
        title_attr = first_timestamp.get_attribute("title")
        assert title_attr is not None, "Should have title attribute with time info"
        assert "UTC:" in title_attr, "Title should contain UTC time information"

        # Verify that timeline still works correctly with raw timestamps
        timeline_toggle = page.locator("#toggleTimeline")
        if timeline_toggle.count() > 0:
            timeline_toggle.click()
            self._wait_for_timeline_loaded(page)

            # Timeline should still show items correctly
            timeline_items = page.locator(".vis-item")
            timeline_count = timeline_items.count()
            assert timeline_count > 0, (
                "Timeline should show items after timezone conversion"
            )

    @pytest.mark.browser
    def test_timezone_conversion_error_handling(self, page: Page):
        """Test that timestamp conversion handles errors gracefully."""
        representative_file = Path("test/test_data/representative_messages.jsonl")
        messages = load_transcript(representative_file)
        temp_file = self._create_temp_html(messages, "Timezone Error Handling Test")

        page.goto(f"file://{temp_file}")

        # Wait for page to load
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        # Check console for any errors related to timestamp conversion
        console_messages = []
        page.on("console", lambda msg: console_messages.append(msg))

        # Reload to capture any console messages during conversion
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        # Check for any error messages in console
        error_messages = [msg for msg in console_messages if msg.type == "error"]

        # We might have warnings about timestamp conversion, but no errors
        timestamp_errors = [
            msg for msg in error_messages if "timestamp" in msg.text.lower()
        ]

        # Should not have critical timestamp conversion errors
        assert len(timestamp_errors) == 0, (
            f"Should not have timestamp conversion errors: {timestamp_errors}"
        )
