"""Gutter icons for the minimal HTML theme (``--theme minimal``).

Each row's gutter names its role (``User``, ``Read``, ``Hook`` …); a small
stroke icon beside that label makes a long page scannable. The glyphs are
original 16×16 line drawings, defined **once per page** as ``<symbol>``\\ s
in an inline sprite (``icon_sprite``, emitted right after ``<body>``,
outside ``#transcript`` so a live update's swap keeps it) and referenced
from each row with ``<svg class='mn-ic'><use href='#mi-…'/></svg>``
(``role_icon_markup``). The icon is decoration: ``aria-hidden``, no text,
so the role label stays what screen readers, search and the filter read.

Styling (``components/minimal/layout.css``): ``fill: none``, stroke in
``currentColor`` — the row's role colour ``--rc`` —, round caps and joins.

Every symbol is emitted, not only the ones a page uses: a live update can
bring a kind the page did not have when it was rendered, and the sprite
is not part of the markup a live update replaces.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from markupsafe import Markup

from ..models import UserMemoryMessage

if TYPE_CHECKING:
    from ..renderer import TemplateMessage

# -- Glyphs --------------------------------------------------------------------
#
# Inner markup of each symbol (viewBox 0 0 16 16). Drawn for a 1.5 stroke
# at 12–13px: whole shapes, few details, nothing closer than ~1.5 units.
# A zero-length segment (``v.01``) with a round cap draws a dot.

GLYPHS: dict[str, str] = {
    # ---- people and conversation
    "user": '<circle cx="8" cy="5" r="2.75"/>'
    '<path d="M2.75 14c0-2.9 2.35-4.75 5.25-4.75s5.25 1.85 5.25 4.75"/>',
    "steer": '<path d="M3 14V9.5A4.5 4.5 0 0 1 7.5 5H13"/>'
    '<path d="M10.25 2.25 13 5l-2.75 2.75"/>',
    "command": '<rect x="2" y="2" width="12" height="12" rx="2.5"/>'
    '<path d="M9.75 4.75 6.25 11.25"/>',
    "output": '<path d="M2.5 3.5h11M2.5 6.5h11M2.5 9.5h7.5M2.5 12.5h4.5"/>',
    "bash-in": '<path d="M2.5 3.75 6.75 8 2.5 12.25M8.75 12.25h4.75"/>',
    "compacted": '<path d="M8 1.5v4.5M5.5 3.5 8 6l2.5-2.5M8 14.5V10'
    'M5.5 12.5 8 10l2.5 2.5M2 8h12"/>',
    "memory": '<path d="M4 2.25h8v11.5l-4-3-4 3z"/>',
    "memory-write": '<path d="M4 2.25h8v11.5l-4-3-4 3z"/><path d="M8 4.75v3.5M6.25 6.5h3.5"/>',
    "teammate": '<circle cx="6" cy="5.25" r="2.25"/>'
    '<path d="M1.75 13.75c0-2.5 1.9-4.25 4.25-4.25s4.25 1.75 4.25 4.25"/>'
    '<path d="M10.5 3.15a2.25 2.25 0 0 1 0 4.2M12.25 9.9c1.3.55 2 1.85 2 3.85"/>',
    "async": '<path d="M4 11.25V7a4 4 0 0 1 8 0v4.25l1.25 1.5H2.75z"/>'
    '<path d="M6.5 14.75h3"/>',
    "assistant": '<path d="M8 1.75c.45 3.3 2.95 5.8 6.25 6.25-3.3.45-5.8 2.95-6.25 6.25'
    '-.45-3.3-2.95-5.8-6.25-6.25C5.05 7.55 7.55 5.05 8 1.75z"/>',
    "agent": '<rect x="2.5" y="5" width="11" height="8.75" rx="2.25"/>'
    '<path d="M8 5V2.5M6 8.5V10M10 8.5V10"/>',
    "thinking": '<path d="M5.5 10.5a3 3 0 0 1-.75-5.9 4 4 0 0 1 7.15-.85 3.4 3.4 0 0 1 .35 6.75z"/>'
    '<circle cx="4.25" cy="12.75" r="1"/><path d="M1.9 14.75v.01"/>',
    # ---- system
    "info": '<circle cx="8" cy="8" r="6.25"/><path d="M8 7.25v4M8 4.75v.01"/>',
    "warning": '<path d="M8 2.25 14.5 13.5h-13z"/><path d="M8 6.75v3M8 11.5v.01"/>',
    "error": '<path d="M5.4 1.75h5.2l3.65 3.65v5.2l-3.65 3.65H5.4L1.75 10.6V5.4z"/>'
    '<path d="M8 4.75v3.75M8 11.25v.01"/>',
    "hook": '<circle cx="10.5" cy="3.25" r="1.5"/>'
    '<path d="M10.5 4.75V10a3.5 3.5 0 0 1-7 0V8l2.25 2.25"/>',
    "recap": '<path d="M2.75 8a5.25 5.25 0 1 0 1.54-3.71"/>'
    '<path d="M4.25 1.75v2.5h2.5M8 5.25V8l2 1.25"/>',
    "image": '<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.75"/>'
    '<circle cx="10.25" cy="6.25" r="1.25"/>'
    '<path d="M2.25 12.25 6 8.5l3.25 3.25 1.5-1.5 3 3"/>',
    "branch": '<circle cx="4.5" cy="3.5" r="1.75"/><circle cx="4.5" cy="12.5" r="1.75"/>'
    '<circle cx="11.5" cy="3.5" r="1.75"/>'
    '<path d="M4.5 5.25v5.5M11.5 5.25c0 3.75-7 2.5-7 5.5"/>',
    # ---- tool calls and results
    "result": '<path d="M13 2.5v5A2.5 2.5 0 0 1 10.5 10H3"/><path d="M6 7 3 10l3 3"/>',
    "tool-error": '<circle cx="8" cy="8" r="6.25"/><path d="M5.75 5.75l4.5 4.5M10.25 5.75l-4.5 4.5"/>',
    "tool": '<path d="M10.1 2a3.6 3.6 0 0 0-3.55 4.75L2.3 11a1.6 1.6 0 0 0 2.3 2.25l4.2-4.2'
    'a3.6 3.6 0 0 0 4.75-3.55l-2 2-2.2-.35-.35-2.2z"/>',
    "read": '<path d="M1.5 8S4 3.25 8 3.25 14.5 8 14.5 8 12 12.75 8 12.75 1.5 8 1.5 8z"/>'
    '<circle cx="8" cy="8" r="2"/>',
    "write": '<path d="M9.5 1.75H4.25a1 1 0 0 0-1 1v10.5a1 1 0 0 0 1 1h7.5a1 1 0 0 0 1-1V5z"/>'
    '<path d="M9.5 1.75V5h3.25M8 7.5v4.5M5.75 9.75h4.5"/>',
    "edit": '<path d="M10.5 2.5a1.75 1.75 0 0 1 2.5 0l.5.5a1.75 1.75 0 0 1 0 2.5L5.5 13.5 2.25 14'
    'l.5-3.25z"/><path d="M9.25 3.75 12.25 6.75"/>',
    "multiedit": '<path d="M1.75 3h5M1.75 6h3"/>'
    '<path d="M11.25 4a1.6 1.6 0 0 1 2.25 0l.5.5a1.6 1.6 0 0 1 0 2.25L7.25 13.5 4.25 14'
    'l.5-3z"/><path d="M1.75 13.75h1"/>',
    "delete": '<path d="M2.25 4.25h11.5M6 4.25V2.25h4v2M3.75 4.25l.75 9.5h7l.75-9.5'
    'M6.75 7v4M9.25 7v4"/>',
    "bash": '<rect x="1.75" y="2.5" width="12.5" height="11" rx="2"/>'
    '<path d="M4.5 6.25 6.75 8.5 4.5 10.75M8.5 10.75h3"/>',
    "glob": '<path d="M8 2v12M2.8 5l10.4 6M2.8 11l10.4-6"/>',
    "grep": '<circle cx="7" cy="7" r="4.75"/><path d="M10.5 10.5 14.25 14.25"/>',
    "websearch": '<circle cx="6.75" cy="6.75" r="5"/>'
    '<path d="M1.75 6.75h10M6.75 1.75c-1.75 1.6-1.75 8.4 0 10M6.75 1.75c1.75 1.6 1.75 8.4 0 10'
    'M10.5 10.5l3.75 3.75"/>',
    "webfetch": '<circle cx="8" cy="8" r="6.25"/>'
    '<path d="M1.75 8h12.5M8 1.75c-2.2 2-2.2 10.5 0 12.5M8 1.75c2.2 2 2.2 10.5 0 12.5"/>',
    "taskoutput": '<path d="M1.75 9.25h3.5l1 2h3.5l1-2h3.5"/>'
    '<path d="M3.75 3h8.5l2 6.25V13a1 1 0 0 1-1 1H2.75a1 1 0 0 1-1-1V9.25z"/>',
    "taskstop": '<circle cx="8" cy="8" r="6.25"/>'
    '<rect x="5.75" y="5.75" width="4.5" height="4.5" rx=".5"/>',
    "todo": '<path d="M1.75 4.5 3.25 6 5.75 3M1.75 10.5 3.25 12l2.5-3M8.25 4.75h6M8.25 10.75h6"/>',
    "ask": '<path d="M2.75 2.5h10.5a1.25 1.25 0 0 1 1.25 1.25v6.5a1.25 1.25 0 0 1-1.25 1.25H7.5'
    'L4.5 14v-2.5H2.75a1.25 1.25 0 0 1-1.25-1.25v-6.5A1.25 1.25 0 0 1 2.75 2.5z"/>'
    '<path d="M6.5 5.4a1.5 1.5 0 1 1 2.1 1.4c-.4.2-.6.55-.6 1v.2M8 9.6v.01"/>',
    "plan": '<path d="M5.5 2.75H4.25A1.25 1.25 0 0 0 3 4v9.25a1.25 1.25 0 0 0 1.25 1.25h7.5'
    'A1.25 1.25 0 0 0 13 13.25V4a1.25 1.25 0 0 0-1.25-1.25H10.5"/>'
    '<rect x="5.5" y="1.5" width="5" height="2.5" rx=".75"/>'
    '<path d="M5.5 9.25 7.25 11l3.25-3.5"/>',
    "skill": '<path d="M9.25 1.5 3.5 9h4.25l-1 5.5L12.5 7H8.25z"/>',
    "artifact": '<rect x="1.75" y="2.5" width="12.5" height="11" rx="2"/>'
    '<path d="M1.75 5.75h12.5M6.25 11.25 9.75 7.75M6.75 7.75h3v3"/>',
    "monitor": '<path d="M1.5 8.5h2.75l1.75-4.25 3 8.5 1.75-4.25h3.75"/>',
    "wakeup": '<circle cx="8" cy="8.75" r="5.25"/>'
    '<path d="M8 6.25v2.5l1.75 1.25M1.75 4.25 4 2.25M14.25 4.25 12 2.25"/>',
    "cron": '<rect x="2" y="3" width="12" height="11" rx="1.75"/>'
    '<path d="M2 6.75h12M5.25 1.75v2.5M10.75 1.75v2.5M5.5 10.25h1M9.5 10.25h1"/>',
    "taskcreate": '<rect x="2.25" y="2.25" width="11.5" height="11.5" rx="2.5"/>'
    '<path d="M8 5.25v5.5M5.25 8h5.5"/>',
    "taskupdate": '<rect x="2.25" y="2.25" width="11.5" height="11.5" rx="2.5"/>'
    '<path d="M5.25 8.25 7.25 10.25 10.75 6"/>',
    "tasklist": '<path d="M6 4h8M6 8h8M6 12h8M2 4h1M2 8h1M2 12h1"/>',
    "send": '<path d="M14.25 1.75 7 9M14.25 1.75 9.75 14.25 7 9 1.75 6.25z"/>',
    "workflow": '<rect x="1.75" y="1.75" width="5.5" height="4.5" rx="1"/>'
    '<rect x="8.75" y="9.75" width="5.5" height="4.5" rx="1"/>'
    '<path d="M4.5 6.25v3.5a2 2 0 0 0 2 2h2.25"/>',
    "phase": '<path d="M3.5 14.5V1.75"/><path d="M3.5 2.5h9l-2.25 3.25L12.5 9h-9"/>',
    "code": '<path d="M5 4.25 1.75 8 5 11.75M11 4.25 14.25 8 11 11.75M9.25 2.75l-2.5 10.5"/>',
    "wait": '<path d="M4 1.75h8M4 14.25h8M5 1.75v2.5C5 6 8 6.5 8 8s-3 2-3 3.75v2.5'
    'M11 1.75v2.5C11 6 8 6.5 8 8s3 2 3 3.75v2.5"/>',
}

# Names, as the icon legend (docs) shows them.
GLYPH_NAMES: dict[str, str] = {
    "user": "User prompt",
    "steer": "Steering",
    "command": "Slash command",
    "output": "Command / Bash output",
    "bash-in": "Bash input (!)",
    "compacted": "Compacted",
    "memory": "Memory (recall)",
    "memory-write": "Memory (save)",
    "teammate": "Teammate / Team*",
    "async": "Async result",
    "assistant": "Assistant",
    "agent": "Agent / Task",
    "thinking": "Thinking",
    "info": "System",
    "warning": "Warning",
    "error": "System error",
    "hook": "Hook",
    "recap": "Recap",
    "image": "Image",
    "branch": "Branch",
    "result": "Tool result",
    "tool-error": "Tool error",
    "tool": "MCP / other tool",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "multiedit": "MultiEdit",
    "delete": "Delete",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "websearch": "WebSearch",
    "webfetch": "WebFetch",
    "taskoutput": "TaskOutput",
    "taskstop": "TaskStop",
    "todo": "TodoWrite",
    "ask": "AskUserQuestion",
    "plan": "ExitPlanMode",
    "skill": "Skill",
    "artifact": "Artifact",
    "monitor": "Monitor",
    "wakeup": "ScheduleWakeup",
    "cron": "Cron*",
    "taskcreate": "TaskCreate",
    "taskupdate": "TaskUpdate",
    "tasklist": "TaskList",
    "send": "SendMessage",
    "workflow": "Workflow",
    "phase": "Workflow phase",
    "code": "ToolExecution",
    "wait": "Wait",
}

# Tool name → glyph. Anything else (``mcp__…``, plugin tools, built-ins
# without a renderer) is the generic ``tool``.
TOOL_ICONS: dict[str, str] = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "multiedit",
    "NotebookEdit": "edit",
    "Delete": "delete",
    "Bash": "bash",
    "PowerShell": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "WebSearch": "websearch",
    "WebFetch": "webfetch",
    "Task": "agent",
    "Agent": "agent",
    "TaskOutput": "taskoutput",
    "TaskStop": "taskstop",
    "TodoWrite": "todo",
    "AskUserQuestion": "ask",
    "ask_user_question": "ask",
    "ExitPlanMode": "plan",
    "EnterPlanMode": "plan",
    "Skill": "skill",
    "Artifact": "artifact",
    "Monitor": "monitor",
    "ScheduleWakeup": "wakeup",
    "CronCreate": "cron",
    "CronList": "cron",
    "CronDelete": "cron",
    "TeamCreate": "teammate",
    "TeamDelete": "teammate",
    "TaskCreate": "taskcreate",
    "TaskUpdate": "taskupdate",
    "TaskList": "tasklist",
    "SendMessage": "send",
    "Workflow": "workflow",
    "ToolExecution": "code",
    "wait": "wait",  # Codex's synthetic wait pairs (providers/codex.py)
}

# A Read / Write / Edit on a file under memory/ (the `memory` modifier).
_MEMORY_TOOL_ICONS = {
    "Read": "memory",
    "Write": "memory-write",
    "Edit": "memory-write",
    "MultiEdit": "memory-write",
}

# User-entry modifiers, in role_label's order.
_USER_ICONS = (
    ("task-notification", "async"),
    ("teammate", "teammate"),
    ("compacted", "compacted"),
    ("command-output", "output"),
    ("slash-command", "command"),
    ("steering", "steer"),
)

_SYSTEM_ICONS = (
    ("system-error", "error"),
    ("system-warning", "warning"),
    ("system-hook", "hook"),
    ("system-hook-attachment", "hook"),
    ("system-away-summary", "recap"),
)


def tool_icon(tool_name: Any, memory: bool = False) -> str:
    """Glyph of a tool call: its own, a memory variant, else ``tool``."""
    name = str(tool_name or "")
    if memory and name in _MEMORY_TOOL_ICONS:
        return _MEMORY_TOOL_ICONS[name]
    return TOOL_ICONS.get(name, "tool")


def role_icon(message: "TemplateMessage", css_classes: str) -> str:
    """Glyph name of a card, following ``minimal_theme.role_label``'s
    taxonomy (the card's class string, as the template computed it)."""
    classes = set(css_classes.split())
    content = message.content
    if "tool_use" in classes:
        if "workflow_phase" in classes:
            return "phase"
        if "workflow_agent" in classes:
            return "agent"
        return tool_icon(getattr(content, "tool_name", None), "memory" in classes)
    if "tool_result" in classes:
        return "tool-error" if "error" in classes else "result"
    if "thinking" in classes:
        return "thinking"
    if "assistant" in classes:
        return "agent" if "sidechain" in classes else "assistant"
    if "user" in classes:
        for modifier, icon in _USER_ICONS:
            if modifier in classes:
                return icon
        if isinstance(content, UserMemoryMessage):
            return "memory-write"
        return "user"
    if "bash-input" in classes:
        return "bash-in"
    if "bash-output" in classes:
        return "output"
    if "system" in classes:
        for modifier, icon in _SYSTEM_ICONS:
            if modifier in classes:
                return icon
        return "info"
    if "image" in classes:
        return "image"
    return "info"


def icon_markup(name: str) -> Markup:
    """``<svg class='mn-ic'><use href='#mi-NAME'/></svg>``, decoration only."""
    if name not in GLYPHS:
        name = "tool"
    return Markup(
        f"<svg class='mn-ic' aria-hidden='true'><use href='#mi-{name}'/></svg>"
    )


def role_icon_markup(message: "TemplateMessage", css_classes: str) -> Markup:
    """The card's gutter icon (the ``mn_role_icon`` Jinja global)."""
    return icon_markup(role_icon(message, css_classes))


@lru_cache(maxsize=1)
def icon_sprite() -> Markup:
    """The page's sprite: every glyph as a ``<symbol id='mi-…'>``, once.

    Zero-size and out of flow (not ``display: none``, under which some
    engines drop what a ``<use>`` references); ``aria-hidden``.
    """
    symbols = "".join(
        f'<symbol id="mi-{name}" viewBox="0 0 16 16">{body}</symbol>'
        for name, body in GLYPHS.items()
    )
    return Markup(
        "<svg class='mn-sprite' xmlns='http://www.w3.org/2000/svg' width='0' height='0'"
        f" aria-hidden='true' focusable='false'>{symbols}</svg>"
    )
