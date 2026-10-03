"""Unit tests for the minimal theme's template helpers and page structure (P3a).

The helpers in ``claude_code_log.html.minimal_theme`` feed the minimal
row's gutter (role label, short time, compact tokens) and the header's
meta line; they're called only from the template's minimal branches.
Browser behaviour (toggle, toolbar, layout) is in
``test_minimal_theme_browser.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from claude_code_log.converter import convert_jsonl_to, load_transcript
from claude_code_log.html import minimal_theme
from claude_code_log.html.renderer import generate_html

TEST_DATA = Path(__file__).parent / "test_data"
REPRESENTATIVE = TEST_DATA / "representative_messages.jsonl"


def _msg(**content: Any) -> Any:
    return cast(Any, SimpleNamespace(content=SimpleNamespace(**content)))


class TestRoleLabel:
    @pytest.mark.parametrize(
        ("classes", "label"),
        [
            ("user", "User"),
            ("user steering", "Steer"),
            ("user slash-command", "Command"),
            ("user command-output", "Output"),
            ("user compacted", "Compacted"),
            ("user task-notification", "Async"),
            ("user teammate", "Teammate"),
            ("assistant", "Assistant"),
            ("assistant sidechain agent-depth-1", "Agent"),
            ("thinking", "Thinking"),
            ("tool_result", "Result"),
            ("tool_result error", "Error"),
            ("system system-info", "System"),
            ("system system-warning", "Warning"),
            ("system system-error", "Error"),
            ("system system-hook", "Hook"),
            ("system system-away-summary", "Recap"),
            ("bash-input", "Bash"),
            ("bash-output", "Output"),
            ("tool_use workflow_phase", "Phase"),
            ("tool_use workflow_agent", "Agent"),
            ("unknown", "Unknown"),
        ],
    )
    def test_labels_follow_the_css_taxonomy(self, classes: str, label: str) -> None:
        assert minimal_theme.role_label(_msg(), classes) == label

    def test_tool_use_is_labelled_by_tool_name(self) -> None:
        assert minimal_theme.role_label(_msg(tool_name="Read"), "tool_use") == "Read"
        assert minimal_theme.role_label(_msg(), "tool_use memory") == "Tool"


class TestGenericTitle:
    @pytest.mark.parametrize(
        "title",
        [
            "",
            None,
            "User",
            "🤷 User",
            "🤖 Assistant",
            "💭 Thinking",
            "🔗 Sub-assistant",
        ],
    )
    def test_role_only_titles_are_generic(self, title: Any) -> None:
        assert minimal_theme.is_generic_title(title)

    @pytest.mark.parametrize(
        "title",
        [
            "📝 Edit <span class='tool-summary'>/tmp/x.py</span>",
            "🔄 Async result <span>Agent &quot;x&quot; completed</span>",
            "🚨 Error",
        ],
    )
    def test_informative_titles_stay(self, title: str) -> None:
        assert not minimal_theme.is_generic_title(title)


class TestCompactNumbers:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (0, "0"),
            (950, "950"),
            (1000, "1k"),
            (9400, "9.4k"),
            (61234, "61.2k"),
            (182345, "182k"),
            (1_400_000, "1.4M"),
            (2_000_000_000, "2B"),
        ],
    )
    def test_compact_count(self, value: int, text: str) -> None:
        assert minimal_theme.compact_count(value) == text

    def test_token_usage_is_context_in_and_output(self) -> None:
        usage = "Input: 3 | Output: 1400 | Cache Creation: 1000 | Cache Read: 60200"
        assert minimal_theme.compact_token_usage(usage) == "61.2k · 1.4k"
        assert (
            minimal_theme.compact_token_usage("Input: 25 | Output: 120") == "25 · 120"
        )
        assert minimal_theme.compact_token_usage(None) == ""
        assert minimal_theme.compact_token_usage("no numbers") == ""

    def test_gutter_time(self) -> None:
        assert minimal_theme.gutter_time("2025-07-03 15:50:07") == "15:50:07"
        assert minimal_theme.gutter_time("15:50:07") == "15:50:07"
        assert minimal_theme.gutter_time("") == ""


class TestPageMeta:
    def test_single_session(self) -> None:
        sessions = [
            {
                "id": "3f9a21c4-aaaa",
                "timestamp_range": "2026-10-03 14:02:00 - 2026-10-03 14:31:00",
                "message_count": 46,
                "token_summary": "Token usage – Input: 2,000 | Output: 9,400 | Cache Read: 180,000",
            }
        ]
        assert minimal_theme.page_meta(sessions, None) == [
            "3f9a21c4",
            "2026-10-03 14:02:00 - 2026-10-03 14:31:00",
            "46 msgs",
            "182k in",
            "9.4k out",
        ]

    def test_several_sessions_sum_and_skip_branch_rows(self) -> None:
        sessions = [
            {
                "id": "a",
                "message_count": 3,
                "token_summary": "Token usage – Input: 10 | Output: 5",
            },
            {
                "id": "b",
                "message_count": 4,
                "token_summary": "Token usage – Input: 20 | Output: 5",
            },
            {"id": "b@x", "is_branch": True, "message_count": 99},
            {"id": "f", "is_fork_point": True},
        ]
        assert minimal_theme.page_meta(sessions, None) == [
            "2 sessions",
            "7 msgs",
            "30 in",
            "10 out",
        ]

    def test_page_stats_win_on_paginated_pages(self) -> None:
        sessions = [{"id": "a", "message_count": 3}, {"id": "b", "message_count": 4}]
        stats = {
            "message_count": 120,
            "date_range": "2026-10-01 - 2026-10-03",
            "token_summary": "Input: 1,000 | Output: 2,000",
        }
        assert minimal_theme.page_meta(sessions, stats) == [
            "2 sessions",
            "2026-10-01 - 2026-10-03",
            "120 msgs",
            "1k in",
            "2k out",
        ]

    def test_nothing_to_say(self) -> None:
        assert minimal_theme.page_meta([], None) == []


class TestMinimalPage:
    def test_rows_carry_gutter_spans(self) -> None:
        html = generate_html(
            load_transcript(REPRESENTATIVE, silent=True), "T", theme="minimal"
        )
        assert "<span class='mn-role'>User</span>" in html
        assert "<span class='mn-role'>Edit</span>" in html
        assert "<span class='mn-time'>15:50:07</span>" in html
        assert (
            "<span class='mn-tok' title='Input: 25 | Output: 120'>25 · 120</span>"
            in html
        )
        assert "class='mn-title mn-generic'" in html
        assert "<div class='mn-stage'><div id=\"transcript\">" in html
        assert "<nav class='mn-toolbar' aria-label='Transcript tools'>" in html
        # The head applies a stored colour scheme before the body exists.
        head = html.split("</head>", 1)[0]
        assert "claude-code-log:theme" in head
        assert "--bg: #14161a" in head  # dark tokens
        assert "theme-minimal" not in generate_html(
            load_transcript(REPRESENTATIVE, silent=True), "T", theme="classic"
        )

    def test_paginated_minimal_page_keeps_the_next_link_markers(
        self, tmp_path: Path
    ) -> None:
        """converter's bounded read expects the nav block early in the page."""
        from claude_code_log.converter import _NAV_BLOCK_PREFIX_CHARS

        project = tmp_path / "project"
        source = (
            TEST_DATA
            / "real_projects"
            / "-Users-dain-workspace-coderabbit-review-helper"
        )
        import shutil

        shutil.copytree(source, project)
        convert_jsonl_to("html", project, silent=True, theme="minimal", page_size=30)
        first = (project / "combined_transcripts.html").read_text(encoding="utf-8")
        start = first.index("<!-- PAGINATION_NEXT_LINK_START -->")
        assert "<!-- PAGINATION_NEXT_LINK_END -->" in first
        assert start < _NAV_BLOCK_PREFIX_CHARS
