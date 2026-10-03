"""Unit tests for the minimal theme's template helpers and page structure (P3a/P3b).

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


class TestCallTitle:
    """The call line drops the pictograph and the name the gutter repeats."""

    def _split(self, title: str, classes: str = "tool_use", **content: Any) -> Any:
        return minimal_theme.call_title(title, _msg(**content), classes)

    def test_tool_name_and_emoji_move_to_the_hidden_prefix(self) -> None:
        parts = self._split(
            "📝 Edit <span class='tool-summary'>/tmp/x.py</span>", tool_name="Edit"
        )
        assert parts["prefix"] == "📝 Edit "
        assert parts["rest"] == "<span class='tool-summary'>/tmp/x.py</span>"
        assert not parts["generic"]
        # The whole title survives as plain text for the tooltip.
        assert parts["tooltip"] == "📝 Edit /tmp/x.py"

    def test_name_only_titles_become_generic(self) -> None:
        for title, name in (
            ("🛠️ TodoWrite", "TodoWrite"),
            ("📝 Todo List", "TodoWrite"),
        ):
            parts = self._split(title, tool_name=name)
            assert parts["generic"], title
            assert parts["rest"] == ""

    def test_role_label_and_separator(self) -> None:
        hook = self._split("🪝 Hook · Stop", "system system-hook-attachment")
        assert (hook["prefix"], hook["rest"]) == ("🪝 Hook · ", "Stop")
        phase = self._split("🧩 Phase: Map", "tool_use workflow_phase")
        assert phase["rest"] == "Map"
        error = self._split("🚨 Error", "tool_result error")
        assert error["generic"]

    def test_task_tools_drop_the_task_word(self) -> None:
        parts = self._split(
            "🛠️ Task <code>#1</code> <span class='tool-summary'>Add</span>",
            tool_name="TaskCreate",
        )
        assert parts["rest"].startswith("<code>#1</code>")

    def test_a_longer_word_is_not_the_name(self) -> None:
        parts = self._split("🛠️ Tasks remaining", tool_name="Task")
        assert parts["rest"] == "Tasks remaining"

    def test_ascii_symbols_are_content_not_decoration(self) -> None:
        parts = self._split("/test-command", "user slash-command")
        assert parts["prefix"] == ""
        assert parts["rest"] == "/test-command"

    def test_title_hint_joins_the_tooltip(self) -> None:
        parts = minimal_theme.call_title(
            "💻 Bash <span class='tool-summary'>Run it</span>",
            _msg(tool_name="Bash"),
            "tool_use",
            "ID: toolu_1",
        )
        assert parts["tooltip"] == "💻 Bash Run it · ID: toolu_1"

    def test_role_only_titles_stay_generic(self) -> None:
        assert self._split("🤷 User", "user")["generic"]
        assert self._split("🔗 Sub-assistant", "assistant sidechain")["generic"]
        assert self._split("", "assistant")["generic"]


class TestSessionHeader:
    def test_summary_and_short_id(self) -> None:
        content = SimpleNamespace(
            title="Fix the bug • abcdef12",
            session_id="abcdef1234",
            summary="Fix the bug",
        )
        html = minimal_theme.session_header(content, "Fix the bug • abcdef12")
        assert "<span class='mn-sh-sum' title='Fix the bug'>Fix the bug</span>" in html
        assert (
            "<span class='mn-sh-id' title='Session abcdef1234'>abcdef12</span>" in html
        )
        assert "•" not in html

    def test_keeps_badges_and_backlink(self) -> None:
        content = SimpleNamespace(
            title="abcdef12", session_id="abcdef1234", summary=None
        )
        formatted = (
            '<a class="session-backlink">↳ continues from x</a>abcdef12'
            '<span class="session-team-badge">Team</span>'
        )
        html = minimal_theme.session_header(content, formatted)
        assert html.startswith('<a class="session-backlink">')
        assert "<span class='mn-sh-id'" in html
        assert html.endswith('<span class="session-team-badge">Team</span>')


class TestPygmentsDark:
    def test_committed_sheet_matches_the_generator(self) -> None:
        """Drift guard: regenerate with scripts/generate_minimal_pygments_css.py."""
        import importlib.util

        script = (
            Path(__file__).parent.parent
            / "scripts"
            / "generate_minimal_pygments_css.py"
        )
        spec = importlib.util.spec_from_file_location("gen_pygments_dark", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.OUTPUT.read_text(encoding="utf-8") == module.build_css()

    def test_sheet_is_scoped_to_both_dark_selectors(self) -> None:
        sheet = (
            Path(str(minimal_theme.__file__)).parent
            / "templates"
            / "components"
            / "minimal"
            / "pygments_dark.css"
        ).read_text(encoding="utf-8")
        rules = [
            line.strip()
            for line in sheet.splitlines()
            if line.strip().startswith((":root", ".highlight"))
        ]
        assert rules
        media = [r for r in rules if r.startswith(':root:not([data-theme="light"])')]
        forced = [r for r in rules if r.startswith(':root[data-theme="dark"]')]
        assert len(media) == len(forced) == len(rules) / 2
        assert all(" .theme-minimal .highlight ." in r for r in rules)
        # The classic bold keyword must not leak into dark mode.
        assert any(
            r.endswith(".highlight .k { font-weight: normal; color: #ff7b72 }")
            for r in forced
        )


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
        # Call line: emoji and tool name hidden (kept for search), path shown.
        assert (
            "<span class='mn-tn'>📝 Edit </span><span class='tool-summary'>"
            "/tmp/decorator_example.py</span>" in html
        )
        # Compact session header: summary, short id, model.
        assert "<span class='mn-sh-sum' title='User learned about" in html
        assert (
            "<span class='mn-sh-id' title='Session test_session'>test_ses</span>"
            in html
        )
        assert "Session: User learned" not in html
        # P3b sheets are inlined.
        assert ':root[data-theme="dark"] .theme-minimal .highlight .k' in html
        assert "--cc-blue-bg: color-mix(" in html
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
