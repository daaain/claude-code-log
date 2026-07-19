#!/usr/bin/env python3
"""Test cases for AskUserQuestion tool rendering."""

from claude_code_log.factories.tool_factory import parse_askuserquestion_output
from claude_code_log.html import (
    format_askuserquestion_input,
    format_askuserquestion_output,
    format_askuserquestion_result,
)
from claude_code_log.models import (
    AskUserQuestionAnswer,
    AskUserQuestionInput,
    AskUserQuestionItem,
    AskUserQuestionOption,
    AskUserQuestionOutput,
    ToolResultContent,
)


class TestAskUserQuestionRendering:
    """Test AskUserQuestion tool rendering functionality."""

    def test_format_askuserquestion_multiple_questions(self):
        """Test AskUserQuestion formatting with multiple questions."""
        ask_input = AskUserQuestionInput(
            questions=[
                AskUserQuestionItem(
                    question="Should tar archives be processed recursively?",
                    header="Filesystem tar",
                    options=[
                        AskUserQuestionOption(
                            label="Yes, both filesystem and embedded",
                            description="Treat .tar/.tar.gz like .zip",
                        ),
                        AskUserQuestionOption(
                            label="Only embedded tar archives",
                            description="Only process tar archives inside ZIP files",
                        ),
                    ],
                    multiSelect=False,
                ),
                AskUserQuestionItem(
                    question="Which tar formats should be supported?",
                    header="Tar formats",
                    options=[
                        AskUserQuestionOption(
                            label=".tar and .tar.gz only",
                            description="Most common",
                        ),
                        AskUserQuestionOption(
                            label="Also .tgz",
                            description="Include .tgz alias",
                        ),
                    ],
                    multiSelect=False,
                ),
            ]
        )

        html = format_askuserquestion_input(ask_input)

        # Check overall structure
        assert 'class="askuserquestion-content"' in html
        assert 'class="question-block"' in html

        # Check both questions are rendered
        assert "Should tar archives be processed recursively?" in html
        assert "Which tar formats should be supported?" in html

        # Check headers
        assert "Filesystem tar" in html
        assert "Tar formats" in html

        # Check options
        assert "Yes, both filesystem and embedded" in html
        assert "Only embedded tar archives" in html
        assert ".tar and .tar.gz only" in html

        # Check descriptions
        assert "Treat .tar/.tar.gz like .zip" in html
        assert "Only process tar archives inside ZIP files" in html

        # Check question label
        assert "Q:" in html

        # Check select hint
        assert "(select one)" in html

    def test_format_askuserquestion_single_question(self):
        """Test AskUserQuestion formatting with a single question."""
        ask_input = AskUserQuestionInput(
            questions=[
                AskUserQuestionItem(
                    question="How should errors be reported?",
                    header="Error format",
                    options=[
                        AskUserQuestionOption(
                            label="Option A", description="Comment line"
                        ),
                        AskUserQuestionOption(
                            label="Option B", description="Marker entry"
                        ),
                        AskUserQuestionOption(
                            label="Option C", description="Extra field"
                        ),
                    ],
                    multiSelect=False,
                )
            ]
        )

        html = format_askuserquestion_input(ask_input)

        # Check structure
        assert 'class="askuserquestion-content"' in html
        assert "How should errors be reported?" in html
        assert "Error format" in html

        # Check all three options
        assert "Option A" in html
        assert "Option B" in html
        assert "Option C" in html

    def test_format_askuserquestion_multiselect(self):
        """Test AskUserQuestion formatting with multiSelect enabled."""
        ask_input = AskUserQuestionInput(
            questions=[
                AskUserQuestionItem(
                    question="Which features should be enabled?",
                    options=[
                        AskUserQuestionOption(label="Feature A"),
                        AskUserQuestionOption(label="Feature B"),
                        AskUserQuestionOption(label="Feature C"),
                    ],
                    multiSelect=True,
                )
            ]
        )

        html = format_askuserquestion_input(ask_input)

        # Check multi-select hint
        assert "(select multiple)" in html
        assert "Feature A" in html
        assert "Feature B" in html
        assert "Feature C" in html

    def test_format_askuserquestion_legacy_single_question(self):
        """Test backwards compatibility with single 'question' key format."""
        ask_input = AskUserQuestionInput(question="What is your preference?")

        html = format_askuserquestion_input(ask_input)

        # Should still render the question
        assert 'class="askuserquestion-content"' in html
        assert "What is your preference?" in html
        assert "Q:" in html

    def test_format_askuserquestion_no_options(self):
        """Test AskUserQuestion formatting without options."""
        ask_input = AskUserQuestionInput(
            questions=[
                AskUserQuestionItem(
                    question="Please describe the issue in detail.",
                    header="Issue",
                )
            ]
        )

        html = format_askuserquestion_input(ask_input)

        # Should render without options list
        assert "Please describe the issue in detail." in html
        assert "Issue" in html
        # Should not have options-related elements
        assert "question-options" not in html
        assert "(select" not in html

    def test_format_askuserquestion_empty_input(self):
        """Test AskUserQuestion with empty questions returns 'No question' message."""
        ask_input = AskUserQuestionInput()  # Empty questions list

        html = format_askuserquestion_input(ask_input)

        # Should show 'No question' message
        assert "askuserquestion-content" in html
        assert "No question" in html

    def test_format_askuserquestion_escapes_html(self):
        """Test that HTML special characters are escaped."""
        ask_input = AskUserQuestionInput(
            questions=[
                AskUserQuestionItem(
                    question="Use <script> tag or &amp; symbol?",
                    header="HTML <test>",
                    options=[
                        AskUserQuestionOption(
                            label="<option>", description="Test & verify"
                        )
                    ],
                )
            ]
        )

        html = format_askuserquestion_input(ask_input)

        # HTML entities should be escaped
        assert "&lt;script&gt;" in html
        assert "&lt;test&gt;" in html
        assert "&lt;option&gt;" in html
        # Input "&amp;" should be escaped to "&amp;amp;"
        assert "&amp;amp;" in html


class TestAskUserQuestionResultRendering:
    """Test AskUserQuestion tool result rendering functionality."""

    def test_format_result_single_qa(self):
        """Test formatting a result with a single Q&A pair."""
        content = (
            'User has answered your question: "What is your preference?"="Option A". '
            "You can now continue with the user's answers in mind."
        )

        html = format_askuserquestion_result(content)

        # Should render styled HTML
        assert 'class="askuserquestion-content askuserquestion-result"' in html
        assert 'class="question-block answered"' in html
        assert "What is your preference?" in html
        assert "Option A" in html
        assert "Q:" in html
        assert "A:" in html

    def test_format_result_multiple_qa(self):
        """Test formatting a result with multiple Q&A pairs."""
        content = (
            "User has answered your questions: "
            '"Should tar archives be processed recursively?"="Yes, both filesystem and embedded", '
            '"Which tar formats should be supported?"="Also .tgz". '
            "You can now continue with the user's answers in mind."
        )

        html = format_askuserquestion_result(content)

        # Should have two question blocks
        assert html.count('class="question-block answered"') == 2

        # Check both Q&A pairs
        assert "Should tar archives be processed recursively?" in html
        assert "Yes, both filesystem and embedded" in html
        assert "Which tar formats should be supported?" in html
        assert "Also .tgz" in html

    def test_format_result_not_answered(self):
        """Test that non-answer results return empty string."""
        content = "User cancelled the question."

        html = format_askuserquestion_result(content)

        # Should return empty to fall through to default handling
        assert html == ""

    def test_format_result_error_message(self):
        """Test that error messages return empty string."""
        content = "Error: Could not parse user response."

        html = format_askuserquestion_result(content)

        assert html == ""

    def test_format_result_escapes_html(self):
        """Test that HTML in questions/answers is escaped."""
        content = (
            'User has answered your question: "Use <script> tag?"="Yes, use <b>bold</b>". '
            "You can now continue with the user's answers in mind."
        )

        html = format_askuserquestion_result(content)

        # HTML should be escaped
        assert "&lt;script&gt;" in html
        assert "&lt;b&gt;bold&lt;/b&gt;" in html
        # Should not have raw HTML tags
        assert "<script>" not in html
        assert "<b>" not in html

    def test_format_result_malformed_no_closing(self):
        """Test handling of malformed result without closing sentence."""
        content = 'User has answered your question: "Q"="A"'

        html = format_askuserquestion_result(content)

        # Should return empty due to missing closing sentence
        assert html == ""

    def test_format_result_with_quotes_in_answer(self):
        """Test handling answers that might contain special characters."""
        # Note: This tests the regex pattern's ability to handle the format
        content = (
            'User has answered your question: "Preference?"="Option with comma, here". '
            "You can now continue with the user's answers in mind."
        )

        html = format_askuserquestion_result(content)

        assert "Preference?" in html
        assert "Option with comma, here" in html

    def test_format_result_new_wording(self):
        """Regression for #180: the current harness wording must parse.

        The result sentence changed from 'User has answered your questions:'
        to 'Your questions have been answered:'. Both must render styled Q&A.
        """
        content = (
            'Your questions have been answered: "Which DB?"="PostgreSQL". '
            "You can now continue with these answers in mind."
        )
        html = format_askuserquestion_result(content)
        assert 'class="question-block answered"' in html
        assert "Which DB?" in html
        assert "PostgreSQL" in html


def _result_content(tool_use_result):
    """A ToolResultContent whose text is irrelevant (structured path wins)."""
    return ToolResultContent(
        type="tool_result",
        tool_use_id="toolu_x",
        content="Your questions have been answered: ... You can now continue.",
    ), tool_use_result


class TestAskUserQuestionParser:
    """parse_askuserquestion_output: structured-first, text fallback (#180)."""

    def test_parse_structured_carries_options(self):
        """Structured toolUseResult yields answers enriched with options."""
        tool_use_result = {
            "questions": [
                {
                    "question": "Which DB?",
                    "header": "Database",
                    "multiSelect": False,
                    "options": [
                        {"label": "PostgreSQL", "description": "Relational."},
                        {"label": "SQLite", "description": "Embedded."},
                    ],
                }
            ],
            "answers": {"Which DB?": "PostgreSQL"},
        }
        tool_result, tur = _result_content(tool_use_result)
        out = parse_askuserquestion_output(tool_result, None, tur)
        assert out is not None
        assert len(out.answers) == 1
        qa = out.answers[0]
        assert qa.question == "Which DB?"
        assert qa.answer == "PostgreSQL"
        assert qa.header == "Database"
        assert [o.label for o in qa.options] == ["PostgreSQL", "SQLite"]
        assert qa.multi_select is False

    def test_parse_structured_preferred_over_text(self):
        """When both are present, the structured answers map wins."""
        tool_use_result = {"answers": {"Q?": "A from structure"}}
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_x",
            content='Your questions have been answered: "Q?"="A from text". '
            "You can now continue.",
        )
        out = parse_askuserquestion_output(tool_result, None, tool_use_result)
        assert out is not None
        assert out.answers[0].answer == "A from structure"

    def test_parse_text_fallback_new_wording(self):
        """No structured data → fall back to the new-wording summary string."""
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_x",
            content='Your questions have been answered: "Q1"="A1", "Q2"="A2". '
            "You can now continue with these answers in mind.",
        )
        out = parse_askuserquestion_output(tool_result, None, None)
        assert out is not None
        assert [(a.question, a.answer) for a in out.answers] == [
            ("Q1", "A1"),
            ("Q2", "A2"),
        ]
        # Text fallback has no option context.
        assert out.answers[0].options == []

    def test_parse_non_answer_returns_none(self):
        tool_result = ToolResultContent(
            type="tool_result", tool_use_id="toolu_x", content="User cancelled."
        )
        assert parse_askuserquestion_output(tool_result, None, None) is None

    def test_parse_legacy_paren_s_wording(self):
        """The legacy literal 'question(s)' wording also parses (CR on #189)."""
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_x",
            content='User has answered your question(s): "Q1"="A1". '
            "You can now continue with the user's answers in mind.",
        )
        out = parse_askuserquestion_output(tool_result, None, None)
        assert out is not None
        assert [(a.question, a.answer) for a in out.answers] == [("Q1", "A1")]

    def test_parse_clarify_rejection_free_form(self):
        """The 'clarify' rejection (free-form reply) parses to per-question
        answers — free-form text where given, empty where not (#180)."""
        content = (
            "The user doesn't want to proceed with this tool use. "
            "To tell you how to proceed, the user said:\n"
            "The user wants to clarify these questions.\n\n"
            "    Questions asked:\n"
            '- "Which DB?"\n'
            "  Answer: Actually, let's use DuckDB\n"
            '- "Enable caching?"\n'
            "  (No answer provided)"
        )
        tool_result = ToolResultContent(
            type="tool_result", tool_use_id="toolu_x", content=content
        )
        out = parse_askuserquestion_output(tool_result, None, None)
        assert out is not None
        assert [(a.question, a.answer) for a in out.answers] == [
            ("Which DB?", "Actually, let's use DuckDB"),
            ("Enable caching?", ""),
        ]


class TestAskUserQuestionEnrichedOutput:
    """format_askuserquestion_output: options with the chosen one marked."""

    def _output(self, multi_select=False, answer="PostgreSQL"):
        return AskUserQuestionOutput(
            answers=[
                AskUserQuestionAnswer(
                    question="Which DB?",
                    answer=answer,
                    header="Database",
                    multi_select=multi_select,
                    options=[
                        AskUserQuestionOption(label="PostgreSQL", description="Rel."),
                        AskUserQuestionOption(label="SQLite", description="Emb."),
                    ],
                )
            ],
            raw_message="",
        )

    def test_selected_option_marked_others_not(self):
        html = format_askuserquestion_output(self._output())
        # The chosen option gets the selected class + check mark.
        assert 'class="question-option selected"' in html
        assert "option-check" in html
        # The unchosen option is a plain option (no 'selected').
        assert html.count('class="question-option selected"') == 1
        assert 'class="question-option"' in html
        # Header preserved.
        assert "Database" in html

    def test_only_matched_option_is_selected(self):
        """When the answer matches an option, exactly that option is selected
        and no extra free-form block is appended."""
        html = format_askuserquestion_output(self._output())
        # One selected option, no second selected block for the answer text.
        assert html.count('class="question-option selected"') == 1

    def test_free_text_answer_renders_as_selected_block(self):
        """A free-form answer (matches no option) renders as an extra selected
        block after the offered options (issue #180)."""
        html = format_askuserquestion_output(self._output(answer="MongoDB"))
        # The offered options stay unselected; the typed reply is the choice.
        assert html.count('class="question-option selected"') == 1
        assert "MongoDB" in html
        assert "PostgreSQL" in html  # offered options still shown

    def test_multiselect_marks_each_chosen_option(self):
        html = format_askuserquestion_output(
            self._output(multi_select=True, answer="PostgreSQL, SQLite")
        )
        assert html.count('class="question-option selected"') == 2

    def test_multiparagraph_free_form_answer_is_inline_safe(self):
        """A multi-paragraph free-form reply must not emit block <p> inside the
        inline <strong> wrapper of the selected block (CR on PR #189)."""
        html = format_askuserquestion_output(
            self._output(answer="First paragraph.\n\nSecond paragraph.")
        )
        assert "<strong><p>" not in html
        assert "</p>" not in html  # no stray block tags in the inline block
        assert "<br>" in html  # paragraph break folded to <br>
        assert "First paragraph." in html and "Second paragraph." in html


def _render_transcript(entries: list[dict]) -> str:
    import json
    import tempfile
    from pathlib import Path

    from claude_code_log.converter import load_transcript
    from claude_code_log.html.renderer import generate_html

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        path = Path(f.name)
    try:
        return generate_html(load_transcript(path), "AUQ")
    finally:
        path.unlink(missing_ok=True)


def _auq_tool_use(uuid: str, parent, tool_id: str, questions: list[dict]) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-05-29T11:02:40Z",
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s",
        "version": "2.1.156",
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "id": "m-" + uuid,
            "type": "message",
            "model": "claude",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": questions},
                }
            ],
        },
    }


class TestAskUserQuestionCollapse:
    """Integration (#180): answered pairs collapse to one self-contained card;
    unpaired inputs are kept; free-form replies aren't shown as errors."""

    QUESTIONS = [
        {
            "question": "Use `PostgreSQL` or SQLite?",
            "header": "Database",
            "multiSelect": False,
            "options": [
                {"label": "PostgreSQL", "description": "Robust `relational` DB."},
                {"label": "SQLite", "description": "Embedded."},
            ],
        }
    ]

    def test_answered_pair_collapses_input_card(self):
        q = self.QUESTIONS
        entries = [
            _auq_tool_use("a1", None, "t1", q),
            {
                "type": "user",
                "timestamp": "2026-05-29T11:02:47Z",
                "parentUuid": "a1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s",
                "version": "2.1.156",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "Your questions have been answered: "
                            '"Use `PostgreSQL` or SQLite?"="PostgreSQL". '
                            "You can now continue.",
                        }
                    ],
                },
                "toolUseResult": {
                    "questions": q,
                    "answers": {"Use `PostgreSQL` or SQLite?": "PostgreSQL"},
                },
            },
        ]
        html = _render_transcript(entries)
        # Input card is ghosted: the "Asking questions..." title is gone.
        assert "❓ Asking questions" not in html
        # The result card highlights the chosen option…
        assert 'class="question-option selected"' in html
        # …and Markdown in the question renders (backticks → <code>).
        assert "<code>PostgreSQL</code>" in html

    def test_unpaired_question_keeps_input_card(self):
        """A tool_use with no answering result (session blocked) still shows."""
        html = _render_transcript([_auq_tool_use("a1", None, "t1", self.QUESTIONS)])
        assert "❓ Asking questions" in html

    def test_free_form_reply_not_rendered_as_error(self):
        q = self.QUESTIONS
        entries = [
            _auq_tool_use("a1", None, "t1", q),
            {
                "type": "user",
                "timestamp": "2026-05-29T11:02:47Z",
                "parentUuid": "a1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": "s",
                "version": "2.1.156",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": "The user doesn't want to proceed with this "
                            "tool use. The user wants to clarify these questions.\n"
                            "    Questions asked:\n"
                            '- "Use `PostgreSQL` or SQLite?"\n'
                            "  Answer: Neither — let's use DuckDB",
                        }
                    ],
                },
            },
        ]
        html = _render_transcript(entries)
        # The clarify result is shown as a normal answered card, not "Error".
        assert ">Error<" not in html
        assert "DuckDB" in html
        # The user's free-form reply is the selected block.
        assert 'class="question-option selected"' in html
