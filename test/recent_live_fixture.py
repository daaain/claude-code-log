"""Recent sessions that change on disk the way live ones do.

``RecentLive(projects)`` writes a projects directory whose sessions, across
four projects, are in every state the recent sessions page tells apart, then
moves them on stage by stage, appending JSONL lines exactly as Claude Code
does while it runs. Each stage is converted by the real pipeline
(:func:`refresh`: the full run, which writes the cache, the session pages,
``recent.html`` and its ``recent-data.js``), so what the page shows is what
``recent.py`` reads from real transcripts.

``start``
    *pin* (foldyard) and *rows* (claude-code-log) are mid-turn, *bench*
    (wenmode) has finished a turn but its background benchmark is still
    running, *zsh* (dotfiles) ended on an API error, and two older
    sessions finished hours ago.
``tool_runs``
    *rows* edits a file and starts ``just ci``: it moves to the top, still
    mid-turn, its last message that tool call.
``finishes``
    ``just ci`` passes and *rows* answers in Markdown: finished.
``asks_you``
    *pin* asks a question (an unanswered ``AskUserQuestion``): asked you.
``new_session``
    A new session starts in wenmode, mid-turn.
``bg_done``
    *bench*'s benchmark reports back (a ``<task-notification>``) and Claude
    sums it up: finished.

Timestamps are the clock's when each stage is applied (the setup is placed
minutes to hours before it), so the page, on its live clock, reads them as
just happened. Each write moves its file's mtime on by at least 2 s, since
the cache treats a file whose mtime is within 1 s of the recorded one as
unchanged (``test/conftest.bump_mtime``).

Used by ``test/test_recent_live_browser.py`` (end to end), the recent
sessions demo scenes in ``scripts/demos/record.py`` and the play mode,
``scripts/demos/recent_play.py``. Not a test module itself.
"""

from __future__ import annotations

import json
import os
import time
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Distinct in their first 8 characters: the page shows those (the copy
# button), and they are what tells rows apart at a glance.
ROWS = "7f3a9c21-5d0e-4b6a-9e1f-2c8d4a6b0e11"
PIN = "b81e4f07-a2c3-4d95-8f60-1e7c3b9a5d22"
BENCH = "3c9d2e8a-71f4-4c0b-a6d5-9b2e0f4c8a33"
ZSH = "d4507b19-e8a2-4f63-b1c7-6a3d9e2f0b44"
CI_KEY = "5ae1c6d3-0b9f-4e27-8d41-7c5a2b6e9f55"
ATTACH = "9e6f0a4b-3c2d-4a18-b7e9-4d1f8c3a6b66"
TABLES = "1b2c8f5e-6d4a-4f39-a0c2-8e7b5d1a3c77"

STAGES = ("start", "tool_runs", "finishes", "asks_you", "new_session", "bg_done")

#: What each stage is about, for the play mode's log.
DESCRIBE = {
    "start": "six sessions across four projects, in every state",
    "tool_runs": "claude-code-log runs `just ci`: it moves to the top",
    "finishes": "claude-code-log finishes: its reply grows in place",
    "asks_you": "foldyard asks you a question: it slides to the top",
    "new_session": "a new session starts in wenmode: it fades in",
    "bg_done": "wenmode's background benchmark reports back: finished",
}

#: After each stage, the session at the top of the list and its status.
TOP = {
    "start": (PIN, "mid-turn"),
    "tool_runs": (ROWS, "mid-turn"),
    "finishes": (ROWS, "finished"),
    "asks_you": (PIN, "asked you"),
    "new_session": (TABLES, "mid-turn"),
    "bg_done": (BENCH, "finished"),
}

ROWS_REPLY = """\
Rows now slide instead of jumping. Each change is measured, applied, then
played back from where it was:

- a row that moves up the list slides past the others, drawn above them;
- an open row grows by revealing its new content, and the rows below move
  down to make room;
- a closed row's panel fades while the rows below slide up over it.

`just ci` is green, and with reduced motion nothing moves."""

PIN_QUESTION = {
    "questions": [
        {
            "question": "Which cache mode should the 9p mount use?",
            "header": "Cache mode",
            "multiSelect": False,
            "options": [
                {
                    "label": "mmap",
                    "description": "Fastest reads; needs the newer kernel.",
                },
                {"label": "loose", "description": "Works everywhere, slower."},
            ],
        }
    ]
}


def _before(at: datetime, seconds: float) -> datetime:
    return at - timedelta(seconds=seconds)


def _project_dir(name: str) -> str:
    return f"-home-dev-{name}"


class _Session:
    """One session's JSONL, appended to entry by entry."""

    def __init__(
        self, projects: Path, project: str, session_id: str, branch: str
    ) -> None:
        self.cwd = f"/home/dev/{project}"
        self.path = projects / _project_dir(project) / f"{session_id}.jsonl"
        self.session_id = session_id
        self.branch = branch
        self.last_uuid: Optional[str] = None
        self.last_at: Optional[datetime] = None
        self.count = 0
        self.tools = 0
        self.pending: list[dict[str, Any]] = []

    def _stamp(self, at: datetime) -> str:
        # Strictly increasing within the session, whatever the clock does.
        if self.last_at is not None and at <= self.last_at:
            at = self.last_at + timedelta(milliseconds=1)
        self.last_at = at
        utc = at.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"

    def _entry(self, kind: str, at: datetime, **extra: Any) -> dict[str, Any]:
        self.count += 1
        uuid = str(
            uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"{self.session_id}/{self.count}")
        )
        entry: dict[str, Any] = {
            "parentUuid": self.last_uuid,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.1.180",
            "gitBranch": self.branch,
            "type": kind,
            "uuid": uuid,
            "timestamp": self._stamp(at),
        }
        entry.update(extra)
        self.last_uuid = uuid
        self.pending.append(entry)
        return entry

    def title(self, text: str) -> None:
        self.pending.append(
            {"type": "ai-title", "aiTitle": text, "sessionId": self.session_id}
        )

    def prompt(self, at: datetime, text: Any) -> None:
        self._entry("user", at, message={"role": "user", "content": text})

    def assistant(
        self, at: datetime, content: list[dict[str, Any]], model: str = ""
    ) -> None:
        self._entry(
            "assistant",
            at,
            message={
                "id": f"msg_{self.session_id[:8]}{self.count:04d}",
                "type": "message",
                "role": "assistant",
                "model": model or "claude-opus-4-7",
                "stop_reason": None,
                "content": content,
                "usage": {
                    "input_tokens": 6,
                    "cache_creation_input_tokens": 1800,
                    "cache_read_input_tokens": 30000 + 700 * self.count,
                    "output_tokens": 220,
                },
            },
        )

    def say(self, at: datetime, text: str) -> None:
        self.assistant(at, [{"type": "text", "text": text}])

    def error(self, at: datetime, text: str) -> None:
        self.assistant(at, [{"type": "text", "text": text}], model="<synthetic>")

    def call(self, at: datetime, name: str, inp: dict[str, Any]) -> str:
        """A tool call with no result yet; returns its id."""
        self.tools += 1
        tool_id = f"toolu_{self.session_id[:8]}{self.tools:04d}"
        self.assistant(
            at, [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]
        )
        return tool_id

    def result(
        self, at: datetime, tool_id: str, text: str, structured: Any = None
    ) -> None:
        extra = {"toolUseResult": structured} if structured is not None else {}
        self._entry(
            "user",
            at,
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": text}
                ],
            },
            **extra,
        )

    def tool(
        self,
        at: datetime,
        name: str,
        inp: dict[str, Any],
        text: str,
        took: float = 4,
        structured: Any = None,
    ) -> None:
        tool_id = self.call(at, name, inp)
        self.result(at + timedelta(seconds=took), tool_id, text, structured)

    def flush(self) -> None:
        """Append what was written since the last flush, moving the mtime on."""
        if not self.pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        before = self.path.stat().st_mtime if self.path.exists() else 0.0
        with self.path.open("a", encoding="utf-8") as out:
            out.writelines(json.dumps(e) + "\n" for e in self.pending)
        self.pending.clear()
        mtime = max(time.time(), before + 2.0)
        os.utime(self.path, (mtime, mtime))


class RecentLive:
    """The sessions at ``start``, moved on by :meth:`advance`."""

    def __init__(
        self,
        projects: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.projects = projects
        self.clock = clock
        self.stage: Optional[str] = None
        self.sessions: dict[str, _Session] = {}
        self.ci = ""  # the pending `just ci` call's tool id

    def _session(
        self, session_id: str, project: str = "", branch: str = "main"
    ) -> _Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = _Session(
                self.projects, project, session_id, branch
            )
        return self.sessions[session_id]

    @property
    def page(self) -> Path:
        return self.projects / "recent.html"

    @property
    def url(self) -> str:
        return self.page.as_uri()

    # ---- stages -------------------------------------------------------------

    def start(self) -> None:
        now = self.clock()

        def ago(minutes: float) -> datetime:
            return now - timedelta(minutes=minutes)

        rows = self._session(ROWS, "claude-code-log", "recent-motion")
        rows.title("Animate the recent sessions rows")
        rows.prompt(ago(96), "Sketch how the recent sessions rows could animate.")
        rows.tool(ago(95), "Read", {"file_path": "recent.js"}, "(the file)")
        rows.say(ago(88), "FLIP fits: measure, change, then play each row back.")
        rows.prompt(
            ago(9),
            "Rows jump when the list reorders. Make them slide instead, "
            "and keep it still for reduced motion.",
        )
        rows.tool(ago(8.5), "Read", {"file_path": "recent.js"}, "(the file)")
        rows.tool(ago(6), "Edit", {"file_path": "recent.js"}, "Edited recent.js")
        rows.tool(
            ago(2),
            "Bash",
            {"command": "just test-browser -k motion"},
            "5 passed in 2.1s",
            took=30,
        )

        pin = self._session(PIN, "foldyard", "lima-mounts")
        pin.title("Pin the 9p cache to mmap")
        pin.prompt(ago(70), "Why is the to-box mount so slow on reads?")
        pin.say(ago(64), "The 9p mount has no cache, so every read is a round trip.")
        pin.prompt(ago(4), "Try pinning the 9p cache to mmap.")
        pin.tool(ago(3.5), "Read", {"file_path": "lima.yaml"}, "(the file)")
        pin.tool(ago(1.2), "Bash", {"command": "uname -r"}, "6.8.0-45-generic")

        bench = self._session(BENCH, "wenmode", "main")
        bench.title("Benchmark the tokenizer")
        bench.prompt(ago(15), "Benchmark the new tokenizer against main.")
        bench.tool(
            ago(14),
            "Bash",
            {"command": "just bench --rounds 20", "run_in_background": True},
            "Command running in background with ID: bench1",
            took=1,
            structured={"stdout": "", "backgroundTaskId": "bench1"},
        )
        bench.say(ago(13), "The benchmark is running; I'll report when it finishes.")

        zsh = self._session(ZSH, "dotfiles", "main")
        zsh.title("Tidy the zsh prompt")
        zsh.prompt(ago(31), "Make the zsh prompt show the git branch.")
        zsh.tool(ago(30), "Read", {"file_path": ".zshrc"}, "(the file)")
        zsh.error(ago(26), "API Error: 529 Overloaded. Please try again.")

        ci_key = self._session(CI_KEY, "claude-code-log", "main")
        ci_key.title("Fix the Windows CI cache key")
        ci_key.prompt(ago(170), "The Windows CI job misses the cache every run.")
        ci_key.tool(ago(168), "Bash", {"command": "gh run view"}, "(the log)")
        ci_key.say(ago(160), "The key hashed a path with backslashes; fixed.")

        attach = self._session(ATTACH, "foldyard", "main")
        attach.title("Speed up fy agent attach")
        attach.prompt(ago(300), "fy agent attach takes four seconds. Why?")
        attach.say(ago(290), "It waited on a DNS lookup; now it doesn't.")

    # Each stage ends at the clock's now, its earlier steps just before it:
    # an entry stamped ahead of the clock would outrank the next stage's.

    def tool_runs(self) -> None:
        rows = self.sessions[ROWS]
        now = self.clock()
        rows.tool(_before(now, 6), "Edit", {"file_path": "recent.css"}, "Edited", 1)
        self.ci = rows.call(now, "Bash", {"command": "just ci"})

    def finishes(self) -> None:
        rows = self.sessions[ROWS]
        now = self.clock()
        rows.result(_before(now, 2), self.ci, "All checks passed!")
        rows.say(now, ROWS_REPLY)

    def asks_you(self) -> None:
        self.sessions[PIN].call(self.clock(), "AskUserQuestion", PIN_QUESTION)

    def new_session(self) -> None:
        tables = self._session(TABLES, "wenmode", "docs-tables")
        now = self.clock()
        tables.title("Render tables in the docs")
        tables.prompt(_before(now, 8), "The docs site drops Markdown tables. Why?")
        tables.tool(_before(now, 6), "Read", {"file_path": "mkdocs.yml"}, "(it)", 1)
        tables.call(now, "Grep", {"pattern": "tables", "path": "docs"})

    def bg_done(self) -> None:
        bench = self.sessions[BENCH]
        now = self.clock()
        bench.prompt(
            _before(now, 3),
            "<task-notification>\n<task-id>bench1</task-id>\n"
            "<status>completed</status>\n<summary>Background command "
            '"just bench --rounds 20" completed (exit code 0)</summary>\n'
            "</task-notification>",
        )
        bench.say(
            now,
            "The new tokenizer runs at **41 MB/s**, 18% faster than main, "
            "with the same output on every fixture.",
        )

    # ---- running ------------------------------------------------------------

    def advance(self, stage: str) -> None:
        """Apply ``stage`` (in ``STAGES`` order) and convert the result."""
        expected = STAGES[STAGES.index(self.stage) + 1] if self.stage else STAGES[0]
        if stage != expected:
            raise ValueError(f"next stage is {expected!r}, not {stage!r}")
        getattr(self, stage)()
        for session in self.sessions.values():
            session.flush()
        self.stage = stage
        refresh(self.projects)

    def run_to(self, stage: str) -> None:
        """Apply every stage up to and including ``stage``."""
        while self.stage != stage:
            nxt = STAGES[STAGES.index(self.stage) + 1] if self.stage else STAGES[0]
            self.advance(nxt)


def refresh(projects: Path) -> None:
    """Convert ``projects`` as a full run does: the cache, the session pages,
    the index, ``recent.html`` and ``recent-data.js``."""
    from claude_code_log.converter import process_projects_hierarchy

    process_projects_hierarchy(projects, silent=True, theme="minimal", jobs=1)


#: Waits for the top row to be ``[session id, status]``, prompting the page to reload
#: its data file meanwhile (a ``visibilitychange`` while visible reloads it
#: now rather than at its next 15 s tick).
UNTIL_TOP_JS = """([id, status]) => {
    const row = document.querySelector('.rc-row');
    const ok = !!row && row.dataset.id === id
        && row.querySelector('.rc-block-status').textContent === status;
    if (!ok && Date.now() - (window.__recentNudge || 0) > 400) {
        window.__recentNudge = Date.now();
        document.dispatchEvent(new Event('visibilitychange'));
    }
    return ok;
}"""


def wait_for_stage(page: Any, stage: str, timeout: float = 30_000) -> None:
    """Wait until ``page`` (a Playwright page on ``recent.html``) shows
    ``stage``'s top row."""
    session_id, status = TOP[stage]
    page.wait_for_function(
        UNTIL_TOP_JS, arg=[session_id, status], polling=100, timeout=timeout
    )
