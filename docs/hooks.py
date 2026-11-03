"""MkDocs build hooks.

The developer docs under ``dev-docs/`` (surfaced on the site as the *Development*
section) and the root ``CONTRIBUTING.md`` link to source files in the repository
— e.g. ``../claude_code_log/cli.py`` or ``test/README.md``. Those targets are
not part of the documentation site, so under ``mkdocs build --strict`` they would
be flagged as broken links.

Rather than rewrite the source Markdown (which must keep working for people
reading the repo directly), this hook rewrites such links to absolute GitHub
URLs at build time via the ``on_page_markdown`` event.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

_GITHUB_BLOB = "https://github.com/daaain/claude-code-log/blob/main/"

# Top-level repo paths that live outside the docs site. A link resolving to one
# of these (after stripping any leading ``../``) is rewritten to GitHub.
_REPO_ROOTS = (
    "claude_code_log/",
    "test/",
    "scripts/",
    "work/",
    "dev-docs/",
    "stubs/",
    ".github/",
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "LICENSE",
    "justfile",
    "pyproject.toml",
    "mise.toml",
    "mkdocs.yml",
)

_LINK_RE = re.compile(r"(?P<text>\[[^\]]*\])\((?P<target>[^)]+)\)")


def _with_fragment(base: str, fragment: str) -> str:
    return f"{base}#{fragment}" if fragment else base


def _rewrite_target(target: str, page_dir: str) -> str:
    # Leave absolute URLs, anchors, and mailto links untouched.
    if re.match(r"^[a-z]+://", target) or target.startswith(("#", "mailto:")):
        return target

    # Split off any fragment so it can be reattached after rewriting.
    path, _, fragment = target.partition("#")
    if not path:
        return target

    # Normalise away leading ./ and ../ to find the repo-relative path.
    cleaned = path
    while cleaned.startswith(("./", "../")):
        cleaned = cleaned[3:] if cleaned.startswith("../") else cleaned[2:]

    # Links into ``docs/`` point at pages that live on this site. Rewrite them
    # to a path relative to the current page (docs_dir is the site root).
    if cleaned.startswith("docs/"):
        # A bare ``docs/`` directory link maps to the site home.
        site_target = cleaned[len("docs/") :] or "index.md"
        rel = posixpath.relpath(site_target, page_dir or ".")
        return _with_fragment(rel, fragment)

    # Links into source dirs/files don't exist on the site → send to GitHub.
    if cleaned.startswith(_REPO_ROOTS):
        return _with_fragment(_GITHUB_BLOB + cleaned, fragment)

    return target


def on_page_markdown(markdown: str, page: Any = None, **_kwargs: Any) -> str:
    src_uri = getattr(getattr(page, "file", None), "src_uri", "") or ""
    page_dir = posixpath.dirname(src_uri)

    def _sub(match: re.Match[str]) -> str:
        new_target = _rewrite_target(match.group("target"), page_dir)
        return f"{match.group('text')}({new_target})"

    return _LINK_RE.sub(_sub, markdown)
