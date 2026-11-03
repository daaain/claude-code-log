"""The teammate session of the ``minimal_showcase`` project.

``test_data/minimal_showcase/55550000-…-0002.jsonl`` gathers the peer
message bodies a reader looks at to see how teammate messages render:
a JSON ``idle_notification`` with a long Markdown ``result``, a prose
body, a ``key: value`` body, a JSON body nested past the data-table
depth bound, and the harness framing around them. Render it with
``claude-code-log test/test_data/minimal_showcase --theme minimal``.
This pins that each case is still there and still renders as intended,
so the fixture can't rot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_code_log.converter import load_directory_transcripts
from claude_code_log.html.renderer import generate_html

SHOWCASE = Path(__file__).parent / "test_data" / "minimal_showcase"
SESSION = "55550000-0000-4000-8000-000000000002"


@pytest.fixture(scope="module")
def html() -> str:
    entries, tree = load_directory_transcripts(SHOWCASE, silent=True)
    return generate_html(entries, "Showcase", session_tree=tree, theme="minimal")


def _body(html: str, teammate: str) -> str:
    """The rendered body of ``teammate``'s block, up to the trailing framing."""
    start = html.index(f'<span class="teammate-icon">▎</span>{teammate}</span>')
    rest = html[start:]
    return rest[: rest.index("teammate-surrounding-text")]


def test_the_session_is_in_the_project() -> None:
    assert (SHOWCASE / f"{SESSION}.jsonl").is_file()


def test_a_json_payload_is_a_params_table_with_folded_markdown(html: str) -> None:
    body = _body(html, "indexer")
    assert "tool-params-table" in body
    assert "tool-params-expand-all" in body
    assert ">idle_notification<" in body
    result = body[body.index(">result<") :]
    assert "tool-param-markdown" in result
    assert "<strong>search index</strong>" in result
    assert "<table>" in result
    assert "<li>re-run the slow-query check after the next deploy</li>" in result


def test_a_prose_body_stays_markdown(html: str) -> None:
    body = _body(html, "reviewer")
    assert "tool-params-table" not in body
    assert "<strong>migration</strong>" in body
    assert "<li>the new column is nullable, so old writers keep working;</li>" in body


def test_a_key_value_body_is_a_params_table(html: str) -> None:
    body = _body(html, "reporter")
    assert "tool-params-table" in body
    for key in ("task", "status", "rows", "duration"):
        assert f"</span>{key}</td>" in body
    assert "nightly report" in body


def test_json_past_the_depth_bound_falls_back_to_prose(html: str) -> None:
    body = _body(html, "tracer")
    assert "tool-params-table" not in body
    assert "&quot;frames&quot;: [[[[" in body  # the JSON text, as prose


def test_the_framing_is_italic_text(html: str) -> None:
    assert (
        '<div class="teammate-surrounding-text">'
        "Another Claude session sent a message:</div>"
    ) in html
    assert "This came from another Claude session" in html
