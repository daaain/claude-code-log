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
from datetime import datetime, timezone
import unicodedata
from typing import TYPE_CHECKING, Any, Iterable, Optional, cast

# Re-exported: the gutter token counts and the lane stats share it.
from markupsafe import Markup

from ..utils import compact_count as compact_count

if TYPE_CHECKING:
    from ..lanes import CrossLinks, LaneModel
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


# Leading text of a title that only repeats the gutter's role label, beyond
# the tool name / role label themselves: "📝 Todo List" (TodoWrite),
# "🛠️ Task #1 …" (TaskCreate), "🔄 Async result …", "🤷 Slash Command".
_TITLE_ALIASES = (
    "Todo List",
    "Async result",
    "Slash Command",
    "Sub-assistant",
    "Teammate",
    "Error",
    "Task",
)
_AFTER_NAME = re.compile(r"[\s·:]*")
_SPACES = re.compile(r"\s+")
# Unicode categories of the pictographs a title starts with (emoji, their
# variation selector and joiner, arrows such as ↳). ASCII symbols ("/", "$",
# "#") are content, never decoration.
_DECORATION_CATEGORIES = frozenset({"So", "Sk", "Sm", "Mn", "Me", "Cf"})


def _decoration_length(text: str) -> int:
    """Length of the leading run of pictographs and whitespace."""
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if ord(char) > 0x7F and unicodedata.category(char) in _DECORATION_CATEGORIES:
            continue
        return index
    return len(text)


def _plain(fragment: str) -> str:
    return _SPACES.sub(" ", html.unescape(_TAG.sub("", fragment))).strip()


def call_title(
    full_title: Any,
    message: "TemplateMessage",
    css_classes: str,
    title_hint: Optional[str] = None,
) -> dict[str, Any]:
    """Split a card title into what the minimal row shows and what it hides.

    The gutter already names the role (``Edit``, ``Hook``, ``User``), so the
    row's call line drops the title's leading pictograph and a leading
    repeat of that name — ``📝 Edit <span class='tool-summary'>/tmp/x.py``
    shows just the path (mockup ``.call``, whose ``.tn`` is hidden).

    Returns ``prefix`` (HTML the template keeps in a hidden ``.mn-tn`` span,
    so the title's text content — which search and the timeline read — is
    unchanged), ``rest`` (HTML shown), ``generic`` (nothing left to show: the
    title line is hidden), ``tooltip`` (the whole title as plain text, plus
    the classic ``title_hint`` such as ``ID: toolu_…``) and ``role_tooltip``
    (the same text when the hidden title said more than a bare role: the
    gutter's role label is then the only place the name shows, ellipsized
    when long, so it carries the tooltip instead; empty otherwise).
    """
    full = str(full_title or "")
    cut = full.find("<")
    lead, tail = (full, "") if cut < 0 else (full[:cut], full[cut:])
    lead_text = html.unescape(lead)

    start = _decoration_length(lead_text)
    after = lead_text[start:]
    names: set[str] = {role_label(message, css_classes), *_TITLE_ALIASES}
    tool_name = getattr(message.content, "tool_name", None)
    if tool_name:
        names.add(str(tool_name))
    for name in sorted(names, key=lambda n: len(n), reverse=True):
        if not after.startswith(name):
            continue
        following = after[len(name) : len(name) + 1]
        if following and not (following.isspace() or following in "·:"):
            continue  # "Tasks…" is not the name "Task"
        separators = _AFTER_NAME.match(after, len(name))
        start += separators.end() if separators else len(name)
        break

    prefix_text, rest_text = lead_text[:start], lead_text[start:]
    rest = html.escape(rest_text, quote=False) + tail
    plain = _plain(full)
    tooltip = f"{plain} · {title_hint}" if plain and title_hint else plain or ""
    bare_role = is_generic_title(full)
    generic = not _plain(rest) or bare_role
    return {
        "prefix": html.escape(prefix_text, quote=False),
        "rest": rest,
        "generic": generic,
        "tooltip": tooltip or (title_hint or ""),
        "role_tooltip": (tooltip or title_hint or "")
        if generic and not bare_role
        else "",
    }


def session_header(content: Any, formatted: Any) -> str:
    """The minimal session header's title block (non-branch headers).

    Classic renders ``Session: <summary> • <id8>`` as one bold line. The
    minimal header shows the summary on its own (clamped to two lines by CSS;
    the full text is its ``title`` and ``minimal.js`` expands it on click) and
    the short session id as dim mono metadata beside the model. The rest of
    the formatter's output (the "continues from" back-link, the team badge)
    is kept as it is.
    """
    text = str(formatted or "")
    title = getattr(content, "title", None) or ""
    session_id = str(getattr(content, "session_id", "") or "")
    summary = getattr(content, "summary", None)
    escaped_title = html.escape(title)
    short_id = html.escape(session_id[:8])
    id_span = (
        f"<span class='mn-sh-id' title='Session {html.escape(session_id)}'>"
        f"{short_id}</span>"
    )
    if summary:
        escaped_summary = html.escape(summary)
        block = (
            f"<span class='mn-sh-sum' title='{escaped_summary}'>"
            f"{escaped_summary}</span>{id_span}"
        )
    else:
        block = id_span
    if escaped_title and escaped_title in text:
        return text.replace(escaped_title, block, 1)
    return block + text


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


# -- Branch lanes (P5) --------------------------------------------------------
#
# The lane model (``lanes.annotate_lanes``) becomes ``data-*`` attributes on
# the cards, read by the DAG engine (P6/P7). Attributes, not a JSON island:
# ``live_update.js`` patches *cards* in place (a changed card's own markup is
# swapped), so per-card attributes stay current through a patch, whereas a
# separate data block would only refresh on a wholesale swap. Schema:
# work/minimal-theme-dag.md, P5 "As built".

LaneAttrs = dict[int, list[tuple[str, str]]]


def lane_attributes(model: "LaneModel") -> LaneAttrs:
    """``message_index`` → the extra ``data-*`` attributes of that card.

    ``data-lane`` itself is not in here: every card carries it, straight
    from ``TemplateMessage.lane_id`` (see ``lane_attrs``).
    """
    attrs: LaneAttrs = {}
    spawns: dict[int, list[str]] = {}
    merges: dict[int, list[str]] = {}

    def add(index: Optional[int], name: str, value: str) -> None:
        if index is not None:
            attrs.setdefault(index, []).append((name, value))

    for lane in model.branches:
        if lane.spawn_index is not None:
            spawns.setdefault(lane.spawn_index, []).append(lane.lane_id)
        if lane.merge_index is not None:
            merges.setdefault(lane.merge_index, []).append(lane.lane_id)
    for index, lane_ids in spawns.items():
        add(index, "data-spawns", " ".join(lane_ids))
    for index, lane_ids in merges.items():
        add(index, "data-merges", " ".join(lane_ids))

    heads: set[int] = set()
    for lane in model.branches:
        head = lane.head_index
        if head is None or head in heads:
            continue
        heads.add(head)
        add(head, "data-lane-id", lane.lane_id)
        add(head, "data-lane-kind", lane.kind)
        add(head, "data-lane-name", lane.name)
        add(head, "data-lane-tag", lane.tag)
        if lane.meta:
            add(head, "data-lane-meta", lane.meta)
        add(head, "data-lane-parent", lane.parent_lane)
        add(head, "data-lane-depth", str(lane.depth))
        if lane.spawn_index is not None:
            add(head, "data-lane-from", f"d-{lane.spawn_index}")
        if lane.merge_index is not None:
            add(head, "data-lane-to", f"d-{lane.merge_index}")
        if lane.turn_index is not None:
            add(head, "data-lane-turn", f"d-{lane.turn_index}")
        if lane.group:
            add(head, "data-lane-group", lane.group)
        add(head, "data-lane-rank", str(lane.rank))
        add(head, "data-lane-stats", lane.stats)
        if lane.state:
            add(head, "data-lane-state", lane.state)
        if lane.first_ts:
            add(
                head, "data-lane-ts", f"{lane.first_ts} {lane.last_ts or lane.first_ts}"
            )

    for header, (lane_id, fork_point) in model.continuations.items():
        add(header, "data-lane-continues", lane_id)
        if fork_point is not None:
            add(header, "data-lane-from", f"d-{fork_point}")

    for lane in model.teammates:
        if lane.spawn_index is None or lane.first_index is None:
            continue
        add(lane.spawn_index, "data-teammate-link", f"d-{lane.first_index}")
        add(lane.spawn_index, "data-teammate-name", lane.name)
    return attrs


def lane_attrs(message: "TemplateMessage", attrs: Optional[LaneAttrs]) -> Markup:
    """The card's lane attributes as markup (`` data-lane="main" …``)."""
    parts = [("data-lane", message.lane_id)]
    if attrs and message.message_index is not None:
        parts.extend(attrs.get(message.message_index, ()))
    return Markup(
        "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in parts)
    )


def cross_links(message: "TemplateMessage", links: Optional["CrossLinks"]) -> Markup:
    """Same-page links of a card (teammate anchors, ``lanes.teammate_links``).

    One ``<a href='#msg-d-N'>`` per link in a ``.mn-xlinks`` row under the
    content. The label is generated content (``data-label``) so search and
    the timeline never index it; ``aria-label`` names the link. Works
    without JavaScript (a plain anchor); with it, the page's hash handler
    unfolds the target's ancestors and the DAG engine opens its lane.
    """
    if not links or message.message_index is None:
        return Markup("")
    found = links.get(message.message_index)
    if not found:
        return Markup("")
    anchors = "".join(
        f"<a class='mn-xlink' href='#msg-d-{target}'"
        f' data-label="{html.escape(label, quote=True)}"'
        f' aria-label="{html.escape(label.lstrip("→← "), quote=True)}"></a>'
        for target, label in found
    )
    return Markup(f"<div class='mn-xlinks'>{anchors}</div>")


# -- Project index and archive search pages -----------------------------------
#
# ``index.html`` and ``archive_search.html`` in the minimal theme: a compact
# list of projects with dim mono metadata, each project's sessions as rows
# in the transcript's row language (time gutter, rail dot, content). The
# dates rendered here are UTC, correct without JavaScript; ``pages.js``
# localises every element carrying ``data-mn-from`` / ``data-mn-ts``.

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?")


def _iso_parts(timestamp: Any) -> tuple[str, str]:
    """``2025-01-15T10:00:00Z`` → (``2025-01-15``, ``10:00``) — UTC, no JS."""
    match = _ISO_DATE.match(str(timestamp or "").strip())
    if not match:
        return "", ""
    return match.group(1), match.group(2) or ""


def date_span(earliest: Any, latest: Any) -> str:
    """``2025-01-01 – 2025-01-15`` (one date when both fall on the same day)."""
    first, _ = _iso_parts(earliest)
    last, _ = _iso_parts(latest)
    if first and last and first != last:
        return f"{first} – {last}"
    return last or first


def when(earliest: Any, latest: Any) -> Optional[dict[str, str]]:
    """A date range as the template renders it: ``start`` / ``end`` (raw
    timestamps for ``data-mn-from`` / ``data-mn-to``; ``end`` empty for a
    single moment) and ``text``, the UTC ``date_span``."""
    start = str(earliest or latest or "")
    end = str(latest or "")
    if not start:
        return None
    return {
        "start": start,
        "end": end if end and end != start else "",
        "text": date_span(start, end or start),
    }


def project_when(project: Any) -> Optional[dict[str, str]]:
    """A project's date range; its files' last modification without one
    (the classic card's fallback, there in local time)."""
    found = when(project.earliest_timestamp, project.latest_timestamp)
    if found is None and project.last_modified:
        modified = datetime.fromtimestamp(float(project.last_modified), tz=timezone.utc)
        found = when(modified.strftime("%Y-%m-%dT%H:%M:%SZ"), None)
    return found


def token_totals(
    input_tokens: int, output_tokens: int, cache_creation: int, cache_read: int
) -> list[str]:
    """``["1.2M in", "48k out"]``: context consumed (input + cache) and output,
    as the transcript header's meta line counts them (``page_meta``)."""
    total_in = int(input_tokens or 0) + int(cache_creation or 0) + int(cache_read or 0)
    parts: list[str] = []
    if total_in:
        parts.append(f"{compact_count(total_in)} in")
    if output_tokens:
        parts.append(f"{compact_count(int(output_tokens))} out")
    return parts


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def index_summary_meta(summary: Any) -> list[str]:
    """The index header's meta line: projects, files, messages, tokens."""
    parts = [
        _plural(int(summary.total_projects), "project"),
        _plural(int(summary.total_jsonl), "file"),
        f"{int(summary.total_messages):,} msgs",
    ]
    parts.extend(
        token_totals(
            summary.total_input_tokens,
            summary.total_output_tokens,
            summary.total_cache_creation_tokens,
            summary.total_cache_read_tokens,
        )
    )
    return parts


def project_meta(project: Any) -> list[str]:
    """A project row's dim metadata: sessions (else files), messages, tokens.

    The date range is not in here: the template renders it as its own
    element so ``pages.js`` can localise it.
    """
    sessions = len(project.sessions or [])
    parts = [
        _plural(sessions, "session")
        if sessions
        else _plural(int(project.jsonl_count), "file"),
        f"{int(project.message_count):,} msgs",
    ]
    parts.extend(
        token_totals(
            project.total_input_tokens,
            project.total_output_tokens,
            project.total_cache_creation_tokens,
            project.total_cache_read_tokens,
        )
    )
    return parts


def project_tooltip(project: Any) -> str:
    """The classic card's full figures, for the row's ``title``."""
    parts = [
        _plural(int(project.jsonl_count), "transcript file"),
        _plural(int(project.message_count), "message"),
    ]
    if project.token_summary:
        parts.append(str(project.token_summary))
    if project.formatted_time_range:
        parts.append(f"{project.formatted_time_range} (UTC)")
    return " · ".join(parts)


def index_sessions(project: Any) -> list[dict[str, Any]]:
    """A project's sessions as index rows, newest first.

    Each row: ``href`` (the same link the classic session navigation
    builds), ``title`` (summary, may be empty), ``preview`` (first prompt),
    ``short_id``, ``meta`` parts, and the UTC ``date`` / ``time`` of its
    first message with the raw ``timestamp`` for ``pages.js``.
    """
    rows: list[dict[str, Any]] = []
    for entry in _real_sessions(project.sessions):
        session_id = str(entry.get("id") or "")
        first = entry.get("first_timestamp") or ""
        last = entry.get("last_timestamp") or ""
        date, time = _iso_parts(first)
        meta = [session_id[:8]] if session_id else []
        count = entry.get("message_count")
        if count:
            meta.append(_plural(int(count), "msg"))
        tokens = compact_token_usage(entry.get("token_summary"))
        if tokens:
            meta.append(tokens)
        rows.append(
            {
                "href": entry.get("file")
                or f"{project.name}/session-{session_id}.html",
                "title": str(entry.get("summary") or ""),
                "preview": str(entry.get("first_user_message") or ""),
                "short_id": session_id[:8],
                "meta": meta,
                "timestamp": str(first),
                "timestamp_end": str(last) if last and last != first else "",
                "range": str(entry.get("timestamp_range") or ""),
                "date": date,
                "time": time,
            }
        )
    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return rows
