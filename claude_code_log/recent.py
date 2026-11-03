"""Recently active sessions across every project, for ``recent.html``.

The recent-sessions page lists the sessions that changed lately, newest
update first, each with a strip of its turns over the last hours and its
last message. Everything here comes from the transcripts (via the cache):
nothing says whether an agent is still *running* — a transcript cannot
tell a stopped agent from one waiting quietly — so the states are about
whose move it is, not about liveness.

The page is static and refreshes itself: a ``file://`` page cannot fetch
anything, but it can add a ``<script>`` tag, so the data lives in a
sibling ``recent-data.js`` that calls ``window.__cclRecent(payload)``.
The page re-adds that script every few seconds and redraws its rows.
:func:`write_recent_data` rewrites the file; it is cheap (one query for
the session rows, then each recent session's entries from the cache), so
the watch loop calls it after every tick, keeping a :class:`RecentMemo`
so a tick re-reads only the sessions that changed.

Payload shape (all times are epoch milliseconds, UTC)::

    {
      "v": 1,
      "generatedAt": 1793465000000,
      "windowDays": 3,
      "cacheTtlMinutes": 60,
      "bgCapMinutes": 60,
      "wakeGraceMinutes": 5,
      "sessions": [
        {
          "id": "8b04d7c2-...", "short": "8b04d7c2",
          "project": "foldyard", "projectDir": "-home-u-code-foldyard",
          "worktree": "lima-mounts" | null, "branch": "lima-mounts" | null,
          "title": "Pin the to-box 9p cache to mmap",
          "href": "-home-u-code-foldyard/session-8b04d7c2-....html" | null,
          "firstAt": ..., "lastAt": ...,
          "state": "asked" | "error" | "done" | "busy",
          "messages": 96, "turnCount": 3, "toolCount": 41,
          "turns": [[startMs, endMs, "asst" | "ask" | "err" | "open",
                     promptSnippet | null, endSnippet | null,
                     "you" | "task" | "wake"], ...],
          "bg": [[startMs, endMs | null, "bash" | "agent" | "monitor",
                  "completed" | "failed" | "stopped" | null, label,
                  expiresAtMs | null], ...],
          "wakes": [[atMs, label], ...],
          "earlierTurns": 0,
          "tools": [ms, ...],
          "lastPrompt": {"at": ms, "html": "..."} | null,
          "last": {"at": ms, "who": "claude" | "question" | "plan",
                   "html": "..."}
                | {"at": ms, "who": "tool" | "error", "text": "..."}
                | null
        }
      ]
    }

``turns`` holds only the turns that ended within :data:`TURN_HORIZON_HOURS`
(the strip shows less than that); ``earlierTurns`` counts the rest, so
the page can say "+N earlier turns" without carrying them. Each turn's
snippets (the start of its prompt, and of how it ended) are what the
strip's markers show on hover; an implicit turn has no prompt.

``html`` is the text rendered as Markdown by the session pages'
pipeline (:func:`claude_code_log.html.utils.render_markdown`, which
escapes raw HTML and refuses unsafe link schemes), so it is no more
trusted than a session page's message body. The prompt and Claude's
own text carry it in place of ``text``; a tool line or an error stays
plain ``text``. An unanswered ``AskUserQuestion`` or ``ExitPlanMode``
is Claude waiting on you: its questions and options (``question``) or
its plan (``plan``) become the last message and the turn ends ``ask``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, cast

from .cache import CacheManager, get_cache_db_path, get_library_version
from .background_work import BackgroundWork
from .models import (
    AssistantTranscriptEntry,
    AttachmentTranscriptEntry,
    QueueOperationTranscriptEntry,
    SystemTranscriptEntry,
    TextContent,
    ToolResultContent,
    ToolUseContent,
    TranscriptEntry,
    UserTranscriptEntry,
)
from .html.utils import render_markdown, render_user_markdown
from .utils import get_project_display_name

RECENT_PAGE = "recent.html"
RECENT_DATA = "recent-data.js"

#: Sessions whose last update is older than this are left off the page.
WINDOW_DAYS = 3
#: At most this many sessions, newest first: the page is a glance, not an
#: archive, and every session carries its turns and last message.
MAX_SESSIONS = 60
#: Turns that ended longer ago than this are counted, not listed.
TURN_HORIZON_HOURS = 24
#: Tool-call times kept per session (the strip draws one tick each).
MAX_TOOL_TICKS = 400
#: The prompt cache's lifetime the page counts down against.
CACHE_TTL_MINUTES = 60
#: Long texts are cut here; the session page has the rest.
MAX_LAST_CHARS = 6000
MAX_PROMPT_CHARS = 1500
MAX_TITLE_CHARS = 200
#: A marker's hover text: the start of the turn's prompt or its end.
SNIPPET_CHARS = 200
#: Background items (commands, agents, monitors) kept per session.
MAX_BG_ITEMS = 60
#: Work with no end on record counts as running for this long after the
#: session's last entry (a monitor's own expiry can end it sooner): a
#: transcript can't tell work still running from work that died with it.
BG_CAP_MINUTES = 60
#: A wake-up this long past its time without firing was missed.
WAKE_GRACE_MINUTES = 5

# User-turn bodies Claude Code injects that are not something you typed.
_NOT_A_PROMPT_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "Caveat: The messages below were generated by the user",
    "<task-notification>",
    "<system-reminder>",
)
_API_ERROR_PREFIX = "API Error"
_NOTIFICATION_TAG = "<task-notification>"
_WAKE_FIRE = "scheduled_task_fire"


def _ms(timestamp: Optional[str]) -> Optional[int]:
    """An ISO 8601 timestamp as epoch milliseconds, or None."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _snippet(text: Optional[str]) -> Optional[str]:
    """One line of hover text: whitespace folded, clipped."""
    if not text:
        return None
    return _clip(" ".join(text.split()), SNIPPET_CHARS) or None


def _entry_text(entry: UserTranscriptEntry) -> str:
    return "\n".join(
        item.text for item in entry.message.content if isinstance(item, TextContent)
    ).strip()


def _user_text(entry: UserTranscriptEntry) -> Optional[str]:
    """The text of a user entry that is a real prompt, else None.

    Tool results, meta entries and the bodies Claude Code injects
    (command output, caveats, task notifications) are not prompts. A
    notification can follow a preamble, so it is searched for.
    """
    if entry.isMeta:
        return None
    if any(isinstance(item, ToolResultContent) for item in entry.message.content):
        return None
    text = _entry_text(entry)
    if not text or text.startswith(_NOT_A_PROMPT_PREFIXES) or _NOTIFICATION_TAG in text:
        return None
    return text


def _new_turn(
    at: int, origin: str, prompt: Optional[str] = None, trigger: Optional[str] = None
) -> dict[str, Any]:
    """A turn: started by your ``prompt`` (origin ``you``), or by
    background work reporting (``task``) or a wake-up (``wake``), whose
    ``trigger`` says what."""
    return {
        "start": at,
        "end": at,
        "origin": origin,
        "prompt": prompt,
        "trigger": trigger,
        "tools": [],
        "last": None,
        "end_kind": "open",
    }


def _queued_notification(
    entry: AttachmentTranscriptEntry | QueueOperationTranscriptEntry,
) -> Optional[str]:
    """A notification that ends work without starting a turn: delivered
    mid-turn (a ``queued_command`` attachment), or only queued (a
    ``queue-operation``; a session closed before delivering it)."""
    if isinstance(entry, QueueOperationTranscriptEntry):
        if entry.operation != "enqueue":
            return None
        content = entry.content
        text = (
            content
            if isinstance(content, str)
            else "\n".join(i.text for i in content or [] if isinstance(i, TextContent))
        )
    else:
        if entry.attachment.get("type") != "queued_command":
            return None
        prompt = entry.attachment.get("prompt")
        text = prompt if isinstance(prompt, str) else ""
    return text if _NOTIFICATION_TAG in text else None


def _tool_line(item: ToolUseContent) -> str:
    """One line naming a tool call: ``Bash · just test -k race``."""
    data = item.input or {}
    for key in ("command", "file_path", "pattern", "path", "url", "query", "prompt"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return f"{item.name} · {_clip(first, 160)}"
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        return f"{item.name} · {_clip(description, 160)}"
    return item.name


def _dicts(value: Any) -> list[dict[str, Any]]:
    """The mappings in a JSON list (anything else in it, or a non-list, is
    dropped): tool inputs are whatever the model wrote."""
    if not isinstance(value, list):
        return []
    items = cast(list[Any], value)
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _nonblank(value: Any) -> Optional[str]:
    """A string with something in it, stripped; else None."""
    return (value.strip() or None) if isinstance(value, str) else None


def _questions_markdown(data: dict[str, Any]) -> Optional[str]:
    """An ``AskUserQuestion`` call's questions and options, as Markdown.

    None when the input has no usable question, so a malformed call
    stays an ordinary tool call.
    """
    blocks: list[str] = []
    for question in _dicts(data.get("questions")):
        text = _nonblank(question.get("question"))
        if text is None:
            continue
        options: list[str] = []
        for option in _dicts(question.get("options")):
            label = _nonblank(option.get("label"))
            if label is None:
                continue
            description = _nonblank(option.get("description"))
            line = f"- **{label}**"
            if description:
                line += f" — {' '.join(description.split())}"
            options.append(line)
        blocks.append(text)
        if options:
            blocks.append("\n".join(options))
    return "\n\n".join(blocks) or None


#: Who wrote a last message that is Markdown (the rest are plain lines).
_MARKDOWN_AUTHORS = frozenset({"claude", "question", "plan"})
#: ExitPlanMode's input carries no plan when it lives in a plan file.
_PLAN_FALLBACK = "The plan is ready for you to approve."


def _waits_on_you(item: ToolUseContent) -> Optional[tuple[str, str]]:
    """``(who, Markdown)`` for a tool call that waits on you, else None.

    ``AskUserQuestion`` (its questions and options) and ``ExitPlanMode``
    (the plan) hold the turn until you answer; a malformed question call
    is an ordinary tool call.
    """
    data = item.input or {}
    if item.name == "AskUserQuestion":
        questions = _questions_markdown(data)
        return ("question", questions) if questions else None
    if item.name == "ExitPlanMode":
        return ("plan", _nonblank(data.get("plan")) or _PLAN_FALLBACK)
    return None


def asks_you(text: str) -> bool:
    """Whether a reply ends on a question to you.

    A heuristic: the last non-blank line, with trailing Markdown emphasis
    and closing brackets/quotes stripped, ends with a question mark.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    return lines[-1].rstrip("*_`)]\"'”’ ").endswith("?")


@dataclass(frozen=True)
class SessionSummary:
    """What a session's entries say, before the clock is applied.

    Every turn, the background work, the state and the last prompt and
    message (already rendered): the costly part of a session's row, and
    a function of its entries alone, so a :class:`RecentMemo` can keep
    it between builds. :meth:`at` applies the clock (which turns,
    background items and wake-ups fall inside the horizon) and is cheap.
    """

    branch: Optional[str]
    cwd: Optional[str]
    state: str
    turns: list[dict[str, Any]]
    background: BackgroundWork
    last_prompt: Optional[dict[str, Any]]
    last: Optional[dict[str, Any]]

    def at(self, now_ms: int) -> dict[str, Any]:
        """The session's row data as of ``now_ms``."""
        horizon = now_ms - TURN_HORIZON_HOURS * 3600 * 1000
        shown = [t for t in self.turns if t["end"] >= horizon]
        return {
            "branch": self.branch,
            "cwd": self.cwd,
            "state": self.state,
            "turnCount": len(self.turns),
            "toolCount": sum(len(t["tools"]) for t in self.turns),
            "turns": [
                [
                    t["start"],
                    t["end"],
                    t["end_kind"],
                    _snippet(t["prompt"] or t["trigger"]),
                    _snippet(t["last"]["text"]) if t["last"] else None,
                    t["origin"],
                ]
                for t in shown
            ],
            "earlierTurns": len(self.turns) - len(shown),
            "tools": [ms for t in shown for ms in t["tools"]][-MAX_TOOL_TICKS:],
            "bg": self.background.items(horizon, MAX_BG_ITEMS),
            "wakes": self.background.pending_wakes(horizon),
            "lastPrompt": self.last_prompt,
            "last": self.last,
        }


def summarise_session(
    entries: Iterable[TranscriptEntry], *, now_ms: int
) -> dict[str, Any]:
    """Turns, tool calls, the last prompt and message, and a state, as of
    ``now_ms`` (see :func:`read_session`)."""
    return read_session(entries).at(now_ms)


def read_session(entries: Iterable[TranscriptEntry]) -> SessionSummary:
    """Read a session's entries into its :class:`SessionSummary`.

    A turn runs from a prompt you typed to the last entry before the next
    one. How it ended: ``ask`` (Claude's last text asks you something),
    ``err`` (an API error was the last entry), ``open`` (the last turn of
    the session ends on a tool call or result, so it is mid-turn — or
    the agent stopped mid-turn; the transcript cannot tell) or ``asst``.
    """
    turns: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    branch: Optional[str] = None
    cwd: Optional[str] = None
    background = BackgroundWork()

    for entry in entries:
        if not isinstance(
            entry,
            (
                UserTranscriptEntry,
                AssistantTranscriptEntry,
                SystemTranscriptEntry,
                AttachmentTranscriptEntry,
                QueueOperationTranscriptEntry,
            ),
        ):
            continue
        at = _ms(entry.timestamp)
        if at is None:
            continue
        if isinstance(entry, QueueOperationTranscriptEntry):
            queued = _queued_notification(entry)
            if queued is not None:
                background.notified(queued, at)
            continue
        if entry.isSidechain:
            continue

        if isinstance(entry, SystemTranscriptEntry):
            if entry.subtype == _WAKE_FIRE:
                label = background.fired(at) or entry.content
                current = _new_turn(at, "wake", trigger=label)
                turns.append(current)
            continue
        if isinstance(entry, AttachmentTranscriptEntry):
            queued = _queued_notification(entry)
            if queued is not None:
                # Mid-turn: it ends work, but the turn is already going.
                background.notified(queued, at)
            continue

        branch = entry.gitBranch or branch
        cwd = entry.cwd or cwd

        if isinstance(entry, UserTranscriptEntry):
            results = [
                i for i in entry.message.content if isinstance(i, ToolResultContent)
            ]
            for result in results:
                # A toolUseResult describes the entry's one result.
                structured = entry.toolUseResult if len(results) == 1 else None
                background.tool_result(result, structured, at)
            prompt = _user_text(entry)
            if prompt is not None:
                current = _new_turn(at, "you", prompt=prompt)
                turns.append(current)
                continue
            text = "" if results or entry.isMeta else _entry_text(entry)
            if _NOTIFICATION_TAG in text:
                # Delivered while Claude was idle: it starts a turn.
                reported = background.notified(text, at)
                label = next((n.label for n in reported if n.label), None)
                current = _new_turn(at, "task", trigger=label)
                turns.append(current)
                continue
            if current is None:
                continue
            # A tool result (or injected body) keeps the turn going.
            current["end"] = max(current["end"], at)
            if results:
                current["end_kind"] = "open"
            continue

        if current is None:
            # Assistant output before any prompt (a resumed or forked
            # session's head): start an implicit turn so it still counts.
            current = _new_turn(at, "you")
            turns.append(current)
        current["end"] = max(current["end"], at)
        synthetic = entry.message.model == "<synthetic>"
        for item in entry.message.content:
            if isinstance(item, ToolUseContent):
                current["tools"].append(at)
                background.tool_call(item, at)
                waiting = _waits_on_you(item)
                if waiting is not None:
                    # Waiting on your answer or approval; its tool result
                    # (a user entry) reopens the turn once you give it.
                    who, text = waiting
                    current["last"] = {"at": at, "who": who, "text": text}
                    current["end_kind"] = "ask"
                else:
                    current["last"] = {
                        "at": at,
                        "who": "tool",
                        "text": _tool_line(item),
                    }
                    current["end_kind"] = "open"
            elif isinstance(item, TextContent) and item.text.strip():
                text = item.text.strip()
                if synthetic or text.startswith(_API_ERROR_PREFIX):
                    current["last"] = {"at": at, "who": "error", "text": text}
                    current["end_kind"] = "err"
                else:
                    current["last"] = {"at": at, "who": "claude", "text": text}
                    current["end_kind"] = "ask" if asks_you(text) else "asst"

    # Only the session's final turn can still be under way; an earlier
    # turn that ended on a tool call was interrupted and is just over.
    for turn in turns[:-1]:
        if turn["end_kind"] == "open":
            turn["end_kind"] = "asst"

    final = turns[-1] if turns else None
    state = "done"
    if final is not None:
        state = {"ask": "asked", "err": "error", "open": "busy"}.get(
            final["end_kind"], "done"
        )

    last_prompt = next((t for t in reversed(turns) if t["prompt"]), None)
    last = final["last"] if final is not None else None

    return SessionSummary(
        branch=branch,
        cwd=cwd,
        state=state,
        turns=turns,
        background=background,
        last_prompt=_rendered(
            last_prompt["start"] if last_prompt else 0,
            last_prompt["prompt"] if last_prompt else None,
            MAX_PROMPT_CHARS,
            render_user_markdown,
        ),
        last=(
            _rendered(
                last["at"],
                last["text"],
                MAX_LAST_CHARS,
                render_markdown if last["who"] in _MARKDOWN_AUTHORS else None,
                who=last["who"],
            )
            if last
            else None
        ),
    )


#: A fingerprint of the cached entries a session's summary is read from:
#: how many there are and the highest row id. Any insert (an appended
#: entry, a rewritten file) moves the id; the session row's own count and
#: last update would not do, since they skip attachments, and an
#: attachment can end background work.
_MemoKey = tuple[Optional[int], Optional[int]]


class RecentMemo:
    """:class:`SessionSummary` objects kept between payload builds.

    ``watch`` rewrites the data file after every tick, and reading a
    session's entries back from the cache is nearly all of a build's
    cost; most recent sessions haven't changed since the last tick. So
    the loop owns one memo, and a build re-reads only the sessions whose
    cached entries changed (see ``_MemoKey``). The clock is not part
    of the summary — :meth:`SessionSummary.at` applies it on every build
    — so a kept summary never serves a stale horizon. Sessions that leave
    the list are dropped, which bounds the memo by the page's own limit.
    """

    def __init__(self) -> None:
        self._kept: dict[str, tuple[_MemoKey, SessionSummary]] = {}

    def __len__(self) -> int:
        return len(self._kept)

    def summary(
        self, session_id: str, key: _MemoKey, read: Callable[[], SessionSummary]
    ) -> SessionSummary:
        kept = self._kept.get(session_id)
        if kept is not None and kept[0] == key:
            return kept[1]
        summary = read()
        self._kept[session_id] = (key, summary)
        return summary

    def keep_only(self, session_ids: Iterable[str]) -> None:
        wanted = set(session_ids)
        for session_id in [s for s in self._kept if s not in wanted]:
            del self._kept[session_id]


def _rendered(
    at: int,
    text: Optional[str],
    limit: int,
    render: Optional[Callable[[str], str]],
    **extra: str,
) -> Optional[dict[str, Any]]:
    """``{"at", [who], "html"}`` for a prompt or last message, or
    ``{"at", [who], "text"}`` when it isn't Markdown (``render`` None).

    One or the other, never both: the page shows one, and the data file
    is reloaded every few seconds. The text is clipped first, so the
    Markdown rendered is the text shown; a fence the clip leaves open
    simply runs to the end.
    """
    if not text:
        return None
    clipped = _clip(text, limit)
    if render is None:
        return {"at": at, **extra, "text": clipped}
    return {"at": at, **extra, "html": render(clipped)}


def _worktree(cwd: Optional[str], project: str) -> Optional[str]:
    """The checkout's own folder name when it isn't the project's."""
    if not cwd:
        return None
    name = Path(cwd).name
    return name if name and name != project else None


def _default_href(project_dir: Path, session_id: str) -> str:
    """Where the default layout puts a session page, relative to the index."""
    return f"{project_dir.name}/session-{session_id}.html"


def _session_rows(db_path: Path, cutoff_iso: str, limit: int) -> list[sqlite3.Row]:
    """The newest visible session rows across every project.

    A ``mode=ro`` reader: it never writes the shared database, so it needs
    none of the write pragmas (see the connection census in
    ``test/test_cache_sqlite_integrity.py``).
    """
    if not db_path.exists():
        return []
    # as_uri() percent-encodes the absolute path, as the cache's own
    # read-only connections do, so spaces and "?" in it stay a path.
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # entry_count / entry_max_id fingerprint exactly the rows
        # load_session_entries reads (the RecentMemo key): the session
        # row's own counts skip attachments, which can end background
        # work, and any insert moves MAX(id). Index-only, via
        # idx_messages_project_session_ts.
        return conn.execute(
            """SELECT s.session_id, s.summary, s.ai_title, s.first_timestamp,
                      s.last_timestamp, s.message_count, s.first_user_message,
                      s.cwd, p.project_path,
                      (SELECT COUNT(*) FROM messages m
                        WHERE m.project_id = s.project_id
                          AND m.session_id = s.session_id) AS entry_count,
                      (SELECT MAX(m.id) FROM messages m
                        WHERE m.project_id = s.project_id
                          AND m.session_id = s.session_id) AS entry_max_id
               FROM sessions s JOIN projects p ON p.id = s.project_id
               WHERE s.hidden = 0 AND s.last_timestamp >= ?
               ORDER BY s.last_timestamp DESC
               LIMIT ?""",
            (cutoff_iso, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def build_recent_payload(
    projects_dir: Path,
    *,
    now: Optional[datetime] = None,
    href_for: Optional[Callable[[Path, str], Optional[str]]] = None,
    db_path: Optional[Path] = None,
    memo: Optional[RecentMemo] = None,
) -> dict[str, Any]:
    """The page's data: the recently updated sessions across every project.

    ``href_for(project_dir, session_id)`` gives each session page's link
    relative to ``recent.html`` (which sits next to the index); without
    it the default layout's ``<project dir>/session-<id>.html`` is used.
    A link to a page that wasn't written is harmless: the row still
    shows, and the browser reports a missing file only if it is followed.
    With a ``memo`` (``watch`` keeps one), sessions unchanged since the
    last build aren't read again.
    """
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    cutoff = (now - timedelta(days=WINDOW_DAYS)).astimezone(timezone.utc)
    # Cached timestamps are ISO 8601 in UTC ("...Z"), so they sort as text.
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    db_path = db_path or get_cache_db_path(projects_dir)
    rows = _session_rows(db_path, cutoff_iso, MAX_SESSIONS)

    managers: dict[str, CacheManager] = {}
    names: dict[str, str] = {}
    sessions: list[dict[str, Any]] = []
    for row in rows:
        project_path = Path(row["project_path"])
        key = str(project_path)
        session_id = row["session_id"]
        try:
            if key not in managers:
                managers[key] = CacheManager(
                    project_path, get_library_version(), db_path, read_only=True
                )
                names[key] = get_project_display_name(
                    project_path.name, managers[key].get_working_directories()
                )
            manager = managers[key]

            def read(manager: CacheManager = manager) -> SessionSummary:
                return read_session(manager.load_session_entries(session_id))

            summary = (
                memo.summary(
                    session_id, (row["entry_count"], row["entry_max_id"]), read
                )
                if memo is not None
                else read()
            )
        except Exception:
            # One unreadable project must not take the page down.
            continue
        info = summary.at(now_ms)
        project = names[key]
        title = row["ai_title"] or row["summary"] or row["first_user_message"] or ""
        href = (href_for or _default_href)(project_path, session_id)
        sessions.append(
            {
                "id": session_id,
                "short": session_id[:8],
                "project": project,
                "projectDir": project_path.name,
                "worktree": _worktree(info["cwd"] or row["cwd"], project),
                "branch": info["branch"],
                "title": _clip(" ".join(title.split()), MAX_TITLE_CHARS)
                or session_id[:8],
                "href": href,
                "firstAt": _ms(row["first_timestamp"]),
                "lastAt": _ms(row["last_timestamp"]),
                "state": info["state"],
                "messages": row["message_count"],
                "turnCount": info["turnCount"],
                "toolCount": info["toolCount"],
                "turns": info["turns"],
                "earlierTurns": info["earlierTurns"],
                "tools": info["tools"],
                "bg": info["bg"],
                "wakes": info["wakes"],
                "lastPrompt": info["lastPrompt"],
                "last": info["last"],
            }
        )

    if memo is not None:
        memo.keep_only(row["session_id"] for row in rows)

    return {
        "v": 1,
        "generatedAt": now_ms,
        "windowDays": WINDOW_DAYS,
        "cacheTtlMinutes": CACHE_TTL_MINUTES,
        "bgCapMinutes": BG_CAP_MINUTES,
        "wakeGraceMinutes": WAKE_GRACE_MINUTES,
        "sessions": sessions,
    }


def payload_json(payload: dict[str, Any]) -> str:
    """The payload as JSON that is also safe inside an inline ``<script>``.

    Transcript text is untrusted: ``</script>`` (or ``<!--``) in a
    message must not end or comment out the element it is embedded in.
    Escaping ``<`` as ``\\u003c`` keeps the JSON identical once parsed.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


def recent_data_js(payload: dict[str, Any]) -> str:
    """The ``recent-data.js`` body: one call handing the page its payload."""
    return (
        "/* Generated by claude-code-log: the recent sessions page reloads this"
        " file to refresh itself. */\n"
        f"window.__cclRecent && window.__cclRecent({payload_json(payload)});\n"
    )


def write_recent_data(
    index_dir: Path,
    projects_dir: Path,
    *,
    payload: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    memo: Optional[RecentMemo] = None,
) -> Optional[dict[str, Any]]:
    """Rewrite ``recent-data.js`` next to ``recent.html``, if that page exists.

    Returns the payload written, or None when there is no page to feed
    (a single-file conversion, or an output tree without the index).
    ``memo`` carries session summaries over from the previous call (see
    :class:`RecentMemo`).
    """
    from .utils import atomic_write_text

    if not (index_dir / RECENT_PAGE).exists():
        return None
    payload = payload or build_recent_payload(projects_dir, now=now, memo=memo)
    atomic_write_text(index_dir / RECENT_DATA, recent_data_js(payload))
    return payload
