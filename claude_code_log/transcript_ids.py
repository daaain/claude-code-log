"""Validation of the identifiers a transcript carries.

A transcript's ``sessionId`` and ``agentId`` are untrusted input, yet they
end up as file names (``session-<id>.html``, ``<id>.jsonl``,
``agent-<id>.jsonl``), as an argument to ``claude --resume``, in links, and
in Textual markup. Claude Code writes UUIDs and short hex ids, so every
legitimate id fits a small charset. An id that doesn't is replaced, where the
entry is read, by a *quarantined* stand-in: a stable hash of the original,
so the bad session's entries still group together and render, while nothing
downstream ever sees the original string.

``#agent-<id>`` suffixes are the loader's own stamping of spliced sub-agent
lines (``{trunk}#agent-{id}``, see dev-docs/agents.md); they reach the cache
and come back through :func:`sanitize_entry_ids`, so a session id may carry
them. Anything that names a file or a ``claude`` session must use the plain
form (:func:`is_plain_id`).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional, cast

# One id: what Claude Code writes (UUIDs, hex agent ids), with room for the
# synthetic ids tests and other providers use. No separator, no leading
# ``-`` or ``.``, bounded so ``session-<id>.html`` stays a legal file name.
_PLAIN_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
PLAIN_ID_RE = re.compile(_PLAIN_ID)
SESSION_ID_RE = re.compile(rf"{_PLAIN_ID}(?:#agent-{_PLAIN_ID})*")

QUARANTINE_PREFIX = "quarantined-"


def is_plain_id(value: object) -> bool:
    """True when ``value`` is safe as a file name part and a CLI argument."""
    return isinstance(value, str) and PLAIN_ID_RE.fullmatch(value) is not None


def is_session_id(value: object) -> bool:
    """True when ``value`` is a valid (possibly agent-stamped) session id."""
    return isinstance(value, str) and SESSION_ID_RE.fullmatch(value) is not None


def quarantined_id(raw: object) -> str:
    """The stand-in for an invalid id: stable, so one bad id maps to one id."""
    data = repr(raw).encode("utf-8", "surrogatepass")
    return QUARANTINE_PREFIX + hashlib.sha256(data).hexdigest()[:16]


def is_quarantined(session_id: Optional[str]) -> bool:
    return session_id is not None and session_id.startswith(QUARANTINE_PREFIX)


def sanitize_entry_ids(entry: dict[str, Any]) -> list[str]:
    """Replace invalid ids in a raw transcript entry, in place.

    Covers ``sessionId``, ``agentId``, ``spawnedAgentId`` and
    ``toolUseResult.agentId``. Returns the names of the fields replaced (empty
    for a clean entry), so the caller can warn without echoing the value,
    which may carry terminal escapes. Idempotent: a stand-in is itself valid.
    """
    replaced: list[str] = []
    sid = entry.get("sessionId")
    if sid is not None and not is_session_id(sid):
        entry["sessionId"] = quarantined_id(sid)
        replaced.append("sessionId")
    for key in ("agentId", "spawnedAgentId"):
        value = entry.get(key)
        if value is not None and not is_plain_id(value):
            entry[key] = quarantined_id(value)
            replaced.append(key)
    result: object = entry.get("toolUseResult")
    if isinstance(result, dict):
        result_dict = cast(dict[str, Any], result)
        value = result_dict.get("agentId")
        if value is not None and not is_plain_id(value):
            result_dict["agentId"] = quarantined_id(value)
            replaced.append("toolUseResult.agentId")
    return replaced


def session_path(
    directory: Path, session_id: str, pattern: str = "{}.jsonl"
) -> Optional[Path]:
    """``directory / pattern.format(session_id)``, or None if that could escape.

    For every sink that turns a session id into a file to read, write or
    delete. Ingestion already replaces invalid ids, but rows in a cache built
    elsewhere (or by an older version) never pass through it, so each sink
    checks again. A plain id has no separator and can't start with ``.``, so
    the name stays a single component directly inside ``directory`` (resolving
    the path instead would break symlinked transcripts).
    """
    if not is_plain_id(session_id):
        return None
    return directory / pattern.format(session_id)


def session_file_name(session_id: str, tail: str) -> str:
    """``session-<id><tail>``, for every output page named after a session.

    Raises rather than writing: ingestion has already replaced invalid ids,
    so one arriving here came from a cache that bypassed it, and a name that
    could carry a path separator (``..`` is collapsed lexically on Windows)
    must never reach the file system.
    """
    if not is_session_id(session_id):
        raise ValueError("refusing to name an output file after an invalid session id")
    return f"session-{session_id}{tail}"
