"""Background work a session started, read from its transcript.

For the recent sessions page (:mod:`claude_code_log.recent`): which
background commands, async agents and monitors a session launched, when
each ended and how, and which wake-ups are still to come. Like the rest
of that page it is the transcript's account only: work with no end on
record may still be running, or may have died with its process; the page
decides how long to believe it is running.

How each kind shows up (surveyed across real transcripts):

- **Launch.** The tool result of a ``Bash``, ``Agent``/``Task`` or
  ``Monitor`` call names the id the work runs under: ``backgroundTaskId``,
  ``agentId`` (with ``status: "async_launched"``) or ``taskId`` in the
  structured ``toolUseResult``, or the same id in the result text. Bash
  is found by its result, not its ``run_in_background`` flag: a command
  that outlives its timeout is moved to the background without it.
- **End.** A ``<task-notification>`` with that ``<task-id>`` and a final
  ``<status>``, or a ``TaskStop`` / ``KillShell`` / ``KillBash`` on the
  id. A monitor's events are notifications without a status; its expiry
  is an event whose text says so. An agent can report an interim
  "waiting on its own background work" before its real end. When it
  then reports by ``SubagentHandback``, the notification that follows
  keeps that interim summary; only its result says the report was
  delivered, and that is the end.
- **Wake-ups.** ``ScheduleWakeup``'s ``toolUseResult.scheduledFor`` is
  exactly when it fires (or the result text gives the delay); a
  ``{stop: true}`` call cancels what is pending. The firing is a
  ``system`` entry with subtype ``scheduled_task_fire``.

Notifications arrive in a user entry when Claude is idle (and start a
turn) or in a ``queued_command`` attachment mid-turn; the user-entry
body may open with a ``[SYSTEM NOTIFICATION …]`` preamble, so they are
found by searching, not by prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, cast

from .models import ToolResultContent, ToolUseContent

#: The tools whose launch can start background work, and its kind.
_KINDS = {"Bash": "bash", "Agent": "agent", "Task": "agent", "Monitor": "monitor"}
#: The tools that stop background work, and the input key naming its id.
_STOPPERS = {"TaskStop": "task_id", "KillShell": "shell_id", "KillBash": "shell_id"}
#: Final notification statuses, as the page names them.
_FINAL = {
    "completed": "completed",
    "failed": "failed",
    "stopped": "stopped",
    "killed": "stopped",
}
#: A fire matches the pending wake-up nearest it, within this.
_FIRE_MATCH_MS = 10 * 60000

_NOTIFICATION_RE = re.compile(
    r"<task-notification>\s*(?P<body>.*?)\s*</task-notification>", re.DOTALL
)
_FIELD_RE = re.compile(
    r"<(?P<tag>task-id|status|summary|event|result)>(?P<body>.*?)</(?P=tag)>",
    re.DOTALL,
)
# Launch ids in result text, for results without a structured
# toolUseResult: only the harness's own launch message, at the start of
# the result (a foreground command's output can say anything).
_LAUNCH_ID_RES = {
    "bash": (
        re.compile(r"\A\s*Command running in background with ID: (?P<id>[\w-]+)"),
        re.compile(
            r"\A\s*Command did not complete within [^\n]*?"
            r"moved to the background \(ID: (?P<id>[\w-]+)\)"
        ),
    ),
    "agent": (
        re.compile(
            r"\A\s*Async agent launched successfully\b.*?^agentId: (?P<id>[\w-]+)",
            re.DOTALL | re.MULTILINE,
        ),
    ),
    "monitor": (re.compile(r"\A\s*Monitor started \(task (?P<id>[\w-]+)"),),
}
_MONITOR_TIMEOUT_RE = re.compile(r"timeout (?P<ms>\d+)ms")
_MONITOR_EXPIRES_RE = re.compile(r"expires in (?P<min>\d+)m\b")
_WAKE_IN_RE = re.compile(r"Next wakeup scheduled for [\d:]+ \(in (?P<s>\d+)s\)")
_MONITOR_EXPIRED_PREFIXES = ("[Monitor timed out", "[Monitor expired")
_INTERIM = "is waiting on its own background work"
_HANDED_BACK = "report was delivered to you"


@dataclass
class Notification:
    """One ``<task-notification>`` block's fields."""

    task_id: Optional[str]
    status: Optional[str]
    summary: Optional[str]
    event: Optional[str]
    result: Optional[str] = None

    @property
    def label(self) -> Optional[str]:
        """What it says happened: the summary, else the event."""
        return self.summary or self.event

    @property
    def interim(self) -> bool:
        """An agent saying it is waiting on its own work, not ending.
        After a hand-back the summary still says so, but the result
        says the report was delivered: that one is the end."""
        return _INTERIM in (self.summary or "") and _HANDED_BACK not in (
            self.result or ""
        )


def notifications(text: str) -> list[Notification]:
    """Every ``<task-notification>`` block in ``text``, in order."""
    found: list[Notification] = []
    for block in _NOTIFICATION_RE.finditer(text):
        fields = {
            m.group("tag"): m.group("body").strip()
            for m in _FIELD_RE.finditer(block.group("body"))
        }
        found.append(
            Notification(
                task_id=fields.get("task-id") or None,
                status=fields.get("status") or None,
                summary=fields.get("summary") or None,
                event=fields.get("event") or None,
                result=fields.get("result") or None,
            )
        )
    return found


@dataclass
class _Work:
    kind: str
    label: str
    start: int
    end: Optional[int] = None
    status: Optional[str] = None
    expires: Optional[int] = None


@dataclass
class _Wake:
    at: int
    label: str


def _result_text(result: ToolResultContent) -> str:
    if isinstance(result.content, str):
        return result.content
    return "\n".join(str(part.get("text", "")) for part in result.content)


def _structured(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _first_line(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().splitlines()[0]


class BackgroundWork:
    """Collects a session's background work, entry by entry.

    Feed it the session's main-chain entries in order: tool calls
    (:meth:`tool_call`), their results (:meth:`tool_result`), the
    notifications (:meth:`notified`) and wake-up fires (:meth:`fired`).
    """

    def __init__(self) -> None:
        self._calls: dict[str, tuple[ToolUseContent, int]] = {}
        self._work: dict[str, _Work] = {}
        self._wakes: list[_Wake] = []

    # ---- feeding ------------------------------------------------------
    def tool_call(self, item: ToolUseContent, at: int) -> None:
        self._calls[item.id] = (item, at)
        data = item.input or {}
        key = _STOPPERS.get(item.name)
        if key is not None:
            self._end(data.get(key), at, "stopped")
        if item.name == "ScheduleWakeup" and data.get("stop") is True:
            self._wakes.clear()

    def tool_result(self, result: ToolResultContent, structured: Any, at: int) -> None:
        """A tool result; ``structured`` is its entry's ``toolUseResult``
        (only when the entry holds this one result: it describes one)."""
        call = self._calls.get(result.tool_use_id)
        if call is None:
            return
        item, called_at = call
        details = _structured(structured)
        text = _result_text(result)
        if item.name == "ScheduleWakeup":
            self._scheduled(item, details, text, called_at)
            return
        kind = _KINDS.get(item.name)
        if kind is None:
            return
        task_id = self._launch_id(kind, details, text)
        if task_id is None:
            return
        data = item.input or {}
        label = (
            _first_line(data.get("description"))
            or _first_line(data.get("command"))
            or item.name
        )
        self._work[task_id] = _Work(
            kind=kind,
            label=label,
            start=called_at,
            expires=self._expiry(kind, data, details, text, called_at),
        )

    def notified(self, text: str, at: int) -> list[Notification]:
        """Apply the notifications in ``text``; return them, so a turn
        they start can say what reported."""
        found = notifications(text)
        for note in found:
            work = self._work.get(note.task_id or "")
            if work is None:
                continue
            if note.status is None:
                if (note.event or "").startswith(_MONITOR_EXPIRED_PREFIXES):
                    self._ended(work, at, "completed")
                continue
            final = _FINAL.get(note.status)
            if final is None or note.interim:
                continue
            self._ended(work, at, final)
        return found

    def fired(self, at: int) -> Optional[str]:
        """A wake-up fired: drop the pending one nearest ``at``, returning
        its label (None when no pending wake-up is near: a cron job)."""
        if not self._wakes:
            return None
        nearest = min(self._wakes, key=lambda w: abs(w.at - at))
        if abs(nearest.at - at) > _FIRE_MATCH_MS:
            return None
        self._wakes.remove(nearest)
        return nearest.label

    # ---- reading ------------------------------------------------------
    def items(self, horizon: int, limit: int) -> list[list[Any]]:
        """``[start, end, kind, status, label, expiresAt]`` for the work
        not over before ``horizon``, oldest first, at most ``limit``."""
        shown = [w for w in self._work.values() if w.end is None or w.end >= horizon]
        shown.sort(key=lambda w: w.start)
        return [
            [w.start, w.end, w.kind, w.status, w.label, w.expires]
            for w in shown[-limit:]
        ]

    def pending_wakes(self, horizon: int) -> list[list[Any]]:
        """``[at, label]`` for the wake-ups yet to fire."""
        return [
            [w.at, w.label]
            for w in sorted(self._wakes, key=lambda w: w.at)
            if w.at >= horizon
        ]

    # ---- helpers ------------------------------------------------------
    @staticmethod
    def _ended(work: _Work, at: int, status: str) -> None:
        """Record an end. One notification can be on record more than
        once (queued, then delivered): the earliest record of the same
        end wins. A different end (an agent resumed and finished again)
        replaces it."""
        if work.end is not None and work.status == status and work.end <= at:
            return
        work.end, work.status = at, status

    def _end(self, task_id: Any, at: int, status: str) -> None:
        work = self._work.get(task_id) if isinstance(task_id, str) else None
        if work is not None and work.end is None:
            work.end, work.status = at, status

    @staticmethod
    def _launch_id(kind: str, details: dict[str, Any], text: str) -> Optional[str]:
        structured = {
            "bash": details.get("backgroundTaskId"),
            "agent": (
                details.get("agentId")
                if details.get("status") == "async_launched"
                or details.get("isAsync") is True
                else None
            ),
            "monitor": details.get("taskId"),
        }[kind]
        if isinstance(structured, str) and structured:
            return structured
        for pattern in _LAUNCH_ID_RES[kind]:
            match = pattern.search(text)
            if match:
                return match.group("id")
        return None

    @staticmethod
    def _expiry(
        kind: str,
        data: dict[str, Any],
        details: dict[str, Any],
        text: str,
        start: int,
    ) -> Optional[int]:
        """When a monitor stops by itself (None: persistent, or not one)."""
        if kind != "monitor" or details.get("persistent") is True:
            return None
        timeout = _int(details.get("timeoutMs")) or _int(data.get("timeout_ms"))
        if timeout is None:
            match = _MONITOR_TIMEOUT_RE.search(text)
            if match:
                timeout = int(match.group("ms"))
            elif match := _MONITOR_EXPIRES_RE.search(text):
                timeout = int(match.group("min")) * 60000
        return start + timeout if timeout else None

    def _scheduled(
        self,
        item: ToolUseContent,
        details: dict[str, Any],
        text: str,
        called_at: int,
    ) -> None:
        if details.get("stopped") is True:
            self._wakes.clear()
            return
        at = _int(details.get("scheduledFor"))
        if not at:
            match = _WAKE_IN_RE.search(text)
            at = called_at + int(match.group("s")) * 1000 if match else None
        if not at:
            return
        data = item.input or {}
        label = (
            _first_line(data.get("reason"))
            or _first_line(data.get("prompt"))
            or "wake-up"
        )
        self._wakes.append(_Wake(at=at, label=label))
