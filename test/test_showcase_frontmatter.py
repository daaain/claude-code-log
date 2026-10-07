"""The front-matter session of the ``minimal_showcase`` project.

``test_data/minimal_showcase/55550000-…-0003.jsonl`` gathers the cases a
reader looks at to see YAML front matter render as data rather than as a
setext heading:
- a Write of a note with a long Markdown ``description`` and nested
  ``metadata``;
- a Read of a skill file whose collapsed preview starts at the body;
- a user message that opens with front matter, with a backtick value and
  a ``|`` block scalar;
- an invalid YAML block shown as YAML;
- a leading ``---`` + blank line, which stays a horizontal rule.

Render it with
``claude-code-log test/test_data/minimal_showcase --theme minimal``.
This pins that each case is still there and still renders as intended,
so the fixture can't rot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.dag import SessionTree
from claude_code_log.html.renderer import generate_html
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import TranscriptEntry

SHOWCASE = Path(__file__).parent / "test_data" / "minimal_showcase"
SESSION = "55550000-0000-4000-8000-000000000003"


@pytest.fixture(scope="module")
def loaded() -> tuple[list[TranscriptEntry], SessionTree]:
    return load_directory_transcripts(SHOWCASE, silent=True)


@pytest.fixture(scope="module")
def html(loaded: tuple[list[TranscriptEntry], SessionTree]) -> str:
    entries, tree = loaded
    return generate_html(entries, "Showcase", session_tree=tree, theme="minimal")


@pytest.fixture(scope="module")
def md(loaded: tuple[list[TranscriptEntry], SessionTree]) -> str:
    return MarkdownRenderer().generate(loaded[0], "Showcase")


def _after(text: str, marker: str, length: int = 6000) -> str:
    start = text.index(marker)
    return text[start : start + length]


def test_the_session_is_in_the_project() -> None:
    assert (SHOWCASE / f"{SESSION}.jsonl").is_file()


def test_a_written_note_shows_its_front_matter_as_a_table(html: str) -> None:
    write = _after(html, "docs/release-checklist.md")
    block = write[
        write.index("class='frontmatter'") : write.index("Release checklist</h1>")
    ]
    assert "tool-params-table" in block
    assert "release-checklist</td>" in block
    # The long description folds as Markdown; metadata nests.
    assert "<strong>full test suite</strong>" in block
    assert "tool-params-nested" in block
    assert "<h2>name:" not in write


def test_a_read_skill_previews_its_body(html: str) -> None:
    read = _after(html, "skills/deploy-runbook/SKILL.md", 12000)
    preview = read[read.index("<div class='preview-content") : read.index("</summary>")]
    assert "<h1>Deploy runbook</h1>" in preview
    assert "deploy-runbook" not in preview
    full = read[read.index("class='code-full") :]
    assert "class='frontmatter'" in full
    assert "tool-params-nested" in full  # the tags list and the limits map


def test_a_user_message_opens_with_a_front_matter_table(html: str) -> None:
    title = html.index("Weekly sync notes")
    block = html[html.rindex("class='frontmatter'", 0, title) :]
    block = block[: block.index("Please turn these notes into a short summary")]
    assert "Weekly sync notes" in block and "tool-params-table" in block
    assert "<h2>title:" not in html


def test_invalid_yaml_is_shown_as_yaml(html: str) -> None:
    invalid = html[html.index('<span class="nt">status</span>') :]
    invalid = invalid[: invalid.index("Here is the draft.")]
    assert (
        '<div class="highlight">'
        in html[: html.index('<span class="nt">status</span>')][-200:]
    )
    assert '<span class="nt">note: Use when</span>' in invalid
    assert "tool-params-table" not in invalid


def test_a_leading_rule_stays_a_rule(html: str) -> None:
    rule = html[
        html.index("Next: tag the release") - 2000 : html.index("Next: tag the release")
    ]
    assert "<hr" in rule
    assert "<strong>Summary.</strong>" in rule
    assert "class='frontmatter'" not in rule


class TestMarkdownOutput:
    def test_titles_skip_front_matter_and_rules(self, md: str) -> None:
        assert "## 🤷 User: *Please turn these notes into a short summary…*" in md
        assert "### 🤖 Assistant: *Here is the draft.*" in md
        assert "*---*" not in md

    def test_a_backtick_value_stays_in_its_code_span(self, md: str) -> None:
        assert "**command:** ``run `make check` before pushing``" in md

    def test_a_block_scalar_is_fenced_not_a_heading(self, md: str) -> None:
        assert (
            "**agenda:**\n\n```\nReview the open items.\n\n"
            "# Not a heading: this line is part of the value\n"
        ) in md

    def test_invalid_yaml_is_a_yaml_fence(self, md: str) -> None:
        assert (
            "> ```yaml\n> status: draft\n> note: Use when: the parser fails\n> ```"
            in md
        )


def test_a_teammate_body_opens_with_a_front_matter_table(html: str) -> None:
    badge = html.index(
        '<span class="teammate-icon">▎</span>archivist</span><span class="teammate-summary">Handoff'
    )
    body = html[badge:]
    body = body[: body.index("teammate-surrounding-text")]
    assert "class='frontmatter'" in body and "handoff-note</td>" in body
    assert "<h1>Handoff</h1>" in body
