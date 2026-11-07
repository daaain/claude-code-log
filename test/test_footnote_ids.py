"""Footnote ids are scoped per message.

wenmode gives every rendered Markdown body the same GitHub-style footnote
ids (``user-content-fn-1``, ``user-content-fnref-1``, ``footnote-label``).
A transcript page holds many bodies, so without a per-message scope the
second answer's ``[1]`` jumps to the first answer's note. The Markdown
output has the same collision in its ``[^1]`` labels.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import convert_jsonl_to, load_transcript
from claude_code_log.html.renderer import generate_html

NOTE = "Claim[^1].\n\n[^1]: Note of answer {}.\n"

ID_RE = re.compile(r'id="([^"]*(?:fn|footnote)[^"]*)"')
HREF_RE = re.compile(r'href="#([^"]*(?:fn|footnote)[^"]*)"')
DESCRIBED_RE = re.compile(r'aria-describedby="([^"]*)"')


def _entry(uuid: str, parent: str | None, role: str, text: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": role,
        "content": [{"type": "text", "text": text}],
    }
    if role == "assistant":
        message.update(id=f"m-{uuid}", type="message", model="claude")
    return {
        "type": role,
        "timestamp": f"2026-01-01T10:00:0{len(uuid)}.000Z",
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s",
        "version": "1.0.0",
        "uuid": uuid,
        "message": message,
    }


@pytest.fixture
def two_answers(tmp_path: Path) -> Path:
    """Two answers, each defining its own ``[^1]``."""
    entries = [
        _entry("u1", None, "user", "One."),
        _entry("a12", "u1", "assistant", NOTE.format("ONE")),
        _entry("u123", "a12", "user", "Two."),
        _entry("a1234", "u123", "assistant", NOTE.format("TWO")),
    ]
    src = tmp_path / "notes.jsonl"
    src.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return src


def _check_footnote_ids(html: str) -> None:
    ids = ID_RE.findall(html)
    assert len(ids) == 6, ids  # 2 × (label heading, ref, note)
    assert len(set(ids)) == len(ids), sorted(ids)
    for target in HREF_RE.findall(html):
        assert ids.count(target) == 1, (target, ids)
    for described in DESCRIBED_RE.findall(html):
        assert ids.count(described) == 1, (described, ids)


def test_html_page_footnote_ids_are_unique(two_answers: Path) -> None:
    html = generate_html(load_transcript(two_answers, silent=True), "Notes")
    _check_footnote_ids(html)


@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_converted_project_footnote_ids_are_unique(
    two_answers: Path, tmp_path: Path, theme: str
) -> None:
    """The conversion path, where a fragment store formats each message."""
    out = convert_jsonl_to(
        "html", two_answers, tmp_path / "notes.html", theme=theme, use_cache=False
    )
    _check_footnote_ids(Path(out).read_text(encoding="utf-8"))


def test_markdown_output_footnote_labels_are_distinct(
    two_answers: Path, tmp_path: Path
) -> None:
    out = convert_jsonl_to(
        "markdown", two_answers, tmp_path / "notes.md", use_cache=False
    )
    text = Path(out).read_text(encoding="utf-8")
    definitions = re.findall(r"^\W*\[\^([^\]]+)\]:", text, flags=re.MULTILINE)
    assert len(definitions) == 2 and len(set(definitions)) == 2, definitions
    # Each answer's reference uses its own label.
    for label in definitions:
        assert text.count(f"[^{label}]") >= 2, (label, text)


def test_a_preview_copy_does_not_repeat_the_full_copys_ids(tmp_path: Path) -> None:
    """A long body folds into a preview plus the full text, both rendered;
    a footnote complete within the preview lines must not give the card
    two elements with one id."""
    text = NOTE.format("LONG") + "\n".join(["More."] * 30)
    src = tmp_path / "long.jsonl"
    src.write_text(json.dumps(_entry("a1", None, "assistant", text)) + "\n")
    html = generate_html(load_transcript(src, silent=True), "Long")
    ids = ID_RE.findall(html)
    assert len(ids) == len(set(ids)), sorted(ids)
    assert any("preview" in i for i in ids) and any("preview" not in i for i in ids)
    for target in HREF_RE.findall(html):
        assert ids.count(target) == 1, (target, ids)


# Text that only MENTIONS the ids and the `[^x]` syntax: in code, where a
# reader expects it verbatim. Scoping must not touch any of it.
MENTION = (
    "The anchor is `user-content-fn-1` and the back link `user-content-fnref-1`.\n\n"
    "```css\n#user-content-fn-1:target { color: red }\n```\n"
)
REGEX_WITH_NOTE = (
    "Use a negated class[^1]:\n\n"
    "```python\nre.compile(r'[^abc]+')\n```\n\n"
    "and inline `[^xyz]` too.\n\n[^1]: A note.\n"
)
DEFINITION_IN_CODE = "A sample:\n\n```md\nText[^1].\n\n[^1]: shown, not defined.\n```\n"


def _one_answer(tmp_path: Path, name: str, text: str) -> Path:
    src = tmp_path / f"{name}.jsonl"
    entries = [
        _entry("u1", None, "user", "Go."),
        _entry("a12", "u1", "assistant", text),
    ]
    src.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return src


def test_mentioned_ids_in_code_are_left_verbatim(tmp_path: Path) -> None:
    html = generate_html(
        load_transcript(_one_answer(tmp_path, "m", MENTION), silent=True), "M"
    )
    text = re.sub(r"<[^>]+>", "", html)
    assert "user-content-fn-1" in text and "user-content-fnref-1" in text
    assert "user-content-fn-m" not in html and "user-content-fnref-m" not in html


def test_markdown_scoping_leaves_code_and_undefined_labels_alone(
    tmp_path: Path,
) -> None:
    out = convert_jsonl_to(
        "markdown",
        _one_answer(tmp_path, "r", REGEX_WITH_NOTE),
        tmp_path / "r.md",
        use_cache=False,
    )
    md = Path(out).read_text(encoding="utf-8")
    assert "re.compile(r'[^abc]+')" in md and "`[^xyz]`" in md, md
    labels = sorted(set(re.findall(r"\[\^([^\]\s]+)\]", md)))
    assert len(labels) == 3 and "abc" in labels and "xyz" in labels, labels
    (scoped,) = [label for label in labels if label not in ("abc", "xyz")]
    assert scoped.startswith("m") and scoped.endswith("-1"), scoped
    # The reference, the definition and the heading excerpt all use it.
    assert md.count(f"[^{scoped}]") == 3, md


def test_a_definition_shown_in_code_does_not_turn_scoping_on(tmp_path: Path) -> None:
    out = convert_jsonl_to(
        "markdown",
        _one_answer(tmp_path, "d", DEFINITION_IN_CODE),
        tmp_path / "d.md",
        use_cache=False,
    )
    md = Path(out).read_text(encoding="utf-8")
    assert "Text[^1]." in md and "[^1]: shown, not defined." in md, md
    assert "[^m" not in md, md


def test_two_text_items_in_one_message_get_distinct_ids(tmp_path: Path) -> None:
    entry = _entry("a12", "u1", "assistant", "A[^1].\n\n[^1]: first.\n")
    entry["message"]["content"].append(
        {"type": "text", "text": "B[^1].\n\n[^1]: second.\n"}
    )
    src = tmp_path / "two-items.jsonl"
    src.write_text(
        "\n".join(json.dumps(e) for e in [_entry("u1", None, "user", "Two?"), entry])
        + "\n"
    )
    html = generate_html(load_transcript(src, silent=True), "Two")
    _check_footnote_ids(html)
    out = convert_jsonl_to("markdown", src, tmp_path / "two.md", use_cache=False)
    definitions = re.findall(
        r"^\W*\[\^([^\]]+)\]:", Path(out).read_text(encoding="utf-8"), re.M
    )
    assert len(definitions) == 2 and len(set(definitions)) == 2, definitions


# Fences as CommonMark reads them: a closer at least as long as the opener,
# up to three spaces of indentation, and an unclosed fence running to the
# end. A definition shown in any of them is code, not a footnote.
FENCES_SHOWING_A_DEFINITION = {
    "longer-closer": "A sample:\n\n```md\nText[^1].\n\n[^1]: shown.\n`````\n\nAfter.\n",
    "indented": "A sample:\n\n  ```md\n  Text[^1].\n\n  [^1]: shown.\n  ```\n",
    "unclosed": "A sample:\n\n```md\nText[^1].\n\n[^1]: shown.\n",
    "tildes-longer-closer": "A sample:\n\n~~~\nText[^1].\n\n[^1]: shown.\n~~~~\n",
}


@pytest.mark.parametrize("name", sorted(FENCES_SHOWING_A_DEFINITION))
def test_a_definition_in_any_fence_shape_turns_no_scoping_on(
    tmp_path: Path, name: str
) -> None:
    out = convert_jsonl_to(
        "markdown",
        _one_answer(tmp_path, name, FENCES_SHOWING_A_DEFINITION[name]),
        tmp_path / f"{name}.md",
        use_cache=False,
    )
    md = Path(out).read_text(encoding="utf-8")
    assert "Text[^1]." in md and "[^1]: shown." in md, md
    assert "[^m" not in md, md


def test_a_longer_closer_keeps_the_code_as_written_beside_a_real_note(
    tmp_path: Path,
) -> None:
    text = "Claim[^1].\n\n```\nnot a ref: [^1]\n`````\n\n[^1]: A note.\n"
    out = convert_jsonl_to(
        "markdown",
        _one_answer(tmp_path, "mixed", text),
        tmp_path / "mixed.md",
        use_cache=False,
    )
    md = Path(out).read_text(encoding="utf-8")
    assert "not a ref: [^1]" in md, md
    scoped = sorted(set(re.findall(r"\[\^(m[^\]]+)\]", md)))
    assert len(scoped) == 1, scoped
    assert md.count(f"[^{scoped[0]}]") == 3, md  # reference, definition, excerpt


def test_a_thinking_blocks_heading_uses_its_scoped_label(tmp_path: Path) -> None:
    entries = [
        _entry("u1", None, "user", "Go."),
        {
            **_entry("a12", "u1", "assistant", ""),
            "message": {
                "role": "assistant",
                "id": "m-a12",
                "type": "message",
                "model": "claude",
                "content": [
                    {"type": "thinking", "thinking": "Claim[^1].\n\n[^1]: A note.\n"}
                ],
            },
        },
    ]
    src = tmp_path / "thinking.jsonl"
    src.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    out = convert_jsonl_to("markdown", src, tmp_path / "thinking.md", use_cache=False)
    md = Path(out).read_text(encoding="utf-8")
    labels = set(re.findall(r"\[\^([^\]\s]+)\]", md))
    assert len(labels) == 1 and next(iter(labels)).startswith("m"), (labels, md)
