#!/usr/bin/env python3
"""Check the documented Codex ThreadItem census against a generated schema."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


DOC_PATH = Path(__file__).parents[3] / "dev-docs" / "tools-coverage.md"
SECTION_START = "### Public item families"
SECTION_END = "### Concrete Codex call adapters"
ROW_RE = re.compile(
    r"^\| `(?P<name>[^`]+)` \| "
    r"(?P<status>Direct|Adapted|Partial|Missing) \|",
    re.MULTILINE,
)
TOTAL_RE = re.compile(
    r"\*\*Totals:\*\* (?P<direct>\d+) direct · "
    r"(?P<adapted>\d+) adapted · (?P<partial>\d+) partial · "
    r"(?P<missing>\d+) missing\."
)


def schema_items(path: Path) -> set[str]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    variants = schema["definitions"]["ThreadItem"]["oneOf"]
    return {variant["properties"]["type"]["enum"][0] for variant in variants}


def documented_items() -> tuple[dict[str, str], Counter[str], Counter[str]]:
    document = DOC_PATH.read_text(encoding="utf-8")
    section = document.split(SECTION_START, 1)[1].split(SECTION_END, 1)[0]
    rows = ROW_RE.findall(section)
    names = Counter(name for name, _ in rows)
    statuses = Counter(status.lower() for _, status in rows)
    return dict(rows), names, statuses


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {Path(sys.argv[0]).name} SCHEMA.json", file=sys.stderr)
        return 2

    expected = schema_items(Path(sys.argv[1]))
    documented, name_counts, statuses = documented_items()
    actual = set(documented)
    errors: list[str] = []

    if duplicates := sorted(name for name, count in name_counts.items() if count > 1):
        errors.append(f"duplicate documented items: {', '.join(duplicates)}")
    if missing := sorted(expected - actual):
        errors.append(f"schema items missing from documentation: {', '.join(missing)}")
    if stale := sorted(actual - expected):
        errors.append(f"documented items absent from schema: {', '.join(stale)}")

    document = DOC_PATH.read_text(encoding="utf-8")
    total_match = TOTAL_RE.search(document)
    if not total_match:
        errors.append("Codex totals line is missing or malformed")
    else:
        totals = {name: int(value) for name, value in total_match.groupdict().items()}
        if totals != statuses:
            errors.append(
                f"documented totals {totals} do not match table {dict(statuses)}"
            )

    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1

    print(f"Codex ThreadItem census: {len(actual)} variants")
    print(
        "Coverage: "
        + " / ".join(
            f"{status.title()} {statuses[status]}"
            for status in ("direct", "adapted", "partial", "missing")
        )
    )
    print("Generated schema and documentation are in sync [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
