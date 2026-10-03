"""Template helpers for the minimal HTML theme (``--theme minimal``).

The minimal theme lays each message out as a row — a narrow gutter (time,
role label, tokens), a rail with a role-coloured dot, then the content
(see work/minimal-theme-dag.md § 1.2). The gutter needs a few short
strings the classic markup doesn't carry: a one-word role label, a compact
token count and the page's meta line. They are derived here, from data
the template already has, and are called **only** from the template's
minimal branches — so classic output, and the formatter output shared by
both themes (and cached in the fragment store), are untouched.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any, Iterable, Optional, cast

if TYPE_CHECKING:
    from ..renderer import TemplateMessage

# Titles that say nothing the gutter's role label doesn't already say.
# The minimal theme hides the title line for these, so a plain prompt or
# answer is a single row (gutter + text) rather than a heading plus text.
_GENERIC_TITLES = frozenset(
    {"user", "assistant", "thinking", "sub-assistant", "system", "memory"}
)

# Leading pictographs/symbols the template or a formatter prefixes to a
# title ("🤷 User", "🛠️ TodoWrite"): anything up to the first letter/digit.
_LEADING_DECORATION = re.compile(r"^[^\w]+", re.UNICODE)
_TAG = re.compile(r"<[^>]+>")
_TOKEN_PART = re.compile(r"([A-Za-z][A-Za-z ]*?):\s*([\d,]+)")


def role_label(message: "TemplateMessage", css_classes: str) -> str:
    """One-word role label for the gutter, e.g. ``User``, ``Read``, ``Error``.

    ``css_classes`` is the card's class string as the template computed it
    (``css_class_from_message``), so the label follows the same type and
    modifier taxonomy as the card's styling and the filter.
    """
    classes = set(css_classes.split())
    if "tool_use" in classes:
        if "workflow_phase" in classes:
            return "Phase"
        if "workflow_agent" in classes:
            return "Agent"
        name = getattr(message.content, "tool_name", None)
        return str(name) if name else "Tool"
    if "tool_result" in classes:
        return "Error" if "error" in classes else "Result"
    if "thinking" in classes:
        return "Thinking"
    if "assistant" in classes:
        return "Agent" if "sidechain" in classes else "Assistant"
    if "user" in classes:
        for modifier, label in (
            ("task-notification", "Async"),
            ("teammate", "Teammate"),
            ("compacted", "Compacted"),
            ("command-output", "Output"),
            ("slash-command", "Command"),
            ("steering", "Steer"),
        ):
            if modifier in classes:
                return label
        return "User"
    if "bash-input" in classes:
        return "Bash"
    if "bash-output" in classes:
        return "Output"
    if "system" in classes:
        for modifier, label in (
            ("system-error", "Error"),
            ("system-warning", "Warning"),
            ("system-hook", "Hook"),
            ("system-hook-attachment", "Hook"),
            ("system-away-summary", "Recap"),
        ):
            if modifier in classes:
                return label
        return "System"
    if "image" in classes:
        return "Image"
    first = css_classes.split()[0] if css_classes.split() else "Message"
    return first.replace("_", " ").replace("-", " ").capitalize()


def is_generic_title(title: Optional[str]) -> bool:
    """True when a card title is just the role (``🤷 User``, ``💭 Thinking``).

    Such titles are hidden in the minimal theme — the gutter's role label
    says the same thing. Informative titles (a tool's name and summary, an
    async result's description) stay, as the row's first line.
    """
    if not title:
        return True
    text = html.unescape(_TAG.sub("", title)).strip()
    text = _LEADING_DECORATION.sub("", text).strip().lower()
    return not text or text in _GENERIC_TITLES


def compact_count(value: int) -> str:
    """``950`` → ``950``, ``9400`` → ``9.4k``, ``182345`` → ``182k``."""
    if value < 1000:
        return str(value)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if value >= divisor:
            scaled = value / divisor
            text = f"{scaled:.1f}" if scaled < 100 else f"{scaled:.0f}"
            if text.endswith(".0"):
                text = text[:-2]
            return text + suffix
    return str(value)  # pragma: no cover - unreachable


def _token_parts(summary: str) -> dict[str, int]:
    return {
        name.strip().lower(): int(number.replace(",", ""))
        for name, number in _TOKEN_PART.findall(summary)
    }


def _in_out(parts: dict[str, int]) -> tuple[int, int]:
    """Context size in (input + cache creation + cache read), and output."""
    total_in = (
        parts.get("input", 0)
        + parts.get("cache creation", 0)
        + parts.get("cache read", 0)
    )
    return total_in, parts.get("output", 0)


def compact_token_usage(token_usage: Optional[str]) -> str:
    """``Input: 3 | Output: 1400 | Cache Read: 61200`` → ``61.2k · 1.4k``.

    In is the context the turn consumed (input + cache creation + cache
    read), out is the output; the full breakdown stays available as the
    gutter's tooltip.
    """
    if not token_usage:
        return ""
    parts = _token_parts(token_usage)
    if not parts:
        return ""
    total_in, total_out = _in_out(parts)
    return f"{compact_count(total_in)} · {compact_count(total_out)}"


def gutter_time(formatted_timestamp: Optional[str]) -> str:
    """``2025-07-03 15:50:07`` → ``15:50:07`` (the no-JS gutter time).

    ``minimal.js`` replaces it with the viewer's local time on load.
    """
    if not formatted_timestamp:
        return ""
    text = str(formatted_timestamp).strip()
    return text.rsplit(" ", 1)[-1] if " " in text else text


def _real_sessions(sessions: Optional[Iterable[Any]]) -> list[dict[str, Any]]:
    """Session-nav entries that are sessions (not fork/branch/compaction rows)."""
    result: list[dict[str, Any]] = []
    for entry in sessions or []:
        if not isinstance(entry, dict):
            continue
        session = cast(dict[str, Any], entry)
        if (
            session.get("is_fork_point")
            or session.get("is_branch")
            or session.get("is_compaction_point")
        ):
            continue
        result.append(session)
    return result


def page_meta(
    sessions: Optional[Iterable[Any]], page_stats: Optional[dict[str, Any]]
) -> list[str]:
    """Parts of the header's meta line, joined with `` · `` by the template.

    One session: its short id, time range, message count and tokens. Several:
    the session count plus the page's (or the sessions' summed) counts.
    """
    real = _real_sessions(sessions)
    parts: list[str] = []
    if len(real) == 1:
        only = real[0]
        if only.get("id"):
            parts.append(str(only["id"])[:8])
        if only.get("timestamp_range"):
            parts.append(str(only["timestamp_range"]))
    elif len(real) > 1:
        parts.append(f"{len(real)} sessions")

    if page_stats:
        if page_stats.get("date_range") and len(real) != 1:
            parts.append(str(page_stats["date_range"]))
        if page_stats.get("message_count"):
            parts.append(f"{page_stats['message_count']} msgs")
        token_source = str(page_stats.get("token_summary") or "")
    else:
        count = sum(int(s.get("message_count") or 0) for s in real)
        if count:
            parts.append(f"{count} msgs")
        token_source = " | ".join(str(s.get("token_summary") or "") for s in real)

    totals: dict[str, int] = {}
    # A multi-session summary repeats the same labels once per session.
    for name, number in _TOKEN_PART.findall(token_source):
        key = name.strip().lower()
        totals[key] = totals.get(key, 0) + int(number.replace(",", ""))
    total_in, total_out = _in_out(totals)
    if total_in:
        parts.append(f"{compact_count(total_in)} in")
    if total_out:
        parts.append(f"{compact_count(total_out)} out")
    return parts
