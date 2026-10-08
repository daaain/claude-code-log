#!/usr/bin/env python3
"""Format the template components that carry Jinja, with oxfmt.

oxfmt formats the rest of the JS and CSS directly (``just fmt-js``), but a
few files under ``html/templates/components/`` hold a Jinja ``{% include %}``
or ``{# comment #}``, which no JS or CSS parser accepts. ``.oxfmtrc.json``
ignores them, and this script formats them instead:

1. **mask** — each Jinja construct becomes a ``/* jinja:N */`` placeholder,
   a comment in both JS and CSS;
2. **format** — oxfmt formats the masked copy in a staging directory, with
   the repo's configuration;
3. **unmask** — each placeholder is replaced by its construct, verbatim and
   at its original column.

A construct must sit on lines of its own; anything else is refused rather
than guessed at. The files are found by content, so a new include is picked
up without a list to update, but it must also be added to ``ignorePatterns``
(the plain oxfmt run cannot parse it), which this script checks.

``--check`` writes nothing, lists the files that would change and exits 1
if there are any. It cannot use ``oxfmt --check`` on the masked copy:
oxfmt indents a placeholder that the file itself keeps at column 0.

oxfmt comes from the optional ``js`` dependency group
(``uv sync --group js``); ``just fmt-js`` and ``just fmt-check`` run this
script with it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENTS = REPO / "claude_code_log" / "html" / "templates" / "components"
CONFIG = REPO / ".oxfmtrc.json"

JINJA = re.compile(r"\{%.*?%\}|\{#.*?#\}|\{\{.*?\}\}", re.DOTALL)
PLACEHOLDER = re.compile(r"^[ \t]*/\* jinja:(\d+) \*/[ \t]*$", re.MULTILINE)


class MaskError(ValueError):
    pass


def mask(text: str) -> tuple[str, list[str]]:
    """Replace each Jinja construct's lines by a placeholder comment line.

    Returns the masked text and the replaced lines (without their final
    newline), indexed by placeholder number.
    """
    blocks: list[str] = []
    parts: list[str] = []
    pos = 0
    for match in JINJA.finditer(text):
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        end = len(text) if end < 0 else end
        before, after = text[start : match.start()], text[match.end() : end]
        if before.strip() or after.strip() or start < pos:
            raise MaskError(
                f"Jinja must sit on lines of its own: {match.group()[:60]!r}"
            )
        parts += [text[pos:start], f"/* jinja:{len(blocks)} */"]
        blocks.append(text[start:end])
        pos = end
    parts.append(text[pos:])
    return "".join(parts), blocks


def unmask(text: str, blocks: list[str]) -> str:
    """Put back the lines :func:`mask` replaced, whatever their indent now."""
    seen: list[int] = []

    def restore(match: re.Match[str]) -> str:
        seen.append(int(match.group(1)))
        return blocks[seen[-1]]

    text = PLACEHOLDER.sub(restore, text)
    if sorted(seen) != list(range(len(blocks))):
        raise MaskError(f"placeholders lost or duplicated: {seen}")
    return text


def read_config() -> dict:
    """Parse ``.oxfmtrc.json``, whose comments sit on lines of their own."""
    text = CONFIG.read_text(encoding="utf-8")
    return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE))


def jinja_components() -> list[str]:
    """The component JS/CSS files that hold Jinja, as repo-relative paths."""
    found = []
    for path in sorted([*COMPONENTS.rglob("*.js"), *COMPONENTS.rglob("*.css")]):
        if JINJA.search(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(REPO).as_posix())
    return found


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main(argv: list[str]) -> int:
    check = "--check" in argv
    oxfmt = shutil.which("oxfmt")
    if oxfmt is None:
        print(
            "oxfmt not found: install the optional `js` group with"
            " `uv sync --group js`, or run `just fmt-js`.",
            file=sys.stderr,
        )
        return 2

    config = read_config()
    files = jinja_components()
    unlisted = [f for f in files if f not in config.get("ignorePatterns", [])]
    if unlisted:
        print(
            "fmt-templates: add these to ignorePatterns in .oxfmtrc.json"
            " (they hold Jinja, which plain oxfmt cannot parse):",
            *unlisted,
            sep="\n  ",
            file=sys.stderr,
        )
        return 1

    # The staged copy is formatted with the repo's options, but not its
    # ignorePatterns, which name exactly these files.
    del config["ignorePatterns"]
    with tempfile.TemporaryDirectory(prefix="fmt-templates-") as tmp:
        stage = Path(tmp)
        _write(stage / "oxfmtrc.json", json.dumps(config))
        blocks = {}
        for f in files:
            masked, blocks[f] = mask(_read(REPO / f))
            _write(stage / f, masked)
        result = subprocess.run(
            [oxfmt, "-c", "oxfmtrc.json", "--write", *files],
            cwd=stage,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            return result.returncode

        changed = []
        for f in files:
            formatted = unmask(_read(stage / f), blocks[f])
            if formatted != _read(REPO / f):
                changed.append(f)
                if not check:
                    _write(REPO / f, formatted)

    verb = "would reformat" if check else "reformatted"
    for f in changed:
        print(f"fmt-templates: {verb} {f}")
    print(f"fmt-templates: {len(files)} files with Jinja, {len(changed)} {verb}")
    return 1 if check and changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
