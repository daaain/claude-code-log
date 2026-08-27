"""Resolve git commit SHAs to remote-hosted URLs (issue #156).

Used by the mistune SHA-link plugin (``markdown_plugins.py``) to turn
plain ``7c2e6f6``-shaped tokens in rendered Markdown into clickable
links — but only when the commit is actually reachable on a known
remote. Local-only commits stay as plain text so the rendered
transcript doesn't sprout broken links for work-in-progress branches.

## Architecture

- ``resolve_sha(cwd, sha)`` is the all-in-one resolver: from a working
  directory, look up ``origin``'s remote URL, classify the host
  (currently GitHub only), check the SHA is reachable from a
  remote-tracking branch (``git branch -r --contains``), expand short
  SHA → full SHA via ``git rev-parse``, and return the canonical
  commit URL. Returns ``None`` for any failure (no repo, no remote,
  unknown host, unresolved SHA, etc.).

- All ``git`` invocations are wrapped in ``functools.lru_cache`` —
  including the negative cases (``None``/``False`` results) — so a
  large transcript with hundreds of repeated SHAs only pays the
  subprocess cost once per (cwd, sha) tuple.

- A ``contextvars.ContextVar`` carries the per-render canonical cwd so
  the cached singleton mistune renderers don't have to be rebuilt per
  transcript. Use the ``render_with_repo_context(cwd)`` context
  manager at the top of a render pass; the SHA plugin's resolver
  reads the var transparently.

## Why not ``git ls-remote``?

``ls-remote`` hits the network on every call (~hundreds of ms even
warm). We rely on the user's local remote-tracking refs being
reasonably fresh — the typical claude-code-log invocation follows a
``git fetch`` for the same repo that produced the transcript. If a
SHA on the remote isn't yet reflected locally, it renders as plain
text; that's a correct-but-slightly-conservative fallback.

## Adding new hosts

``_HOST_URL_PATTERNS`` is the dispatch table. The URL parser in
``_parse_git_url`` handles SSH and HTTPS shapes uniformly, so adding
a new public forge is a one-line entry. For self-hosted instances
(in-house GitLab, Gitea, etc.) we can't enumerate every host name
the world might use, so there's also a fallback-template mechanism
read from the ``CLAUDE_CODE_LOG_GIT_LINK`` environment variable
(set directly, or via the ``--git-link`` CLI flag). The fallback
template uses ``{host}``, ``{path}``, and ``{sha}`` placeholders
and is consulted only when the static map misses, so a user with a
mix of public-forge and self-hosted repos still gets correct links
from both.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import re
import subprocess
from typing import Any, Iterator, Optional


# Commit URL templates per host. Add new hosts here; the URL parser
# already handles SSH (``git@host:path``) and HTTPS (``https://host/path``)
# uniformly, so a new entry is the only change typically needed.
_HOST_URL_PATTERNS: dict[str, str] = {
    "github.com": "https://github.com/{path}/commit/{sha}",
    "gitlab.com": "https://gitlab.com/{path}/-/commit/{sha}",
    "bitbucket.org": "https://bitbucket.org/{path}/commits/{sha}",
}


# Environment variable consulted as a fallback when the static host
# map misses. Lets users wire up self-hosted GitLab / Gitea / Forgejo
# / SourceHut etc. with a single template. Placeholders: ``{host}``
# (parsed from the remote URL), ``{path}`` (owner/repo or
# group/subgroup/repo), ``{sha}`` (full 40-char SHA). The CLI
# ``--git-link`` flag is a UX convenience that sets this env var.
_FALLBACK_TEMPLATE_ENV = "CLAUDE_CODE_LOG_GIT_LINK"


# SSH form ``git@host:path`` or HTTPS ``https://host/path``. The trailing
# ``.git`` is optional; trailing slash is optional.
_GIT_URL_RE = re.compile(
    r"""
    ^                           # start
    (?:
        git@                    # SSH user
      | (?:https?|git)://       # or HTTP(S) / git://
        (?:[^@/]+@)?            # optional user@ on HTTPS
    )
    (?P<host>[^:/]+)            # host
    [:/]                        # SSH ':' or HTTPS '/'
    (?P<path>.+?)               # owner/repo (non-greedy)
    (?:\.git)?                  # optional .git suffix
    /?                          # optional trailing slash
    $                           # end
    """,
    re.VERBOSE,
)


# Per-render canonical cwd. Set by ``render_with_repo_context`` from
# the top-level renderer (HTML / Markdown / JSON); read by
# ``resolve_sha_for_current_render`` which the mistune plugin uses as
# its per-call resolver. Default ``None`` means "no SHA resolution
# active" — the plugin then leaves all SHAs as plain text.
_render_repo_cwd: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_render_repo_cwd", default=None
)


def _parse_git_url(url: str) -> Optional[tuple[str, str]]:
    """Split a git URL into ``(host, "owner/repo")``.

    Returns ``None`` for shapes we don't handle (file paths, custom
    schemes). The path component is returned with any trailing
    ``.git`` stripped so it's safe to template directly into a URL.
    """
    if not url:
        return None
    m = _GIT_URL_RE.match(url.strip())
    if not m:
        return None
    host = m.group("host")
    path = m.group("path")
    if "/" not in path:
        # ``host:repo`` without an owner is malformed for our purposes
        # (GitHub & GitLab both require owner/repo).
        return None
    return host, path


@functools.lru_cache(maxsize=128)
def _git_remote_for(cwd: str) -> Optional[tuple[str, str]]:
    """Return ``(host, "owner/repo")`` for the cwd's ``origin`` remote.

    ``None`` for: cwd not in a git repo, no ``origin`` configured, or
    a remote URL we can't parse. Cached because a single transcript
    typically renders many SHAs from one repo.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_git_url(result.stdout.strip())


@functools.lru_cache(maxsize=4096)
def _commit_reachable_from_remote(cwd: str, sha: str) -> bool:
    """Whether ``sha`` is reachable from any remote-tracking branch.

    Uses local refs only — no network round-trip. Trades freshness for
    speed: if the user pushed a commit and hasn't fetched since,
    ``--contains`` won't see it on a remote-tracking branch and we'll
    render it as plain text. Documented elsewhere; the fallback is
    correct (no broken links).
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "-r", "--contains", sha],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


@functools.lru_cache(maxsize=4096)
def _expand_to_full_sha(cwd: str, sha: str) -> Optional[str]:
    """Resolve a (possibly short) SHA to its full 40-char form.

    The ``^{commit}`` peeling makes this fail cleanly when ``sha`` is
    actually a tag or other ref name we shouldn't link to as a commit.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--verify", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    full = result.stdout.strip()
    return full if len(full) == 40 else None


def _fallback_template() -> Optional[str]:
    """Read the user-supplied fallback URL template, or ``None``.

    Returns ``None`` for empty / unset, or for templates missing the
    required ``{sha}`` placeholder — the missing-``{sha}`` case is
    a silent skip rather than a crash so a misconfigured environment
    degrades to "no link" instead of breaking rendering. The CLI
    handler validates ``{sha}`` (and whitelists placeholders)
    eagerly with a loud error; this function is the defence-in-depth
    for the env-var-only path.

    Note: ``resolve_sha`` is ``@lru_cache``-d, so flipping
    ``CLAUDE_CODE_LOG_GIT_LINK`` mid-process won't re-resolve
    already-cached SHAs. Call ``clear_resolver_caches()`` after
    mutating the env var (the test fixtures already do).
    """
    template = os.environ.get(_FALLBACK_TEMPLATE_ENV, "").strip()
    if not template:
        return None
    if "{sha}" not in template:
        return None
    return template


@functools.lru_cache(maxsize=4096)
def resolve_sha(cwd: Optional[str], sha: str) -> Optional[str]:
    """Resolve a candidate SHA to a commit URL on a known remote.

    Returns ``None`` for any of:

    - No render-time cwd (e.g. plugin invoked outside a render pass).
    - cwd isn't inside a git repo, or has no ``origin`` remote.
    - Remote URL doesn't match any host in ``_HOST_URL_PATTERNS``
      *and* no usable fallback template is set in the environment.
    - SHA isn't reachable from any local remote-tracking branch.
    - SHA can't be expanded to a full commit (e.g. it was actually a
      tag, or shadows a non-commit ref).

    The static host map is consulted first; the fallback template
    fills in for self-hosted forges (in-house GitLab etc.). All
    steps are cached, so repeated SHAs in one transcript pay cost
    only on first encounter.
    """
    if not cwd:
        return None
    remote = _git_remote_for(cwd)
    if remote is None:
        return None
    host, path = remote
    template = _HOST_URL_PATTERNS.get(host)
    if template is None:
        # Static map missed: try the user-supplied fallback before
        # giving up. Keeps the static map small and predictable while
        # still letting users opt in to self-hosted forges.
        fallback = _fallback_template()
        if fallback is None:
            return None
        template = fallback
    if not _commit_reachable_from_remote(cwd, sha):
        return None
    full = _expand_to_full_sha(cwd, sha)
    if full is None:
        return None
    try:
        return template.format(host=host, path=path, sha=full)
    except (KeyError, IndexError, ValueError):
        # Template has an unknown placeholder (``{foo}``), a positional
        # ``{0}``, or unbalanced braces. The CLI handler whitelists
        # ``{host, path, sha}`` up-front and rejects loudly, so the
        # only way we reach here is a malformed env-var-only template
        # — silent skip matches the resolver's other degradation
        # paths (no link is better than a crash mid-render).
        return None


def resolve_sha_for_current_render(sha: str) -> Optional[str]:
    """Resolver fed to the mistune SHA-link plugin.

    Reads the per-render cwd from the ``_render_repo_cwd`` ContextVar
    and delegates to ``resolve_sha``. Returns ``None`` (→ render as
    plain text) when no render context is active.
    """
    return resolve_sha(_render_repo_cwd.get(), sha)


def current_render_repo_cwd() -> Optional[str]:
    """The repo cwd bound by the innermost ``render_with_repo_context``.

    Public read accessor for the ContextVar. Markdown memoization keys on
    this: the SHA-linkifier makes rendered output depend on which repo the
    render is scoped to, so the same text is *not* interchangeable across
    projects (see ``render_cache``).
    """
    return _render_repo_cwd.get()


@contextlib.contextmanager
def render_with_repo_context(cwd: Optional[str]) -> Iterator[None]:
    """Bind a canonical repo cwd for the duration of a render pass.

    The mistune SHA-link plugin reads this value via
    ``resolve_sha_for_current_render``. Resetting on exit keeps the
    var clean across nested or sequential renders.
    """
    token = _render_repo_cwd.set(cwd)
    try:
        yield
    finally:
        _render_repo_cwd.reset(token)


def canonical_cwd_from_messages(messages: list[Any]) -> Optional[str]:
    """Pick a single repo cwd to use for SHA resolution across ``messages``.

    Each transcript entry carries its own ``cwd`` (the working
    directory of the Claude Code session at the moment that entry was
    written). For SHA linkification we need *one* cwd to scope the
    resolver against — picks the most common non-empty value seen
    across messages. Single-project transcripts (the dominant case)
    yield a stable answer; combined transcripts spanning several
    projects pick the dominant project's cwd, which is the right
    behaviour for resolving SHAs the user typed about that work.

    Returns ``None`` when no message exposes a usable cwd; callers
    should fall through to "no SHA resolution" in that case.
    """
    counts: dict[str, int] = {}
    for msg in messages:
        cwd = getattr(msg, "cwd", None)
        if isinstance(cwd, str) and cwd:
            counts[cwd] = counts.get(cwd, 0) + 1
    if not counts:
        return None
    # ``max`` with a key picks the highest count; ties fall to the
    # first-inserted entry (Python dict preserves insertion order),
    # which is the earliest occurrence — a sensible tiebreaker.
    return max(counts, key=lambda k: counts[k])


def clear_resolver_caches() -> None:
    """Drop all LRU caches in this module.

    Useful for tests that mock subprocess and need each test to start
    from a clean cache state, and for long-running processes (e.g. the
    TUI) where the user may swap branches and want stale URLs flushed.
    """
    _git_remote_for.cache_clear()
    _commit_reachable_from_remote.cache_clear()
    _expand_to_full_sha.cache_clear()
    resolve_sha.cache_clear()
