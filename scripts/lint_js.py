#!/usr/bin/env python3
"""Lint the JavaScript the HTML templates ship, with oxlint.

The JS lives in two places, and neither is a plain .js tree a linter can be
pointed at:

* ``html/templates/components/**/*.js`` — standalone files, except that a
  few pull in another one with a Jinja ``{% include %}`` line.
* ``<script>`` blocks inline in the templates, full of Jinja.

So the lint runs on a staged copy. Each component file is copied with its
include lines turned into comments (line numbers unchanged, so a finding's
``file:line`` is the real one). The inline scripts are taken from the pages
as the renderer produces them, in both themes, with every ``components/*.js``
include rendered as a one-line placeholder: those files are linted on their
own, and would otherwise be reported twice. A rendered script is reported as
``rendered/<page>-<theme>/script-<n>.js``; ``--keep`` leaves the staging
directory in place to read one.

oxlint comes from the optional ``js`` dependency group
(``uv sync --group js``); ``just lint-js`` runs this script with it, and
``just ci`` and CI run that. A warning fails the run (``--deny-warnings``),
like an error. Any other arguments are passed to oxlint.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from jinja2 import BaseLoader, Environment

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "claude_code_log" / "html" / "templates"
COMPONENTS = TEMPLATES / "components"
FIXTURE = REPO / "test" / "test_data" / "representative_messages.jsonl"

INCLUDE_LINE = re.compile(r"^(\s*)(\{%-?\s*include\b.*?%\})\s*$")
# A script block the browser runs: no src, and no type, or a JS one. JSON
# islands (type="application/json") are data, not code.
# The end tag tolerates what browsers accept (`</script >`, `</SCRIPT foo>`).
SCRIPT = re.compile(
    r"<script\b([^>]*)>(.*?)</script\b[^>]*>", re.IGNORECASE | re.DOTALL
)
JS_TYPES = {"", "text/javascript", "module", "application/javascript"}


def _stage_components(stage: Path) -> int:
    count = 0
    for source in sorted(COMPONENTS.rglob("*.js")):
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        staged = [
            INCLUDE_LINE.sub(r"\1// lint-js: \2 (linted on its own)", line)
            for line in lines
        ]
        target = stage / source.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(staged), encoding="utf-8")
        count += 1
    return count


class _StubJsIncludes(BaseLoader):
    """Serve each ``components/*.js`` include as a one-line placeholder."""

    def __init__(self, inner: BaseLoader) -> None:
        self.inner = inner

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, Optional[str], Optional[Callable[[], bool]]]:
        if template.startswith("components/") and template.endswith(".js"):
            return f"/* lint-js: {template} is linted on its own */", None, None
        return self.inner.get_source(environment, template)


def _rendered_pages() -> dict[str, str]:
    from claude_code_log.converter import load_transcript
    from claude_code_log.html.renderer import (
        generate_archive_search_html,
        generate_html,
        generate_projects_index_html,
    )
    from claude_code_log.html.utils import get_template_environment

    env = get_template_environment()
    if env.loader is None:
        raise SystemExit("the template environment has no loader")
    env.loader = _StubJsIncludes(env.loader)
    env.cache = None  # nothing compiled yet in this process; keep it so

    entries: Any = load_transcript(FIXTURE, silent=True)
    pages: dict[str, str] = {}
    for theme in ("classic", "minimal"):
        pages[f"transcript-{theme}"] = generate_html(entries, "Lint", theme=theme)
        pages[f"index-{theme}"] = generate_projects_index_html([], theme=theme)
        pages[f"archive_search-{theme}"] = generate_archive_search_html(theme)
    return pages


def _stage_rendered(stage: Path) -> int:
    count = 0
    for page, html in _rendered_pages().items():
        for index, (attrs, body) in enumerate(SCRIPT.findall(html)):
            # A script that held only placeholders has nothing left to lint.
            code = re.sub(r"/\* lint-js: .*? \*/", "", body)
            if re.search(r"\bsrc\s*=", attrs) or not code.strip():
                continue
            kind = re.search(r"""\btype\s*=\s*["']?([^"'\s>]*)""", attrs)
            if (kind.group(1).lower() if kind else "") not in JS_TYPES:
                continue
            target = stage / "rendered" / page / f"script-{index:02d}.js"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            count += 1
    return count


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    oxlint_args = [arg for arg in argv if arg != "--keep"]
    oxlint = shutil.which("oxlint")
    if oxlint is None:
        print(
            "oxlint not found: install the optional `js` group with"
            " `uv sync --group js`, or run `just lint-js`.",
            file=sys.stderr,
        )
        return 2

    stage = Path(tempfile.mkdtemp(prefix="lint-js-"))
    try:
        files = _stage_components(stage)
        scripts = _stage_rendered(stage)
        print(f"lint-js: {files} component files, {scripts} rendered scripts")
        # Run from the staging root so reported paths read as repo paths.
        config = REPO / ".oxlintrc.json"
        cmd = [oxlint, "--deny-warnings"]
        if config.exists():
            cmd += ["--config", str(config)]
        return subprocess.run([*cmd, *oxlint_args, "."], cwd=stage).returncode
    finally:
        if keep:
            print(f"lint-js: staged copy kept in {stage}")
        else:
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
