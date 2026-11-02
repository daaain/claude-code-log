"""The recent sessions page (recent.html): what the renderer writes.

The page's behaviour is covered by test_recent_browser.py; this checks the
static output — a page that holds no data and loads recent-data.js, a data
file transcript text can't break out of, and the refresh hook it calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.html.renderer import generate_recent_html
from claude_code_log.recent import payload_json, recent_data_js

COMPONENTS = (
    Path(__file__).parent.parent
    / "claude_code_log"
    / "html"
    / "templates"
    / "components"
    / "recent"
)
NOW = 1793465000000
EVIL = "</script><img src=x onerror=alert(1)>"


def _payload(**session: Any) -> dict[str, Any]:
    base = {
        "id": "8b04d7c2-1111-4000-8000-000000000000",
        "short": "8b04d7c2",
        "project": "foldyard",
        "projectDir": "-home-u-foldyard",
        "worktree": None,
        "branch": "main",
        "title": "Pin the to-box 9p cache",
        "href": "-home-u-foldyard/session-8b04d7c2.html",
        "firstAt": NOW - 3600000,
        "lastAt": NOW - 240000,
        "state": "asked",
        "messages": 9,
        "turnCount": 1,
        "toolCount": 2,
        "turns": [[NOW - 3600000, NOW - 240000, "ask"]],
        "earlierTurns": 0,
        "tools": [NOW - 3000000, NOW - 2000000],
        "lastPrompt": {"at": NOW - 3600000, "text": "Find out why."},
        "last": {"at": NOW - 240000, "who": "claude", "text": "Shall I?"},
    }
    base.update(session)
    return {
        "v": 1,
        "generatedAt": NOW,
        "windowDays": 3,
        "cacheTtlMinutes": 60,
        "sessions": [base],
    }


@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_page_holds_no_data_and_loads_the_data_file(theme: str) -> None:
    """The page is a fixed shell: its rows come from recent-data.js, on open
    and on every refresh, so it is the same on every run and no transcript
    text is ever inlined into it."""
    html = generate_recent_html(theme)
    assert "<script src='recent-data.js'></script>" in html
    assert "recent-initial" not in html
    assert html == generate_recent_html(theme)


def test_message_text_cannot_break_out_of_the_data_file() -> None:
    payload = _payload(title=EVIL, last={"at": NOW, "who": "claude", "text": EVIL})
    data = recent_data_js(payload)
    assert "<" not in data
    # The JSON keeps the text, with "<" escaped, and parses back unchanged.
    assert "\\u003c/script>\\u003cimg" in data
    body = payload_json(payload)
    assert body in data
    assert json.loads(body)["sessions"][0]["title"] == EVIL


def test_page_hooks_up_the_refresh() -> None:
    html = generate_recent_html()
    assert "window.__cclRecent = function" in html
    assert "recent-data.js" in html
    # The data file calls the same hook.
    assert "window.__cclRecent(" in recent_data_js(_payload())


def test_page_carries_its_chrome() -> None:
    html = generate_recent_html("minimal")
    assert "<h1>Sessions, last update first</h1>" in html
    assert "href='index.html'" in html
    assert "data-mn-theme='dark'" in html  # the scheme toggle
    assert "claude-code-log:theme" in html  # the stored choice applied
    assert "From the transcripts only" in html
    assert "fonts.googleapis" not in html


def test_runtime_never_writes_markup() -> None:
    """Transcript text reaches the page through textContent only."""
    source = (COMPONENTS / "recent.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in source, sink
