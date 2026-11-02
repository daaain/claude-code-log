"""Tests for the recent-sessions data (``claude_code_log/recent.py``).

The page's states come from the transcripts alone, so these pin how a
session's entries become turns, a last message and whose move it is —
and that the conversion writes the page and its self-refresh data file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import load_transcript, process_projects_hierarchy
from claude_code_log.recent import (
    RECENT_DATA,
    RECENT_PAGE,
    TURN_HORIZON_HOURS,
    asks_you,
    build_recent_payload,
    payload_json,
    recent_data_js,
    summarise_session,
    write_recent_data,
)

NOW = datetime(2026, 10, 31, 16, 42, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Session:
    """Writes a session's JSONL lines, one helper per kind of entry."""

    def __init__(self, session_id: str = "sess-1", cwd: str = "/home/u/proj") -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.lines: list[dict[str, Any]] = []
        self._n = 0

    def _base(self, kind: str, minutes_ago: float, **extra: Any) -> dict[str, Any]:
        self._n += 1
        entry: dict[str, Any] = {
            "type": kind,
            "timestamp": _iso(minutes_ago),
            "parentUuid": self.lines[-1]["uuid"] if self.lines else None,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.0.0",
            "uuid": f"{self.session_id}-{self._n}",
            "gitBranch": "feature-x",
        }
        entry.update(extra)
        self.lines.append(entry)
        return entry

    def prompt(self, minutes_ago: float, text: str, **extra: Any) -> None:
        self._base(
            "user",
            minutes_ago,
            message={"role": "user", "content": [{"type": "text", "text": text}]},
            **extra,
        )

    def reply(
        self, minutes_ago: float, text: str, model: str = "claude-opus", **extra: Any
    ) -> None:
        self._base(
            "assistant",
            minutes_ago,
            requestId=f"req-{self._n}",
            message={
                "id": f"m-{self._n}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
            },
            **extra,
        )

    def tool(self, minutes_ago: float, name: str = "Bash", **inputs: Any) -> None:
        tool_id = f"tool-{self._n}"
        self._base(
            "assistant",
            minutes_ago,
            requestId=f"req-{self._n}",
            message={
                "id": f"m-{self._n}",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus",
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": inputs}
                ],
            },
        )
        self._base(
            "user",
            minutes_ago - 0.1,
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
                ],
            },
        )

    def write(self, path: Path) -> Path:
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in self.lines), encoding="utf-8"
        )
        return path

    def summary(self, tmp_path: Path) -> dict[str, Any]:
        entries = load_transcript(
            self.write(tmp_path / f"{self.session_id}.jsonl"), silent=True
        )
        return summarise_session(entries, now_ms=NOW_MS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Shall I add it to shard L2?", True),
        ("Which one should I carry through?**", True),
        ("Done. Want me to push?\n\n", True),
        ("Is this right? I went with option 2.", False),
        ("Pushed to the branch.", False),
        ("", False),
    ],
)
def test_a_reply_asks_when_its_last_line_ends_on_a_question(
    text: str, expected: bool
) -> None:
    assert asks_you(text) is expected


def test_a_reply_that_asks_is_your_move(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Fix the stale cache.")
    s.tool(28, command="just test")
    s.reply(25, "Fixed. Shall I add the test to shard L2?")
    info = s.summary(tmp_path)
    assert info["state"] == "asked"
    assert [t[2] for t in info["turns"]] == ["ask"]
    assert info["last"]["who"] == "claude"
    assert info["last"]["text"].endswith("shard L2?")
    assert info["lastPrompt"]["text"] == "Fix the stale cache."
    assert info["toolCount"] == 1 and len(info["tools"]) == 1
    assert info["branch"] == "feature-x"


def test_a_finished_reply_is_done(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Push it.")
    s.reply(29, "Pushed to the branch.")
    assert s.summary(tmp_path)["state"] == "done"


def test_an_api_error_as_the_last_entry_is_an_error(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Carry on.")
    s.reply(29, "API Error: Request timed out.", model="<synthetic>")
    info = s.summary(tmp_path)
    assert info["state"] == "error"
    assert info["turns"][-1][2] == "err"
    assert info["last"] == {
        "at": info["last"]["at"],
        "who": "error",
        "text": "API Error: Request timed out.",
    }


def test_a_session_ending_on_a_tool_call_is_mid_turn(tmp_path: Path) -> None:
    """The transcript can't tell a running agent from one that stopped
    mid-turn: both are the agent's move, shown by the tool call."""
    s = _Session()
    s.prompt(30, "Run the race test.")
    s.tool(2, command="just test -k race --count 20")
    info = s.summary(tmp_path)
    assert info["state"] == "busy"
    assert info["turns"][-1][2] == "open"
    assert info["last"]["who"] == "tool"
    assert info["last"]["text"] == "Bash · just test -k race --count 20"


def test_a_prompt_with_no_reply_yet_is_the_agents_move(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "First.")
    s.reply(29, "Done.")
    s.prompt(1, "Now the second part.")
    info = s.summary(tmp_path)
    assert info["state"] == "busy"
    assert info["last"] is None
    assert info["lastPrompt"]["text"] == "Now the second part."


def test_only_the_final_turn_can_be_open(tmp_path: Path) -> None:
    """An earlier turn that ended on a tool call was interrupted: it is
    over, not under way."""
    s = _Session()
    s.prompt(60, "Start.")
    s.tool(59, command="sleep 100")
    s.prompt(40, "Never mind, do this instead.")
    s.reply(39, "Done.")
    info = s.summary(tmp_path)
    assert [t[2] for t in info["turns"]] == ["asst", "asst"]
    assert info["turnCount"] == 2


def test_injected_user_bodies_are_not_prompts(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Real prompt.")
    s.reply(29, "Started a background agent.")
    s.prompt(
        20,
        "<task-notification>\n<task-id>a1</task-id>\n"
        "<status>completed</status>\n</task-notification>",
    )
    s.prompt(19, "<local-command-stdout>ok</local-command-stdout>")
    s.prompt(18, "Meta body", isMeta=True)
    info = s.summary(tmp_path)
    assert info["turnCount"] == 1
    assert info["lastPrompt"]["text"] == "Real prompt."


def test_sidechain_entries_are_left_out(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Delegate it.")
    s.reply(29, "Delegated.")
    s.prompt(20, "sub-agent prompt", isSidechain=True)
    s.reply(19, "sub-agent reply?", isSidechain=True)
    info = s.summary(tmp_path)
    assert info["turnCount"] == 1
    assert info["state"] == "done"


def test_turns_older_than_the_horizon_are_counted_not_listed(tmp_path: Path) -> None:
    old = (TURN_HORIZON_HOURS + 2) * 60
    s = _Session()
    s.prompt(old, "Yesterday's work.")
    s.reply(old - 5, "Done.")
    s.prompt(10, "Today's work.")
    s.reply(9, "Done.")
    info = s.summary(tmp_path)
    assert info["turnCount"] == 2
    assert len(info["turns"]) == 1
    assert info["earlierTurns"] == 1


def test_payload_json_cannot_end_a_script_element() -> None:
    payload = {"sessions": [{"title": "</script><img src=x onerror=alert(1)>"}]}
    text = payload_json(payload)
    assert "<" not in text
    assert json.loads(text) == payload
    assert recent_data_js(payload).startswith("/*")


def _project(projects: Path, name: str, session: _Session) -> Path:
    project = projects / name
    project.mkdir(parents=True)
    session.write(project / f"{session.session_id}.jsonl")
    return project


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    recent = _Session("aaaa1111-recent", cwd="/home/u/code/foldyard")
    recent.prompt(30, "Pin the cache mode.")
    recent.reply(25, "Pinned. Shall I add the test?")
    _project(projects, "-home-u-code-foldyard", recent)
    old = _Session("bbbb2222-old", cwd="/home/u/code/other")
    old.prompt(60 * 24 * 10, "Ancient history.")
    old.reply(60 * 24 * 10 - 1, "Done.")
    _project(projects, "-home-u-code-other", old)
    return projects


def test_payload_lists_recent_sessions_across_projects(archive: Path) -> None:
    process_projects_hierarchy(archive, silent=True)
    payload = build_recent_payload(archive, now=NOW)
    assert [s["id"] for s in payload["sessions"]] == ["aaaa1111-recent"]
    session = payload["sessions"][0]
    assert session["state"] == "asked"
    assert session["project"] == "foldyard"
    assert session["short"] == "aaaa1111"
    assert session["href"] == "-home-u-code-foldyard/session-aaaa1111-recent.html"
    assert payload["generatedAt"] == NOW_MS
    assert payload["v"] == 1


def _data_payload(path: Path) -> dict[str, Any]:
    match = re.search(r"__cclRecent\((\{.*\})\);", path.read_text(encoding="utf-8"))
    assert match, "recent-data.js should hand the page its payload"
    return json.loads(match.group(1))


def test_conversion_writes_the_page_and_its_data(archive: Path) -> None:
    process_projects_hierarchy(archive, silent=True)
    assert (archive / RECENT_PAGE).exists()
    payload = _data_payload(archive / RECENT_DATA)
    # The real clock: the fixture's session is 25 minutes old only
    # relative to NOW, so it may fall outside the window — but the file
    # must always be a well-formed payload.
    assert payload["v"] == 1
    assert isinstance(payload["sessions"], list)
    index = (archive / "index.html").read_text(encoding="utf-8")
    assert "href='recent.html'" in index


def test_index_links_to_the_page_in_both_themes(archive: Path) -> None:
    for theme in ("classic", "minimal"):
        process_projects_hierarchy(archive, silent=True, theme=theme)
        index = (archive / "index.html").read_text(encoding="utf-8")
        assert index.count("href='recent.html'") == 1, theme


def test_write_recent_data_needs_the_page(tmp_path: Path) -> None:
    """Nothing to feed without the page (a single-file conversion)."""
    assert write_recent_data(tmp_path, tmp_path, now=NOW) is None
    assert not (tmp_path / RECENT_DATA).exists()


def test_write_recent_data_refreshes_the_data_file(archive: Path) -> None:
    process_projects_hierarchy(archive, silent=True)
    payload = write_recent_data(archive, archive, now=NOW)
    assert payload is not None
    assert [s["id"] for s in _data_payload(archive / RECENT_DATA)["sessions"]] == [
        "aaaa1111-recent"
    ]
