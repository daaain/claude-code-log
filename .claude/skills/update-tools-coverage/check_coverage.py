#!/usr/bin/env python3
"""Compute tool-coverage status from the live factory registries.

Run from the repo root via ``uv run`` (needs pydantic on the path):

    # Feed the upstream documented tool names (whitespace/newline separated)
    printf 'Agent Artifact ... Write' | uv run python .claude/skills/update-tools-coverage/check_coverage.py

Emits, for the given upstream tool set:
  * per-tool support level (Full / Input only / Generic),
  * totals,
  * tools we register that upstream does NOT list (obsolete / undocumented),
  * drift vs. the committed dev-docs/tools-coverage.md table, if present.

The classification here is the *source of truth*; the doc's Notes column is
hand-maintained, so reconcile prose by hand after reading this output.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from claude_code_log.factories.tool_factory import (
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_PARSERS,
)

DOC = Path("dev-docs/tools-coverage.md")


def support(tool: str) -> str:
    has_in = tool in TOOL_INPUT_MODELS
    has_out = tool in TOOL_OUTPUT_PARSERS
    if has_in and has_out:
        return "Full"
    if has_in:
        return "Input only"
    return "Generic"


def read_upstream() -> list[str]:
    raw = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    tools = raw.split()
    if not tools:
        sys.exit(
            "No upstream tool names given.\n"
            "Pass them as args or on stdin (whitespace/newline separated).\n"
            "Get them by fetching https://code.claude.com/docs/en/tools-reference"
        )
    # de-dupe, preserve order
    seen: dict[str, None] = {}
    for t in tools:
        seen.setdefault(t, None)
    return list(seen)


def main() -> None:
    upstream = read_upstream()
    print(f"UPSTREAM documented tools: {len(upstream)}\n")

    print("=== Support level (source of truth) ===")
    width = max(len(t) for t in upstream)
    for t in sorted(upstream):
        print(f"  {t:<{width}}  {support(t)}")

    counts = Counter(support(t) for t in upstream)
    print(
        f"\n=== Totals === "
        f"Full: {counts['Full']}  "
        f"Input only: {counts['Input only']}  "
        f"Generic: {counts['Generic']}"
    )

    registered = set(TOOL_INPUT_MODELS) | set(TOOL_OUTPUT_PARSERS)
    extra = sorted(registered - set(upstream))
    print("\n=== We register but upstream does NOT document ===")
    print("(obsolete renames/supersessions, legacy aliases, or undocumented features)")
    for t in extra:
        sides = []
        if t in TOOL_INPUT_MODELS:
            sides.append("input")
        if t in TOOL_OUTPUT_PARSERS:
            sides.append("output")
        print(f"  {t:<20} typed: {', '.join(sides)}")

    # --- Drift check against the committed doc, if present ------------------
    if not DOC.exists():
        print(f"\n(no {DOC} yet — nothing to diff)")
        return

    doc_rows = dict(
        re.findall(
            r"^\| `(\w+)` \| (Full|Input only|Generic) \|",
            DOC.read_text(encoding="utf-8"),
            re.M,
        )
    )
    print(f"\n=== Drift vs {DOC} ===")
    problems = False
    for t in upstream:
        want, have = support(t), doc_rows.get(t)
        if have is None:
            print(f"  MISSING from doc: {t} (should be {want})")
            problems = True
        elif have != want:
            print(f"  MISMATCH: {t} — doc says {have!r}, code says {want!r}")
            problems = True
    for t in doc_rows.keys() - set(upstream):
        print(f"  STALE in doc (not in upstream list): {t}")
        problems = True
    if not problems:
        print("  in sync [OK]")


if __name__ == "__main__":
    main()
