"""Timeline item labels and tooltips show a message's readable content (#319).

They used to copy the message card's HTML, controls included: a user
message's tooltip showed the escaped source of its raw/md toggle button,
a JSON tool result's showed its "expand all" button and a summary-less
"▶ Details" stub, and both labels started with the button text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

SESSION = "sess-timeline-tooltip"
USER_TEXT = "**Please** read `notes.md` and\n\n- list the TODOs\n- fix them"
JSON_RESULT = json.dumps(
    {
        "id": 21,
        "subject": "Launch prompt",
        "body": "Read message 20 and carry it out. " * 6,
        "to": ["alpha", "beta"],
    }
)
LONG_TEXT = "\n".join(f"line {i}: plain tool output" for i in range(40))


def _entry(
    uuid: str, parent: Optional[str], ts: str, role: str, content: list[dict[str, Any]]
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": content}
    entry: dict[str, Any] = {
        "type": role,
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": SESSION,
        "version": "2.1.0",
        "uuid": uuid,
        "message": message,
    }
    if role == "assistant":
        entry["requestId"] = f"req-{uuid}"
        message.update(
            id=f"msg-{uuid}",
            type="message",
            model="claude-opus-4-7",
            stop_reason="tool_use",
            stop_sequence=None,
        )
    return entry


def _tool_use(
    uuid: str, parent: str, ts: str, tool_id: str, name: str
) -> dict[str, Any]:
    return _entry(
        uuid,
        parent,
        ts,
        "assistant",
        [{"type": "tool_use", "id": tool_id, "name": name, "input": {"id": 21}}],
    )


def _tool_result(
    uuid: str, parent: str, ts: str, tool_id: str, text: str
) -> dict[str, Any]:
    return _entry(
        uuid,
        parent,
        ts,
        "user",
        [{"type": "tool_result", "tool_use_id": tool_id, "content": text}],
    )


def _open_timeline(
    page: Page, tmp_path: Path, user_view: Optional[str], before_build: str = ""
) -> Page:
    entries = [
        _entry(
            "u1",
            None,
            "2026-10-05T10:00:00Z",
            "user",
            [{"type": "text", "text": USER_TEXT}],
        ),
        _tool_use("a1", "u1", "2026-10-05T10:00:05Z", "tu_json", "mcp__mail__read"),
        _tool_result("u2", "a1", "2026-10-05T10:00:06Z", "tu_json", JSON_RESULT),
        _tool_use("a2", "u2", "2026-10-05T10:00:10Z", "tu_text", "mcp__log__read"),
        _tool_result("u3", "a2", "2026-10-05T10:00:11Z", "tu_text", LONG_TEXT),
    ]
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    html = tmp_path / "t.html"
    html.write_text(generate_html(load_transcript(jsonl), "Tooltips"), encoding="utf-8")

    page.goto(f"file://{html}")
    # The persistent browser context shares localStorage across file://
    # pages, so another test's global raw/md preference would leak in.
    page.evaluate("() => localStorage.clear()")
    if user_view is not None:
        page.evaluate(
            "v => localStorage.setItem('claude-code-log:user-view', v)", user_view
        )
    page.reload()
    if before_build:
        page.evaluate(before_build)
    page.click("#toggleTimeline")
    # 30s timeout handles CDN cold loads (first load per xdist worker)
    page.wait_for_selector(".vis-item.vis-box", timeout=30000)
    return page


@pytest.fixture
def timeline_page(page: Page, tmp_path: Path) -> Page:
    return _open_timeline(page, tmp_path, user_view=None)


def _hover_tooltip(page: Page, selector: str) -> str:
    page.hover(selector)
    tooltip = page.locator(".vis-tooltip")
    tooltip.wait_for(state="visible", timeout=5000)
    return tooltip.inner_text()


def _label(page: Page, selector: str) -> str:
    return page.locator(selector).locator(".vis-item-content").inner_text()


USER_ITEM = ".vis-item.vis-box.timeline-item-user"
JSON_RESULT_ITEM = ".vis-item.vis-box.timeline-item-tool_result >> nth=0"
TEXT_RESULT_ITEM = ".vis-item.vis-box.timeline-item-tool_result >> nth=1"


@pytest.mark.browser
def test_user_message_tooltip_is_its_text(timeline_page: Page) -> None:
    tooltip = _hover_tooltip(timeline_page, USER_ITEM)
    assert tooltip == "Please read notes.md and\n• list the TODOs\n• fix them"
    assert "button" not in tooltip
    # The label is the same text, and does not start with the toggle's "raw".
    assert _label(timeline_page, USER_ITEM).startswith("Please read notes.md")


@pytest.mark.browser
def test_json_tool_result_tooltip_shows_its_content(timeline_page: Page) -> None:
    tooltip = _hover_tooltip(timeline_page, JSON_RESULT_ITEM)
    lines = tooltip.splitlines()
    assert lines[:2] == ["id  21", "subject  Launch prompt"]
    assert "Read message 20 and carry it out." in tooltip
    assert lines[-3:] == ["to", "  0  alpha", "  1  beta"]
    assert "Details" not in tooltip
    assert "expand all" not in tooltip
    assert _label(timeline_page, JSON_RESULT_ITEM).startswith(
        "id 21 subject Launch prompt"
    )


@pytest.mark.browser
def test_folded_text_result_tooltip_is_truncated_content(timeline_page: Page) -> None:
    tooltip = _hover_tooltip(timeline_page, TEXT_RESULT_ITEM)
    lines = tooltip.splitlines()
    assert lines[0] == "line 0: plain tool output"
    assert len(lines) == 25
    assert lines[-1] == "line 24: plain tool output …"


@pytest.mark.browser
def test_user_message_tooltip_follows_the_raw_view(page: Page, tmp_path: Path) -> None:
    """With the global raw view on, the tooltip shows the raw text — the
    view the reader sees — and still only once."""
    raw_page = _open_timeline(page, tmp_path, user_view="raw")
    tooltip = _hover_tooltip(raw_page, USER_ITEM)
    assert tooltip == USER_TEXT


# One <pre> text node of 200k lines (~4 MB), set in the DOM so the browser's
# parser doesn't split it: collection must stop at the tooltip's cap rather
# than read (and re-scan) the whole node — that took minutes before.
HUGE_PRE = """() => {
    const pre = document.querySelectorAll('.message.tool_result .details-content pre')[0];
    const lines = [];
    for (let i = 0; i < 200000; i++) lines.push('huge line ' + i + ': output');
    pre.textContent = lines.join('\\n');
}"""


@pytest.mark.browser
def test_huge_pre_text_node_stays_capped(page: Page, tmp_path: Path) -> None:
    huge_page = _open_timeline(page, tmp_path, user_view=None, before_build=HUGE_PRE)
    lines = _hover_tooltip(huge_page, TEXT_RESULT_ITEM).splitlines()
    assert lines[0] == "huge line 0: output"
    assert len(lines) == 25
    assert lines[-1] == "huge line 24: output …"
