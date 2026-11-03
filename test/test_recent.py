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
from typing import Any, Optional

import pytest

from claude_code_log.converter import load_transcript, process_projects_hierarchy
from claude_code_log.cache import CacheManager
from claude_code_log.recent import (
    RECENT_DATA,
    RECENT_PAGE,
    BG_CAP_MINUTES,
    SNIPPET_CHARS,
    WAKE_GRACE_MINUTES,
    TURN_HORIZON_HOURS,
    WINDOW_DAYS,
    RecentMemo,
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

    def tool(
        self,
        minutes_ago: float,
        name: str = "Bash",
        result: bool = True,
        result_text: str = "ok",
        tool_use_result: Any = None,
        sidechain: bool = False,
        **inputs: Any,
    ) -> str:
        """A tool call and (unless ``result`` is False) its result; the
        call's id, for a notification's ``tool-use-id``."""
        tool_id = f"tool-{self._n}"
        side: dict[str, Any] = {"isSidechain": True} if sidechain else {}
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
            **side,
        )
        if not result:
            return tool_id
        extra: dict[str, Any] = dict(side)
        if tool_use_result is not None:
            extra["toolUseResult"] = tool_use_result
        self._base(
            "user",
            minutes_ago - 0.1,
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_text,
                    }
                ],
            },
            **extra,
        )
        return tool_id

    def notify(
        self,
        minutes_ago: float,
        task_id: str,
        status: Optional[str] = None,
        summary: str = "",
        event: Optional[str] = None,
        result: Optional[str] = None,
        mid_turn: bool = False,
        preamble: bool = False,
        queued_only: bool = False,
    ) -> None:
        """A ``<task-notification>``: a user entry when it arrives while
        Claude is idle, a ``queued_command`` attachment mid-turn, or only
        a ``queue-operation`` (queued, never delivered)."""
        fields = f"<task-id>{task_id}</task-id>"
        if status:
            fields += f"\n<status>{status}</status>"
        if summary:
            fields += f"\n<summary>{summary}</summary>"
        if event:
            fields += f"\n<event>{event}</event>"
        if result:
            fields += f"\n<result>{result}</result>"
        body = f"<task-notification>\n{fields}\n</task-notification>"
        if preamble:
            body = "[SYSTEM NOTIFICATION - NOT USER INPUT]\n" + body
        if queued_only:
            self._base(
                "queue-operation", minutes_ago, operation="enqueue", content=body
            )
        elif mid_turn:
            self._base(
                "attachment",
                minutes_ago,
                attachment={
                    "type": "queued_command",
                    "commandMode": "task-notification",
                    "prompt": body,
                },
            )
        else:
            self._base("user", minutes_ago, message={"role": "user", "content": body})

    def wake_fire(
        self, minutes_ago: float, content: str = "Claude resuming /loop wakeup"
    ) -> None:
        """A wake-up firing: the system entry, then its meta prompt."""
        self._base(
            "system", minutes_ago, content=content, subtype="scheduled_task_fire"
        )
        self._base(
            "user",
            minutes_ago,
            isMeta=True,
            message={"role": "user", "content": "check on the CI run"},
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
    assert info["last"]["html"].endswith("shard L2?</p>\n")
    assert info["lastPrompt"]["html"] == "<p>Fix the stale cache.</p>\n"
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
    assert info["lastPrompt"]["html"] == "<p>Now the second part.</p>\n"


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
    # None is a prompt; the notification (delivered while Claude was
    # idle) starts a turn of its own, the rest just continue it.
    assert [t[5] for t in info["turns"]] == ["you", "task"]
    assert info["lastPrompt"]["html"] == "<p>Real prompt.</p>\n"


def _compact(s: _Session, minutes_ago: float) -> None:
    """A compaction as Claude Code writes it: the boundary, then the
    summary as a user entry, then context attachments."""
    s._base(
        "system",
        minutes_ago,
        content="Conversation compacted",
        subtype="compact_boundary",
        isMeta=False,
    )
    s.prompt(
        minutes_ago,
        "This session is being continued from a previous conversation that ran"
        " out of context. The summary below covers the earlier portion of the"
        " conversation.\n\nSummary: ...",
        isCompactSummary=True,
        isVisibleInTranscriptOnly=True,
    )
    s._base("attachment", minutes_ago, attachment={"type": "environment"})


def test_an_idle_compaction_is_not_a_turn(tmp_path: Path) -> None:
    """Claude Code can compact a session while it waits for you: the
    summary it writes is not a prompt, and the session stays finished."""
    s = _Session()
    s.prompt(120, "Commit the work.")
    s.reply(110, "Committed.")
    _compact(s, 55)
    info = s.summary(tmp_path)
    assert info["state"] == "done"
    assert info["turnCount"] == 1
    assert info["turns"][0][1] == _ms_ago(110)  # not stretched to the compaction
    assert info["lastPrompt"]["html"] == "<p>Commit the work.</p>\n"
    assert info["last"]["html"] == "<p>Committed.</p>\n"


def test_a_mid_turn_compaction_continues_the_turn(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(60, "Refactor the parser.")
    s.tool(50, command="just test")
    _compact(s, 45)
    s.tool(40, command="just test")
    s.reply(35, "Refactored; the tests pass.")
    info = s.summary(tmp_path)
    assert info["state"] == "done"
    assert info["turnCount"] == 1
    assert info["toolCount"] == 2
    assert info["turns"][0][:2] == [_ms_ago(60), _ms_ago(35)]


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


def test_the_last_prompt_and_reply_carry_rendered_markdown(tmp_path: Path) -> None:
    """Rendered by the session pages' pipeline: Markdown becomes markup,
    raw HTML in the transcript stays text."""
    s = _Session()
    s.prompt(30, "Fix **this** <img src=x onerror=alert(1)>")
    s.reply(29, "Done:\n\n- `just test` passes\n\n<script>alert(2)</script>")
    info = s.summary(tmp_path)
    prompt_html = info["lastPrompt"]["html"]
    assert "<strong>this</strong>" in prompt_html
    assert "<img" not in prompt_html and "&lt;img" in prompt_html
    last_html = info["last"]["html"]
    assert "<li><code>just test</code> passes</li>" in last_html
    assert "<script>" not in last_html and "&lt;script&gt;" in last_html
    # Lean: the rendered form replaces the text, it doesn't sit beside it.
    assert "text" not in info["last"] and "text" not in info["lastPrompt"]


def test_tool_and_error_lines_are_not_markdown(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Run it.")
    s.tool(2, command="echo **not bold**")
    last = s.summary(tmp_path)["last"]
    assert "html" not in last
    assert last["text"] == "Bash · echo **not bold**"


def test_each_turn_carries_snippets_of_its_prompt_and_end(tmp_path: Path) -> None:
    long_reply = "word " * 200
    s = _Session()
    s.prompt(30, "First\n\n   prompt.")
    s.reply(29, "First reply.")
    s.prompt(20, "Second prompt.")
    s.tool(19, command="just test")
    s.reply(18, long_reply)
    turns = s.summary(tmp_path)["turns"]
    assert turns[0][3:] == ["First prompt.", "First reply.", "you"]
    assert turns[1][3] == "Second prompt."
    assert len(turns[1][4]) <= SNIPPET_CHARS and turns[1][4].endswith("…")


def test_an_implicit_turn_has_no_prompt_snippet(tmp_path: Path) -> None:
    s = _Session()
    s.reply(10, "Resumed and carried on.")
    assert s.summary(tmp_path)["turns"][0][3] is None


ASK_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Which renderer should we use?",
            "header": "Renderer",
            "multiSelect": False,
            "options": [
                {"label": "Server-side", "description": "The wenmode pipeline."},
                {"label": "Client-side", "description": ""},
            ],
        },
        {
            "question": "Where does the chevron go?",
            "header": "Chevron",
            "multiSelect": False,
            "options": [{"label": "Left", "description": "Its own column."}],
        },
    ]
}


def test_an_unanswered_question_tool_asks_you(tmp_path: Path) -> None:
    """Claude waits on AskUserQuestion until you answer: your move, with
    the questions and their options as the last message."""
    s = _Session()
    s.prompt(30, "Plan the change.")
    s.tool(25, "AskUserQuestion", result=False, **ASK_INPUT)
    info = s.summary(tmp_path)
    assert info["state"] == "asked"
    assert info["turns"][-1][2] == "ask"
    assert info["turns"][-1][4].startswith("Which renderer should we use?")
    last = info["last"]
    assert last["who"] == "question"
    assert last["html"].startswith("<p>Which renderer should we use?</p>")
    assert "Where does the chevron go?" in last["html"]
    assert "<strong>Server-side</strong> — The wenmode pipeline." in last["html"]
    assert "<strong>Client-side</strong></li>" in last["html"]


def test_an_answered_question_tool_keeps_the_turn_going(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Plan the change.")
    s.tool(25, "AskUserQuestion", **ASK_INPUT)
    assert s.summary(tmp_path)["state"] == "busy"


def test_a_malformed_question_tool_is_an_ordinary_tool_call(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Plan the change.")
    s.tool(25, "AskUserQuestion", result=False, questions="oops")
    info = s.summary(tmp_path)
    assert info["state"] == "busy"
    assert info["last"] == {
        "at": info["last"]["at"],
        "who": "tool",
        "text": "AskUserQuestion",
    }


def test_a_plan_awaiting_approval_asks_you(tmp_path: Path) -> None:
    """ExitPlanMode waits on your approval: your move, the plan as the
    last message."""
    s = _Session()
    s.prompt(30, "Plan the refactor.")
    s.tool(20, "ExitPlanMode", result=False, plan="## Plan\n\n1. Split `recent.py`")
    info = s.summary(tmp_path)
    assert info["state"] == "asked"
    assert info["turns"][-1][2] == "ask"
    assert info["turns"][-1][4] == "## Plan 1. Split `recent.py`"
    last = info["last"]
    assert last["who"] == "plan"
    assert "<h2" in last["html"] and "<code>recent.py</code>" in last["html"]


def test_an_approved_plan_keeps_the_turn_going(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(30, "Plan the refactor.")
    s.tool(20, "ExitPlanMode", plan="## Plan")
    assert s.summary(tmp_path)["state"] == "busy"


def test_a_plan_without_its_text_still_asks_you(tmp_path: Path) -> None:
    """Some versions keep the plan in a file, not the tool input."""
    s = _Session()
    s.prompt(30, "Plan the refactor.")
    s.tool(20, "ExitPlanMode", result=False)
    info = s.summary(tmp_path)
    assert info["state"] == "asked"
    assert info["last"]["who"] == "plan"
    assert "approve" in info["last"]["html"]


# ---- background work ------------------------------------------------


def _ms_ago(minutes: float) -> int:
    return NOW_MS - int(minutes * 60000)


BG_RESULT = (
    "Command running in background with ID: b1. Output is being written to:"
    " /tmp/tasks/b1.output"
)


def test_background_command_pairs_with_its_notification(tmp_path: Path) -> None:
    """A notification delivered while Claude is idle ends the work and
    starts a turn of its own, labelled with what reported."""
    s = _Session()
    s.prompt(40, "Run the tests in the background.")
    s.tool(
        30,
        command="just test",
        description="Run the unit tests",
        run_in_background=True,
        result_text=BG_RESULT,
        tool_use_result={"stdout": "", "backgroundTaskId": "b1"},
    )
    s.reply(29, "Started; I'll report back.")
    summary = 'Background command "Run the unit tests" completed (exit code 0)'
    s.notify(20, "b1", "completed", summary)
    s.reply(19, "All tests pass.")
    info = s.summary(tmp_path)
    assert info["bg"] == [
        [_ms_ago(30), _ms_ago(20), "bash", "completed", "Run the unit tests", None]
    ]
    assert [t[5] for t in info["turns"]] == ["you", "task"]
    assert info["turns"][1][3] == summary
    assert info["turnCount"] == 2
    # Your last prompt is still the one you typed.
    assert "Run the tests" in info["lastPrompt"]["html"]
    assert info["state"] == "done"


def test_an_auto_backgrounded_command_is_found_by_its_text(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Run the slow suite.")
    s.tool(
        30,
        command="just test-all\necho done",
        result_text=(
            "Command did not complete within its 300s timeout and was moved"
            " to the background (ID: bx9). Output is being written to: /tmp/x"
        ),
    )
    s.reply(29, "It's still going.")
    assert s.summary(tmp_path)["bg"] == [
        [_ms_ago(30), None, "bash", None, "just test-all", None]
    ]


def test_an_async_agent_without_a_notification_has_no_end(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Survey the transcripts.")
    s.tool(
        30,
        "Agent",
        description="Survey transcripts",
        prompt="...",
        tool_use_result={"isAsync": True, "status": "async_launched", "agentId": "a1"},
    )
    s.reply(29, "The survey is running.")
    info = s.summary(tmp_path)
    assert info["bg"] == [
        [_ms_ago(30), None, "agent", None, "Survey transcripts", None]
    ]
    # The transcript state; the page decides it is "waiting".
    assert info["state"] == "done"


def test_an_agents_interim_notification_is_not_its_end(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Survey.")
    s.tool(
        30,
        "Agent",
        description="Survey",
        tool_use_result={"status": "async_launched", "agentId": "a1"},
    )
    s.reply(29, "Running.")
    s.notify(
        20,
        "a1",
        "completed",
        'Agent "Survey" is waiting on its own background work',
        mid_turn=True,
    )
    assert s.summary(tmp_path)["bg"][0][1:4] == [None, "agent", None]


def test_an_agents_hand_back_notification_is_its_end(tmp_path: Path) -> None:
    """An agent that waited on its own work reports by hand-back; the
    notification that follows still carries the interim summary, but
    its result says the report was delivered, so the agent is done."""
    s = _Session()
    s.prompt(40, "Survey.")
    s.tool(
        30,
        "Agent",
        description="Survey",
        tool_use_result={"status": "async_launched", "agentId": "a1"},
    )
    s.reply(29, "Running.")
    interim = 'Agent "Survey" is waiting on its own background work'
    s.notify(
        20,
        "a1",
        "completed",
        interim,
        result=(
            "This agent has not reported yet: it is waiting on its own"
            " background work and will deliver its report through"
            " SubagentHandback when that finishes."
        ),
        mid_turn=True,
    )
    s.notify(
        10,
        "a1",
        "completed",
        interim,
        result=(
            'This agent\'s report was delivered to you as a message from "a1"'
            " (its SubagentHandback call). Read it there; it is not repeated"
            " here."
        ),
        mid_turn=True,
    )
    assert s.summary(tmp_path)["bg"][0][1:4] == [_ms_ago(10), "agent", "completed"]


def test_a_notification_mid_turn_ends_work_without_a_new_turn(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Build and keep going.")
    s.tool(
        30,
        command="just build",
        tool_use_result={"backgroundTaskId": "b2"},
    )
    s.notify(25, "b2", "failed", "failed with exit code 2", mid_turn=True)
    s.reply(24, "The build failed; fixing.")
    info = s.summary(tmp_path)
    assert info["bg"][0][1:4] == [_ms_ago(25), "bash", "failed"]
    assert [t[5] for t in info["turns"]] == ["you"]


def test_a_notification_with_a_preamble_is_not_a_prompt(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Run it.")
    s.tool(30, command="just test", tool_use_result={"backgroundTaskId": "b3"})
    s.reply(29, "Started.")
    s.notify(20, "b3", "completed", "completed (exit code 0)", preamble=True)
    s.reply(19, "Done.")
    info = s.summary(tmp_path)
    assert "Run it." in info["lastPrompt"]["html"]
    assert [t[5] for t in info["turns"]] == ["you", "task"]
    assert info["bg"][0][3] == "completed"


def test_a_notification_only_queued_still_ends_work(tmp_path: Path) -> None:
    """The session closed before it was delivered: the work still ended."""
    s = _Session()
    s.prompt(40, "Run it.")
    s.tool(30, command="sleep 99", tool_use_result={"backgroundTaskId": "b7"})
    s.reply(29, "Started.")
    s.notify(
        20,
        "b7",
        "killed",
        'Background command "sleep 99" was stopped',
        queued_only=True,
    )
    info = s.summary(tmp_path)
    assert info["bg"][0][1:4] == [_ms_ago(20), "bash", "stopped"]
    assert [t[5] for t in info["turns"]] == ["you"]


def test_command_output_that_looks_like_a_launch_is_not_one(tmp_path: Path) -> None:
    """Only the harness's own launch message names an id: a foreground
    command printing similar words (a source file, a log) launched
    nothing."""
    s = _Session()
    s.prompt(40, "Show me the model.")
    s.tool(
        30, command="cat models.py", result_text="class X:\n    agentId: Optional[str]"
    )
    s.tool(
        29,
        command="grep -r background log.txt",
        result_text="log: Command running in background with ID: zz1 (old run)",
    )
    s.tool(
        28,
        "Agent",
        description="Sync agent",
        result_text="Here is the summary. agentId: abc (for reference)",
    )
    s.reply(27, "Here it is.")
    assert s.summary(tmp_path)["bg"] == []


def test_monitors_end_by_status_or_expiry_not_by_events(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(60, "Watch CI and the logs.")
    s.tool(
        50,
        "Monitor",
        command="gh run watch",
        description="CI run",
        tool_use_result={"taskId": "m1", "timeoutMs": 1800000, "persistent": False},
    )
    s.tool(
        49,
        "Monitor",
        command="tail -f log",
        description="Server log",
        result_text="Monitor started (task m2, timeout 600000ms). You will be notified",
    )
    s.tool(
        48,
        "Monitor",
        description="Forever",
        tool_use_result={"taskId": "m3", "persistent": True},
    )
    s.reply(47, "Watching.")
    s.notify(40, "m1", summary='Monitor event: "CI run"', event="step 3 passed")
    s.notify(
        35,
        "m2",
        summary='Monitor event: "Server log"',
        event="[Monitor timed out — re-arm if needed.]",
    )
    info = s.summary(tmp_path)
    by_label = {b[4]: b for b in info["bg"]}
    assert by_label["CI run"][1:4] == [None, "monitor", None]
    assert by_label["CI run"][5] == _ms_ago(50) + 1800000
    assert by_label["Server log"][1:4] == [_ms_ago(35), "monitor", "completed"]
    assert by_label["Forever"][5] is None


def test_task_stop_and_kill_shell_stop_work(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(60, "Run two things.")
    s.tool(50, command="sleep 1000", tool_use_result={"backgroundTaskId": "b4"})
    s.tool(49, command="sleep 2000", tool_use_result={"backgroundTaskId": "b5"})
    s.tool(40, "TaskStop", task_id="b4")
    s.tool(39, "KillShell", shell_id="b5")
    s.reply(38, "Stopped both.")
    assert [b[1:4] for b in s.summary(tmp_path)["bg"]] == [
        [_ms_ago(40), "bash", "stopped"],
        [_ms_ago(39), "bash", "stopped"],
    ]


def test_a_pending_wake_up_and_its_firing(tmp_path: Path) -> None:
    fire_at = _ms_ago(10)
    s = _Session()
    s.prompt(40, "Keep an eye on CI.")
    s.tool(
        30,
        "ScheduleWakeup",
        delaySeconds=1200,
        reason="check the CI run",
        prompt="check on the CI run",
        tool_use_result={"scheduledFor": fire_at, "clampedDelaySeconds": 1200},
    )
    s.reply(29, "I'll check back in 20 minutes.")
    pending = s.summary(tmp_path)
    assert pending["wakes"] == [[fire_at, "check the CI run"]]
    s.wake_fire(10)
    s.reply(9, "CI is green.")
    fired = s.summary(tmp_path)
    assert fired["wakes"] == []
    assert [t[5] for t in fired["turns"]] == ["you", "wake"]
    assert fired["turns"][1][3] == "check the CI run"


def test_a_wake_up_from_its_text_and_a_stop_cancelling_it(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Loop.")
    s.tool(
        30,
        "ScheduleWakeup",
        delaySeconds=600,
        reason="poll",
        result_text="Next wakeup scheduled for 16:22:00 (in 600s). Nothing more",
    )
    s.reply(29, "Waiting.")
    assert s.summary(tmp_path)["wakes"] == [[_ms_ago(30) + 600000, "poll"]]
    s.tool(
        25,
        "ScheduleWakeup",
        stop=True,
        tool_use_result={"stopped": True, "scheduledFor": 0},
    )
    s.reply(24, "Stopped the loop.")
    assert s.summary(tmp_path)["wakes"] == []


def test_a_sidechains_background_work_is_its_own(tmp_path: Path) -> None:
    s = _Session()
    s.prompt(40, "Delegate.")
    s.tool(30, command="x", tool_use_result={"backgroundTaskId": "b9"}, sidechain=True)
    s.reply(29, "Delegated.")
    assert s.summary(tmp_path)["bg"] == []


def test_background_work_reaches_the_payload_through_the_cache(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    s = _Session("cccc3333-bg", cwd="/home/u/code/foldyard")
    s.prompt(40, "Run it in the background.")
    s.tool(30, command="just test", tool_use_result={"backgroundTaskId": "b1"})
    s.reply(29, "Started.")
    s.notify(20, "b1", "completed", "completed (exit code 0)")
    s.reply(19, "Passed.")
    s.tool(
        18,
        "ScheduleWakeup",
        reason="recheck",
        tool_use_result={"scheduledFor": _ms_ago(5)},
    )
    s.reply(17, "Checking again later.")
    s.wake_fire(5)
    s.reply(4, "Still green.")
    _project(projects, "-home-u-code-foldyard", s)
    process_projects_hierarchy(projects, silent=True)
    payload = build_recent_payload(projects, now=NOW)
    assert payload["bgCapMinutes"] == BG_CAP_MINUTES
    assert payload["wakeGraceMinutes"] == WAKE_GRACE_MINUTES
    session = payload["sessions"][0]
    assert session["bg"][0][1:4] == [_ms_ago(20), "bash", "completed"]
    assert [t[5] for t in session["turns"]] == ["you", "task", "wake"]


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


# ---- reuse between watch ticks ------------------------------------------


@pytest.fixture
def two_sessions(tmp_path: Path) -> tuple[Path, _Session, _Session]:
    projects = tmp_path / "projects"
    first = _Session("aaaa1111-first", cwd="/home/u/code/foldyard")
    first.prompt(30, "First.")
    first.reply(29, "Done.")
    second = _Session("bbbb2222-second", cwd="/home/u/code/other")
    second.prompt(20, "Second.")
    second.reply(19, "Done.")
    _project(projects, "-home-u-code-foldyard", first)
    _project(projects, "-home-u-code-other", second)
    process_projects_hierarchy(projects, silent=True)
    return projects, first, second


def _count_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    loaded: list[str] = []
    original = CacheManager.load_session_entries

    def counting(self: CacheManager, session_id: str) -> list[Any]:
        loaded.append(session_id)
        return original(self, session_id)

    monkeypatch.setattr(CacheManager, "load_session_entries", counting)
    return loaded


def test_a_memo_reuses_unchanged_sessions(
    two_sessions: tuple[Path, _Session, _Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    projects, _first, second = two_sessions
    loaded = _count_loads(monkeypatch)
    memo = RecentMemo()
    cold = build_recent_payload(projects, now=NOW, memo=memo)
    assert sorted(loaded) == ["aaaa1111-first", "bbbb2222-second"]

    loaded.clear()
    warm = build_recent_payload(projects, now=NOW, memo=memo)
    assert loaded == []
    assert warm == cold

    # One session grows: only it is read again.
    second.prompt(5, "And another thing.")
    second.reply(4, "Done too.")
    second.write(projects / "-home-u-code-other" / f"{second.session_id}.jsonl")
    process_projects_hierarchy(projects, silent=True)
    loaded.clear()
    grown = build_recent_payload(projects, now=NOW, memo=memo)
    assert loaded == ["bbbb2222-second"]
    assert grown["sessions"][0]["turnCount"] == 2


def test_a_memoised_summary_still_follows_the_clock(
    two_sessions: tuple[Path, _Session, _Session],
) -> None:
    """The memo holds what the transcript says; the clock (the turn
    horizon) is applied on every build."""
    projects, _first, _second = two_sessions
    memo = RecentMemo()
    now = build_recent_payload(projects, now=NOW, memo=memo)
    # The horizon lands between the two sessions' last turns (29 and
    # 19 minutes before NOW).
    later = build_recent_payload(
        projects,
        now=NOW + timedelta(hours=TURN_HORIZON_HOURS) - timedelta(minutes=22),
        memo=memo,
    )
    by_id = {s["id"]: s for s in later["sessions"]}
    assert len(now["sessions"][1]["turns"]) == 1
    assert by_id["aaaa1111-first"]["turns"] == []
    assert by_id["aaaa1111-first"]["earlierTurns"] == 1
    assert len(by_id["bbbb2222-second"]["turns"]) == 1


def test_a_memo_forgets_sessions_that_leave_the_list(
    two_sessions: tuple[Path, _Session, _Session],
) -> None:
    projects, _first, _second = two_sessions
    memo = RecentMemo()
    build_recent_payload(projects, now=NOW, memo=memo)
    assert len(memo) == 2
    # Past the window, neither session is listed any more.
    build_recent_payload(projects, now=NOW + timedelta(days=WINDOW_DAYS + 1), memo=memo)
    assert len(memo) == 0


def test_a_memo_sees_an_entry_the_session_row_does_not_count(
    tmp_path: Path,
) -> None:
    """A notification delivered as an attachment ends work, but the
    session row doesn't count attachments: neither its last update nor
    its message count moves. The memo must still notice it."""
    projects = tmp_path / "projects"
    s = _Session("cccc3333-bg", cwd="/home/u/code/foldyard")
    s.prompt(40, "Run it in the background.")
    s.tool(30, command="sleep 99", tool_use_result={"backgroundTaskId": "b1"})
    s.reply(29, "Started.")
    project = _project(projects, "-home-u-code-foldyard", s)
    process_projects_hierarchy(projects, silent=True)
    memo = RecentMemo()
    running = build_recent_payload(projects, now=NOW, memo=memo)
    assert running["sessions"][0]["bg"][0][1] is None

    # Delivered mid-turn, as an attachment: the session row skips those.
    s.notify(20, "b1", "killed", "was stopped", mid_turn=True)
    s.write(project / f"{s.session_id}.jsonl")
    process_projects_hierarchy(projects, silent=True)
    stopped = build_recent_payload(projects, now=NOW, memo=memo)
    assert stopped["sessions"][0]["bg"][0][1:4] == [_ms_ago(20), "bash", "stopped"]
