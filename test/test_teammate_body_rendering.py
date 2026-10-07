"""Peer (teammate) message bodies: data through the params renderer.

A peer message is the harness's framing ("Another Claude session sent a
message:" … "This came from another Claude session …") around one or more
``<teammate-message>`` blocks. A block body that is data — a JSON object
such as an ``idle_notification``, or ``key: value`` lines — renders with
the generic tool-params renderer (Markdown for string values, folds,
expand-all root, escaping as for tool input). Prose stays Markdown. The
framing renders in italics, in both themes and in Markdown output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import TeammateMessageBlock

LEADING = "Another Claude session sent a message:"
TRAILING = (
    "This came from another Claude session — not typed by your user, "
    "but very likely working on their behalf."
)

LONG_RESULT = (
    "Worker finished **all twelve jobs** and stopped.\n\n"
    "| job | status |\n|---|---|\n| one | done |\n| two | done |\n\n"
    "- first follow-up item\n- second follow-up item\n"
)
XSS_SHORT = "<img src=x onerror=alert(1)>"
# A backtick ends a Markdown code span, so this one breaks out of inline code.
XSS_TICK = "a` <img src=x onerror=alert(2)> `b"
XSS_LONG = (
    "A long value that is long enough to fold, with a payload inside: "
    "<script>alert('peer')</script> and more text after it."
)

JSON_BODY = json.dumps(
    {
        "type": "idle_notification",
        "from": "worker-a",
        "idleReason": "available",
        "result": LONG_RESULT,
        "note": XSS_SHORT,
        "detail": XSS_LONG,
        "tick": XSS_TICK,
    }
)
PROSE_BODY = (
    "Status: I finished the review.\n\nThe **summary** is below.\n\n- one\n- two\n\n"
    "<img src=y onerror=alert(3)>"
)
KV_BODY = "task: index rebuild\nstatus: done\njobs: 13"


def _peer_text() -> str:
    return (
        f"{LEADING}\n"
        f'<teammate-message teammate_id="worker-a" color="blue">\n{JSON_BODY}\n'
        "</teammate-message>\n"
        f'<teammate-message teammate_id="worker-b" color="green" summary="done">\n'
        f"{PROSE_BODY}\n</teammate-message>\n"
        f'<teammate-message teammate_id="worker-c">\n{KV_BODY}\n</teammate-message>\n\n'
        f"{TRAILING}"
    )


def _entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.34",
    }
    base.update(fields)
    return base


def _write(tmp_path: Path, peer_text: str | None = None) -> Path:
    entries = [
        _entry(
            type="user",
            timestamp="2026-04-19T10:00:00.000Z",
            parentUuid=None,
            uuid="u1",
            message={"role": "user", "content": "start"},
        ),
        _entry(
            type="user",
            timestamp="2026-04-19T10:01:00.000Z",
            parentUuid="u1",
            uuid="u2",
            message={"role": "user", "content": peer_text or _peer_text()},
        ),
    ]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _blocks(html: str) -> list[str]:
    """The three teammate cards' bodies, in order, each cut at the next
    card's header or at the trailing framing text."""
    parts = html.split('<div class="teammate-body">')[1:]
    assert len(parts) == 3, f"expected 3 teammate bodies, got {len(parts)}"
    return [
        re.split(r"teammate-message-header|teammate-surrounding-text", p)[0]
        for p in parts
    ]


@pytest.fixture(params=["classic", "minimal"])
def html(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    return generate_html(load_transcript(_write(tmp_path)), "peer", theme=request.param)


class TestHtml:
    def test_json_body_uses_params_renderer(self, html: str) -> None:
        body = _blocks(html)[0]
        assert "tool-params-table" in body.split("teammate-message-header")[0]
        assert "teammate-json" not in html
        # Expand-all root: the long Markdown value folds.
        assert "tool-params-expand-all" in body.split('<div class="teammate-body">')[0]
        # The long `result` is Markdown, not a monospace scalar.
        result = body[body.index(">result<") :]
        assert "tool-param-markdown" in result
        assert "<strong>all twelve jobs</strong>" in result
        assert "<table>" in result
        assert "<li>first follow-up item</li>" in result

    def test_prose_body_stays_markdown(self, html: str) -> None:
        body = _blocks(html)[1]
        assert "tool-params-table" not in body
        assert "<strong>summary</strong>" in body
        assert "<li>one</li>" in body

    def test_key_value_body_uses_params_renderer(self, html: str) -> None:
        body = _blocks(html)[2]
        assert "tool-params-table" in body
        for key in ("task", "status", "jobs"):
            assert f"</span>{key}</td>" in body
        assert "index rebuild" in body

    def test_peer_values_are_escaped(self, html: str) -> None:
        assert "<img src=x" not in html
        assert "<script>alert('peer')" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "&lt;script&gt;" in html

    def test_framing_is_italic_text(self, html: str) -> None:
        # Still plain text in the DOM (search and the timeline read it).
        assert f'<div class="teammate-surrounding-text">{LEADING}</div>' in html
        assert "This came from another Claude session" in html
        rule = re.search(r"\.teammate-surrounding-text \{([^}]*)\}", html)
        assert rule is not None
        assert "font-style: italic" in rule.group(1)


class TestMarkdown:
    @pytest.fixture
    def md(self, tmp_path: Path) -> str:
        return MarkdownRenderer().generate(load_transcript(_write(tmp_path)), "peer")

    def test_json_body_as_key_value_list(self, md: str) -> None:
        assert "> **type:** `idle_notification`" in md
        assert "> **idleReason:** `available`" in md
        assert '{"type": "idle_notification"' not in md

    def test_key_value_body_as_key_value_list(self, md: str) -> None:
        assert "> **status:** `done`" in md

    def test_prose_body_unchanged(self, md: str) -> None:
        assert "> The **summary** is below." in md

    def test_peer_values_are_escaped(self, md: str) -> None:
        # No live tag outside code, in data or prose bodies: the only raw
        # ``<img`` left is the short value, inside its inline code span.
        assert "> **note:** `<img src=x onerror=alert(1)>`" in md
        assert md.count("<img") == 1
        assert "&lt;img src=x onerror=alert(2)&gt;" in md
        assert "&lt;img src=y onerror=alert(3)&gt;" in md

    def test_framing_is_italic(self, md: str) -> None:
        assert f"*{LEADING}*" in md
        assert f"*{TRAILING}*" in md

    def test_deeply_nested_json_body_renders(self, tmp_path: Path) -> None:
        # Too deep for the JSON decoder: the body falls back to prose, and
        # both the Markdown and the HTML output still render.
        body = '{"a": ' + "[" * 100_000 + "]" * 100_000 + "}"
        text = (
            f'<teammate-message teammate_id="w">\n{body}\n</teammate-message>\n\nAfter.'
        )
        messages = load_transcript(_write(tmp_path, text))
        assert "After." in MarkdownRenderer().generate(messages, "peer")
        assert "After." in generate_html(messages, "peer")

    def test_framing_is_escaped(self, tmp_path: Path) -> None:
        # The framing comes from another session too: raw HTML before and
        # after the blocks must not reach the .md file live.
        text = (
            "<img src=a onerror=alert(4)> sent this:\n"
            '<teammate-message teammate_id="worker-a">\nhello\n'
            "</teammate-message>\n\n"
            "and then <img src=b onerror=alert(5)>"
        )
        md = MarkdownRenderer().generate(
            load_transcript(_write(tmp_path, text)), "peer"
        )
        assert "<img" not in md
        assert "&lt;img src=a onerror=alert(4)&gt;" in md
        assert "&lt;img src=b onerror=alert(5)&gt;" in md


class TestBodyParams:
    def _params(self, body: str) -> Any:
        return TeammateMessageBlock(teammate_id="x", body=body).body_params()

    def test_json_object(self) -> None:
        assert self._params('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_json_not_an_object(self) -> None:
        assert self._params('{"a": ' + "[" * 100_000 + "]" * 100_000 + "}") is None
        assert self._params("[1, 2]") is None
        assert self._params("{}") is None
        assert self._params("{not json}") is None

    def test_key_value_lines(self) -> None:
        assert self._params("a: 1\nb_c: two words\n\nd.e: x") == {
            "a": "1",
            "b_c": "two words",
            "d.e": "x",
        }

    def test_prose_is_not_data(self) -> None:
        # A single line, a prose line among labels, a repeated key, a URL.
        assert self._params("status: done") is None
        assert self._params(PROSE_BODY) is None
        assert self._params("a: 1\na: 2") is None
        assert self._params("see: here\nhttps://example.com/x") is None
        assert self._params("- a: 1\n- b: 2") is None

    def test_labelled_paragraphs_are_prose(self) -> None:
        # Paragraphs that open with a capitalised label read as data
        # line by line, but they are prose.
        body = (
            "Relay: the indexing job finished and the report is attached.\n\n"
            "Important: rerun it after the schema change lands."
        )
        assert self._params(body) is None
