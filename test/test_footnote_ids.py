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
