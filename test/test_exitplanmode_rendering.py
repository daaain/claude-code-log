#!/usr/bin/env python3
"""Test cases for ExitPlanMode tool rendering."""

from claude_code_log.renderer import (
    format_exitplanmode_content,
    format_exitplanmode_result,
)
from claude_code_log.models import ToolUseContent


class TestExitPlanModeRendering:
    """Test ExitPlanMode tool rendering functionality."""

    def test_format_exitplanmode_with_plan(self):
        """Test ExitPlanMode tool use rendering with plan content."""
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_01test123",
            name="ExitPlanMode",
            input={
                "plan": "# My Plan\n\n## Overview\n\nThis is the plan.\n\n## Steps\n\n1. First step\n2. Second step"
            },
        )

        html = format_exitplanmode_content(tool_use)

        # Should render as markdown in a plan-content div
        assert 'class="plan-content' in html
        # Markdown should be rendered (h1, h2, etc.)
        assert "<h1>" in html or "My Plan" in html
        assert "<h2>" in html or "Overview" in html

    def test_format_exitplanmode_long_plan_collapsible(self):
        """Test that long plans are rendered as collapsible."""
        # Create a plan with more than 20 lines
        long_plan = "# Long Plan\n\n" + "\n".join(
            [f"- Step {i}: Do something" for i in range(30)]
        )
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_01testlong",
            name="ExitPlanMode",
            input={"plan": long_plan},
        )

        html = format_exitplanmode_content(tool_use)

        # Should be collapsible due to length
        assert "collapsible" in html.lower() or "details" in html.lower()
        assert "plan-content" in html

    def test_format_exitplanmode_empty_plan(self):
        """Test ExitPlanMode with empty plan falls back to params table."""
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_01testempty",
            name="ExitPlanMode",
            input={},
        )

        html = format_exitplanmode_content(tool_use)

        # Should fall back to params table (empty)
        assert "plan-content" not in html

    def test_format_exitplanmode_result_approved(self):
        """Test ExitPlanMode result truncates approved plan echo."""
        result_content = """User has approved your plan. You can now start coding.

Your plan has been saved to: /home/user/.claude/plans/my-plan.md
You can refer back to it if needed during implementation.

## Approved Plan:
# My Plan

## Overview

This is the full plan that should be truncated.

## Steps

1. First step
2. Second step
"""

        processed = format_exitplanmode_result(result_content)

        # Should truncate at "## Approved Plan:"
        assert "User has approved your plan" in processed
        assert "saved to:" in processed
        assert "## Approved Plan:" not in processed
        assert "This is the full plan" not in processed
        # Should not include any extra trailing text
        assert not processed.endswith("\n")

    def test_format_exitplanmode_result_not_approved(self):
        """Test ExitPlanMode result keeps error content intact."""
        result_content = """Plan was not approved. User requested changes.

Please modify the following aspects:
- Add more detail to step 3
- Include error handling
"""

        processed = format_exitplanmode_result(result_content)

        # Should keep full content for errors
        assert "Plan was not approved" in processed
        assert "Please modify" in processed
        assert "Add more detail" in processed

    def test_format_exitplanmode_result_generic(self):
        """Test ExitPlanMode result without approval message."""
        result_content = "Some other tool result content"

        processed = format_exitplanmode_result(result_content)

        # Should return as-is
        assert processed == result_content

    def test_format_exitplanmode_escapes_html_in_result(self):
        """Test that HTML content is preserved (escaping happens later)."""
        result_content = """User has approved your plan. <script>alert('xss')</script>

Your plan has been saved.

## Approved Plan:
# Plan
"""

        processed = format_exitplanmode_result(result_content)

        # The script tag should be in the truncated content (escaping happens later)
        assert "<script>" in processed
        assert "## Approved Plan:" not in processed
        # Should end cleanly without trailing whitespace
        assert not processed.endswith("\n")
