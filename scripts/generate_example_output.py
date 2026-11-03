#!/usr/bin/env python3
"""Render a showcase "example output" HTML page from bundled sample data.

Replaces the old approach of rsyncing a ~140 MB transcript from the maintainer's
private ``~/.claude`` archive and attaching it to GitHub releases. Instead we
render a representative sample that already lives in the repo
(``test/test_data/real_projects/...`` — 23 sessions of this project's own early
development) into a single self-contained HTML file, which the docs build
publishes to the site.

Used two ways:

* By the MkDocs build (``docs/gen_pages.py`` via ``mkdocs-gen-files``) so the
  published example is regenerated on every build and never goes stale.
* Standalone: ``python scripts/generate_example_output.py [OUTPUT.html]
  [THEME]`` (defaults to ``test_output/example-transcript.html`` and the
  classic theme; pass ``minimal`` for the minimal theme).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from claude_code_log.converter import convert_jsonl_to

_REPO_ROOT = Path(__file__).resolve().parent.parent
# A real, multi-session sample of this project's own development. Rich enough to
# show the full range of message types and tools, but only ~9 MB rendered.
_SAMPLE_DIR = (
    _REPO_ROOT
    / "test"
    / "test_data"
    / "real_projects"
    / "-Users-dain-workspace-claude-code-log-sample"
)


def generate_example_html(out_path: Path, theme: str = "classic") -> Path:
    """Render the bundled sample project into a single self-contained HTML file.

    ``theme`` is the HTML theme (``classic`` or ``minimal``); the docs site
    publishes one page per theme.

    The sample is copied to a temp dir first so the render is deterministic
    (built fresh from the JSONL, ignoring any committed cache) and never writes
    into the repo's test data.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "sample"
        shutil.copytree(_SAMPLE_DIR, work)
        # Render from JSONL only — drop any committed cache or stale HTML.
        for leftover in (*work.glob("*.html"), work / "cache"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            elif leftover.exists():
                leftover.unlink()

        result = convert_jsonl_to(
            "html",
            work,
            generate_individual_sessions=False,
            use_cache=False,
            silent=True,
            theme=theme,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result, out_path)
    return out_path


if __name__ == "__main__":
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("test_output/example-transcript.html")
    )
    theme = sys.argv[2] if len(sys.argv) > 2 else "classic"
    written = generate_example_html(target, theme)
    size_mb = written.stat().st_size / 1_000_000
    print(f"Wrote {written} ({size_mb:.1f} MB)")
