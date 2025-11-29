"""Tests for the collapsible details toggle functionality."""

from typing import Any, Dict, List
from claude_code_log.models import (
    AssistantTranscriptEntry,
    AssistantMessage,
    UsageInfo,
    parse_content_item,
)
from claude_code_log.renderer import generate_html


class TestToggleFunctionality:
    """Test collapsible details and toggle functionality."""

    def _create_assistant_message(
        self, content_items: List[Dict[str, Any]]
    ) -> AssistantTranscriptEntry:
        """Helper to create a properly structured AssistantTranscriptEntry."""
        # Convert raw content items to proper ContentItem objects
        parsed_content = [parse_content_item(item) for item in content_items]

        # Create AssistantMessage with proper types
        message = AssistantMessage(
            id="msg_001",
            type="message",
            role="assistant",
            model="claude-3-sonnet-20240229",
            content=parsed_content,
            stop_reason="end_turn",
            stop_sequence=None,
            usage=UsageInfo(
                input_tokens=25,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=120,
                service_tier="standard",
            ),
        )

        return AssistantTranscriptEntry(
            type="assistant",
            timestamp="2025-06-14T10:00:00.000Z",
            parentUuid=None,
            isSidechain=False,
            userType="human",
            cwd="/tmp",
            sessionId="test_session",
            version="1.0.0",
            uuid="test_uuid",
            requestId="req_001",
            message=message,
        )

    def test_toggle_button_present_in_html(self):
        """Test that the toggle button and JavaScript are present in generated HTML."""
        # Create a message with tool use content to ensure we have collapsible details
        long_content = "This is a very long content " * 20  # Make it long enough
        tool_use_content = {
            "type": "tool_use",
            "id": "test_tool",
            "name": "TestTool",
            "input": {"content": long_content},
        }

        message = self._create_assistant_message([tool_use_content])

        html = generate_html([message], "Test Toggle")

        # Check for toggle button
        assert 'id="toggleDetails"' in html, "Should contain toggle button"
        assert 'class="toggle-details floating-btn"' in html, (
            "Should have toggle button styling"
        )

        # Check for JavaScript functionality
        assert "toggleAllDetails" in html, "Should contain toggle JavaScript function"
        assert "updateToggleButton" in html, "Should contain update function"
        assert "addEventListener" in html, "Should set up event listeners"

    def test_toggle_button_with_no_collapsible_content(self):
        """Test that toggle button is hidden when no collapsible details exist."""
        # Create message with short content that won't be collapsible
        text_content = {
            "type": "text",
            "text": "Short text message",
        }

        simple_message = self._create_assistant_message([text_content])

        html = generate_html([simple_message], "Test No Toggle")

        # Toggle button should still be present but JavaScript should hide it
        assert 'id="toggleDetails"' in html, "Toggle button should be in HTML"
        assert "toggleButton.style.display = 'none'" in html, (
            "JavaScript should hide button when no details exist"
        )

    def test_collapsible_details_structure(self):
        """Test the structure of collapsible details elements."""
        # Create content long enough to trigger collapsible in tool params
        long_input = {
            "data": "x" * 300
        }  # Definitely over 200 chars when JSON serialized
        tool_use_content = {
            "type": "tool_use",
            "id": "test_tool",
            "name": "LongTool",
            "input": long_input,
        }

        message = self._create_assistant_message([tool_use_content])

        html = generate_html([message], "Test Structure")

        # Check for tool parameter table with collapsible details
        assert "class='tool-params-table'" in html, "Should have tool params table"
        assert "<summary>" in html, "Should have summary element"
        assert "class='tool-param-collapsible'" in html, (
            "Should have collapsible tool param"
        )

    def test_collapsible_details_css_selectors(self):
        """Test that the CSS selectors used in JavaScript are present."""
        long_content = "Very long content " * 30
        tool_use_content = {
            "type": "tool_use",
            "id": "test_tool",
            "name": "TestTool",
            "input": {"content": long_content},
        }

        message = self._create_assistant_message([tool_use_content])

        html = generate_html([message], "Test Selectors")

        # Check that JavaScript uses the correct selectors for ALL collapsible types
        assert "collapsibleSelector" in html, (
            "JavaScript should define collapsible selector variable"
        )
        # All three collapsible classes should be included
        assert "details.collapsible-details" in html, (
            "JavaScript should target collapsible-details"
        )
        assert "details.collapsible-code" in html, (
            "JavaScript should target collapsible-code"
        )
        assert "details.tool-param-collapsible" in html, (
            "JavaScript should target tool-param-collapsible"
        )

    def test_toggle_button_icons_and_titles(self):
        """Test that toggle button has proper icons and titles."""
        tool_use_content = {
            "type": "tool_use",
            "id": "test_tool",
            "name": "TestTool",
            "input": {"content": "x" * 300},
        }

        message = self._create_assistant_message([tool_use_content])

        html = generate_html([message], "Test Icons")

        # Check for icon switching logic
        assert "textContent = mostlyOpen ? '📦' : '🗃️'" in html, (
            "Should switch between open/close icons"
        )
        assert (
            "title = mostlyOpen ? 'Close all details' : 'Open all details'" in html
        ), "Should switch between open/close titles"

    def test_multiple_collapsible_elements(self):
        """Test handling of multiple collapsible elements."""
        # Create multiple tool uses
        tool_contents: List[Dict[str, Any]] = []
        for i in range(3):
            tool_content = {
                "type": "tool_use",
                "id": f"tool_{i}",
                "name": f"Tool{i}",
                "input": {"content": "x" * 300, "index": i},
            }
            tool_contents.append(tool_content)

        message = self._create_assistant_message(tool_contents)

        html = generate_html([message], "Test Multiple")

        # Should have multiple collapsible tool params (only count actual HTML elements, not in JS)
        import re

        # Remove script tags and their content to avoid counting strings in JavaScript
        html_without_scripts = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
        collapsible_count = html_without_scripts.count("class='tool-param-collapsible'")
        # Each tool has 2 params (content and index), so 3 tools = 6 params, but only content is long enough to be collapsible
        assert collapsible_count == 3, (
            f"Should have 3 collapsible tool params, got {collapsible_count}"
        )

        # Toggle logic should handle multiple elements
        assert "allDetails.forEach" in html, "Should iterate over all details elements"

    def test_thinking_content_collapsible(self):
        """Test that thinking content is also collapsible when long."""
        long_thinking = "This is a very long thinking process " * 20
        thinking_content = {
            "type": "thinking",
            "thinking": long_thinking,
        }

        message = self._create_assistant_message([thinking_content])

        html = generate_html([message], "Test Thinking")

        # Thinking content should also be collapsible
        assert 'class="collapsible-details"' in html, (
            "Thinking content should be collapsible"
        )
        assert "💭 Thinking" in html, "Should show thinking icon"

    def test_tool_result_collapsible(self):
        """Test that tool results are also collapsible when long."""
        long_result = "This is a very long tool result " * 20
        tool_result_content = {
            "type": "tool_result",
            "tool_use_id": "test_tool",
            "content": long_result,
            "is_error": False,
        }

        message = self._create_assistant_message([tool_result_content])

        html = generate_html([message], "Test Tool Result")

        # Tool result should be collapsible
        assert 'class="collapsible-details"' in html, (
            "Tool result should be collapsible"
        )
        assert "🧰" in html, "Should show tool result icon"

    def test_tool_result_error_single_icon(self):
        """Test that error tool results show only the error icon, not double icons."""
        error_result = "Error: Something went wrong"
        tool_result_content = {
            "type": "tool_result",
            "tool_use_id": "test_tool",
            "content": error_result,
            "is_error": True,
        }

        message = self._create_assistant_message([tool_result_content])

        html = generate_html([message], "Test Error Tool Result")

        # Should have the error icon, not the tool result icon
        assert "🚨" in html, "Should show error icon"
        # Should NOT have double icons (🧰 followed by 🚨)
        assert "🧰 🚨" not in html, "Should not have double icons"
        # The error icon should be before "Error" text
        assert "🚨 Error" in html, "Should show error icon with Error text"

    def test_tool_param_collapsible_hides_summary_when_open(self):
        """Test that CSS hides summary when tool param details is open."""
        long_content = "x" * 300
        tool_use_content = {
            "type": "tool_use",
            "id": "test_tool",
            "name": "TestTool",
            "input": {"content": long_content},
        }

        message = self._create_assistant_message([tool_use_content])

        html = generate_html([message], "Test Param Visibility")

        # Check CSS rule that hides summary when details is open
        assert ".tool-param-collapsible[open] > summary" in html, (
            "CSS should target open collapsible summary"
        )
        assert "display: none" in html, "CSS should hide summary when details is open"
