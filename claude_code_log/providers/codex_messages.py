"""Formatting for structured user-side context emitted by Codex."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
import re


def format_codex_user_message(text: str) -> str:
    """Render a complete ``environment_context`` envelope as Markdown.

    The context is delivered through the user-message channel, so leaving its
    XML untouched produces a large wall of tags.  Parse only the exact outer
    envelope; malformed XML and all ordinary user text pass through verbatim.
    """
    stripped = text.strip()
    if not stripped.startswith("<environment_context"):
        return text
    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        return text
    if root.tag != "environment_context":
        return text

    rows: list[tuple[str, str]] = []
    for tag, label in (
        ("cwd", "Working directory"),
        ("shell", "Shell"),
        ("current_date", "Current date"),
        ("timezone", "Timezone"),
    ):
        value = root.findtext(tag)
        if value:
            rows.append((label, value))

    workspace_roots = [
        value
        for node in root.findall("./filesystem/workspace_roots/root")
        if (value := _node_text(node))
    ]

    profile = root.find("./filesystem/permission_profile")
    permission_rows: list[tuple[str, str]] = []
    profile_type = profile.get("type") if profile is not None else None
    file_system = (
        profile.find("file_system") if profile is not None else None
    )
    file_system_type = file_system.get("type") if file_system is not None else None
    if file_system is not None:
        for entry in file_system.findall("entry"):
            access = entry.get("access", "unspecified")
            path = entry.findtext("path")
            special = entry.findtext("special")
            if path:
                permission_rows.append((access, path))
            elif special:
                separator = "" if special.startswith(":") else ":"
                permission_rows.append((access, f"special{separator}{special}"))

    if not (rows or workspace_roots or permission_rows or profile_type):
        return text

    parts = ["### Environment context"]
    if rows:
        parts.extend(["", "| Setting | Value |", "|---|---|"])
        parts.extend(
            f"| {_escape_cell(label)} | {_inline_code(value)} |"
            for label, value in rows
        )

    if workspace_roots:
        parts.extend(["", "#### Workspace roots", ""])
        parts.extend(f"- {_inline_code(path)}" for path in workspace_roots)

    if permission_rows or profile_type or file_system_type:
        parts.extend(["", "#### Filesystem permissions", ""])
        qualifiers: list[str] = []
        if profile_type:
            qualifiers.append(f"profile {_inline_code(profile_type)}")
        if file_system_type:
            qualifiers.append(f"filesystem {_inline_code(file_system_type)}")
        if qualifiers:
            parts.extend(["; ".join(qualifiers).capitalize() + ".", ""])
        if permission_rows:
            parts.extend(["| Access | Target |", "|---|---|"])
            parts.extend(
                f"| {_inline_code(access)} | {_inline_code(target)} |"
                for access, target in permission_rows
            )

    return "\n".join(parts)


def _node_text(node: ElementTree.Element) -> str:
    return "".join(node.itertext()).strip()


def _escape_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", r"\|")


def _inline_code(value: str) -> str:
    compact = _escape_cell(value)
    longest_run = max((len(run) for run in re.findall(r"`+", compact)), default=0)
    # A fence one backtick longer than any run in the value is always safe.
    fence = "`" * max(1, longest_run + 1)
    padding = " " if "`" in compact else ""
    return f"{fence}{padding}{compact}{padding}{fence}"
