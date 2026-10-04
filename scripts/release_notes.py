"""One version's CHANGELOG.md section, as the notes of its GitHub Release.

The release workflow (.github/workflows/release.yml) runs this to write the
notes, and `just release-preview` runs it to show them before tagging. A
version with no section is a refusal, not empty notes: the workflow uses that
to stop a tag `just release-prep` didn't make before anything is published.

Stdlib-only, so the workflow can run it with the runner's own python:

    python3 scripts/release_notes.py <X.Y.Z> [CHANGELOG.md]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_URL = "https://github.com/daaain/claude-code-log"
_VERSION_HEADING = re.compile(r"^## \[([^\]]+)\]")


class MissingSection(ValueError):
    """CHANGELOG.md has no `## [X.Y.Z]` section for the requested version."""


def notes(changelog: str, version: str) -> str:
    """The section's body, plus a compare link to the version below it."""
    lines = changelog.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if (m := _VERSION_HEADING.match(line)) and m.group(1) == version
        ),
        None,
    )
    if start is None:
        raise MissingSection(f"CHANGELOG.md has no '## [{version}]' section")

    # The section runs to the next `## ` heading of any kind (the file ends in
    # an unversioned `## Previous Versions`); only a versioned one is linked.
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    body = "\n".join(lines[start + 1 : end]).strip() + "\n"
    previous = _VERSION_HEADING.match(lines[end]) if end < len(lines) else None
    if previous:
        body += (
            f"\n**Full Changelog**: {REPO_URL}/compare/"
            f"{previous.group(1)}...{version}\n"
        )
    return body


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1] if len(argv) == 2 else "CHANGELOG.md")
    try:
        sys.stdout.write(notes(path.read_text(encoding="utf-8"), argv[0]))
    except MissingSection as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
