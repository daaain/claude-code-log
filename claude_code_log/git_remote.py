"""Resolve git commit SHAs to remote-hosted URLs (issue #156).

Used by the mistune SHA-link plugin (``markdown_plugins.py``) to turn
plain ``7c2e6f6``-shaped tokens in rendered Markdown into clickable
links — but only when the commit is actually reachable on a known
remote. Local-only commits stay as plain text so the rendered
transcript doesn't sprout broken links for work-in-progress branches.

## Architecture

- ``resolve_sha(cwd, sha)`` is the all-in-one resolver: from a working
  directory, look up ``origin``'s remote URL, classify the host
  (static map, then the fallback template), find the commit the SHA
  names, and return the canonical commit URL. Returns ``None`` for any
  failure (no repo, no remote, unknown host, unresolved SHA, etc.).

- Commits are found in **one** ``git rev-list --remotes`` per working
  directory (``_RemoteCommits``), not one subprocess per candidate: a
  short SHA resolves by prefix lookup in that sorted list, and an
  ambiguous prefix stays unresolved, as ``git rev-parse`` would leave
  it. Set membership answers the negatives for free — and in real
  transcripts over a third of the SHA-shaped tokens are not commits at
  all (task ids, UUID fragments, digit runs), each of which used to
  cost a ``git branch -r --contains`` walk just to learn so (issue
  #327: 549 of those, ~22 ms each, against ~20 ms for the whole list).

- When the list cannot be read (``git`` errors or times out), nothing
  links: an unverifiable token stays plain text, as it did when the
  per-candidate check failed. Linking unvalidated instead would make
  every non-commit token a dead link — over a third of them — with
  nothing on the page saying a check was skipped. ``_commit_for`` is
  the one place that decides what links.

- The list is held per process and re-read when a lookup misses and
  the list is older than ``_REMOTE_COMMITS_MAX_AGE_SECONDS``, so a
  long-lived process (``watch``, the TUI) picks up commits fetched
  after it started without re-reading on every miss.

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
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
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
# group/subgroup/repo), ``{sha}`` (full SHA). The CLI ``--git-link``
# flag is a UX convenience that sets this env var.
_FALLBACK_TEMPLATE_ENV = "CLAUDE_CODE_LOG_GIT_LINK"

# Wall-clock cap on the ``git config`` remote lookup. It is a safety
# valve, not a budget: the call is sub-100ms on a normal repo and
# memoized per cwd, so the cap only ever bites when the
# environment — not the repo — is pathologically slow. It was 2s, which
# the Windows CI runner exceeded under parallel test workers (git process
# spawn there is slow enough on its own that ~2s of contention is
# reachable; see the TEMP-redirect note in .github/workflows/ci.yml), and
# a timeout is indistinguishable from "not resolvable" — the SHA silently
# renders as plain text. 5s matches the other subprocess timeouts in the
# codebase and buys ~2.5x headroom without making a genuinely broken git
# stall a render for long.
_GIT_TIMEOUT_SECONDS = 5

# Wall-clock cap on ``git rev-list --remotes``. Longer than the lookup
# cap because the walk scales with history (~20 ms at 1.6k commits,
# ~0.1 s at 13k) and runs once per cwd rather than once per SHA, so a
# generous cap costs nothing on a normal repo — while a timeout costs
# every commit link on the page, which stay plain text.
_GIT_LIST_TIMEOUT_SECONDS = 30

# Floor on how long a commit list is trusted before a lookup miss
# re-reads it. Staleness only matters to a long-lived process (``watch``,
# the TUI) whose transcripts cite commits fetched after it started; the
# floor bounds the re-reads that the non-commit tokens — a third of the
# candidates — would otherwise trigger. A list that was slow to read is
# trusted proportionally longer (``_REMOTE_COMMITS_REREAD_FACTOR`` times
# its read time), so re-reading costs at most ~5% of wall time.
_REMOTE_COMMITS_MAX_AGE_SECONDS = 60.0
_REMOTE_COMMITS_REREAD_FACTOR = 20.0

# Distinct working directories whose commit lists a process holds at
# once. Each costs 20 bytes per commit; an all-projects run in one process
# visits one cwd per project.
_REMOTE_COMMITS_MAX_CWDS = 32

# What a token must look like to be linked at all. Mirrors the plugins'
# ``SHA_PATTERN`` (git's 7-char default abbreviation up to a full SHA-1),
# so ``resolve_sha`` holds that contract whoever calls it: the prefix
# search alone would accept uppercase hex and shorter abbreviations.
_SHA_SHAPE_RE = re.compile(r"[0-9a-f]{7,40}")

# Monotonic clock for list ages; a module attribute so tests can move time.
_now = time.monotonic


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
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_git_url(result.stdout.strip())


@dataclass(frozen=True)
class _RemoteCommits:
    """Every commit reachable from a remote-tracking ref of one cwd.

    Uses local refs only — no network round-trip. Trades freshness for
    speed: a commit pushed but not yet fetched isn't on a remote-tracking
    ref, so it renders as plain text (no broken link).

    ``ids`` holds the binary object ids, sorted and concatenated at a
    fixed ``width`` (20 bytes for SHA-1, 32 for SHA-256): one bytes
    object at the id's own size, rather than a list of ~90-byte hex
    strings — every render worker holds its own copy. ``ids is None``
    means the list could not be read, and finds nothing, like an empty one.
    """

    ids: Optional[bytes]
    width: int
    read_at: float
    trusted_for: float

    def is_stale(self) -> bool:
        return _now() - self.read_at >= self.trusted_for

    def find(self, sha: str) -> Optional[str]:
        """The full id of the one commit ``sha`` abbreviates, else ``None``.

        ``None`` both when no commit starts with ``sha`` and when several
        do: an ambiguous abbreviation stays unresolved, as ``git
        rev-parse`` leaves it. Ambiguity is judged among the listed
        commits only, where git also counts local-only commits and other
        objects — so a prefix shared with an unpushed commit now links to
        the pushed one instead of to nothing.
        """
        ids, width = self.ids, self.width
        if not ids or len(sha) > 2 * width:
            return None
        # Every id starting with ``sha`` lies in [low, high].
        low = bytes.fromhex(sha.ljust(2 * width, "0"))
        high = bytes.fromhex(sha.ljust(2 * width, "f"))
        count = len(ids) // width
        lo, hi = 0, count
        while lo < hi:
            mid = (lo + hi) // 2
            if ids[mid * width : (mid + 1) * width] < low:
                lo = mid + 1
            else:
                hi = mid
        first = ids[lo * width : (lo + 1) * width]
        if lo == count or first > high:
            return None
        if lo + 1 < count and ids[(lo + 1) * width : (lo + 2) * width] <= high:
            return None
        return first.hex()


def _read_remote_commits(cwd: str) -> _RemoteCommits:
    """One ``git rev-list --remotes`` for ``cwd``, as a ``_RemoteCommits``."""
    started = _now()
    ids: Optional[bytes] = None
    width = 20
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-list", "--remotes"],
            capture_output=True,
            text=True,
            timeout=_GIT_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        lines = result.stdout.split()
        if lines:
            width = len(lines[0]) // 2
        try:
            decoded = sorted(bytes.fromhex(line) for line in lines)
        except ValueError:
            decoded = None
        if decoded is not None and all(len(i) == width for i in decoded):
            ids = b"".join(decoded)
    finished = _now()
    return _RemoteCommits(
        ids=ids,
        width=width,
        read_at=finished,
        trusted_for=max(
            _REMOTE_COMMITS_MAX_AGE_SECONDS,
            _REMOTE_COMMITS_REREAD_FACTOR * (finished - started),
        ),
    )


_remote_commits_by_cwd: OrderedDict[str, _RemoteCommits] = OrderedDict()
_remote_commits_lock = threading.Lock()


def _remote_commits(cwd: str, *, reread: bool = False) -> _RemoteCommits:
    """The held commit list for ``cwd``, reading it on first use.

    ``reread`` forces a fresh read (the caller has decided the held list
    is stale). Bounded to ``_REMOTE_COMMITS_MAX_CWDS`` lists, least
    recently used evicted first.
    """
    with _remote_commits_lock:
        held = None if reread else _remote_commits_by_cwd.get(cwd)
        if held is not None:
            _remote_commits_by_cwd.move_to_end(cwd)
            return held
    # Read outside the lock: a slow ``git`` must not serialize other
    # cwds. Two threads racing on one cwd both read; the last one wins.
    held = _read_remote_commits(cwd)
    with _remote_commits_lock:
        _remote_commits_by_cwd[cwd] = held
        _remote_commits_by_cwd.move_to_end(cwd)
        while len(_remote_commits_by_cwd) > _REMOTE_COMMITS_MAX_CWDS:
            _remote_commits_by_cwd.popitem(last=False)
    return held


def _commit_for(cwd: str, sha: str) -> Optional[str]:
    """The commit id to link ``sha`` to, or ``None`` to leave it as text.

    The single definition of what gets linked: a SHA-shaped token that
    abbreviates exactly one commit reachable from a remote-tracking ref,
    linked by that commit's full id. Everything else — local-only
    commits, other repositories' SHAs, task ids, UUID fragments, and
    every token when the list could not be read — stays plain text.

    A miss against a list older than its trust window re-reads the list
    once, so commits fetched since it was read are found; a hit needs no
    re-read. The asymmetry is deliberate: a commit that *leaves* the
    remote (force-push, deleted branch) keeps linking until the process
    restarts, because re-reading on hits would bring back a ``git`` call
    per token.
    """
    if not _SHA_SHAPE_RE.fullmatch(sha):
        return None
    commits = _remote_commits(cwd)
    full = commits.find(sha)
    if full is None and commits.is_stale():
        full = _remote_commits(cwd, reread=True).find(sha)
    return full


def _fallback_template() -> Optional[str]:
    """Read the user-supplied fallback URL template, or ``None``.

    Returns ``None`` for empty / unset, or for templates missing the
    required ``{sha}`` placeholder — the missing-``{sha}`` case is
    a silent skip rather than a crash so a misconfigured environment
    degrades to "no link" instead of breaking rendering. The CLI
    handler validates ``{sha}`` (and whitelists placeholders)
    eagerly with a loud error; this function is the defence-in-depth
    for the env-var-only path.

    Read on every resolve for a host the static map misses, so flipping
    ``CLAUDE_CODE_LOG_GIT_LINK`` mid-process takes effect for SHAs
    rendered afterwards — though the Markdown memo (``render_cache``)
    still serves text it already rendered.
    """
    template = os.environ.get(_FALLBACK_TEMPLATE_ENV, "").strip()
    if not template:
        return None
    if "{sha}" not in template:
        return None
    return template


def resolve_sha(cwd: Optional[str], sha: str) -> Optional[str]:
    """Resolve a candidate SHA to a commit URL on a known remote.

    Returns ``None`` for any of:

    - No render-time cwd (e.g. plugin invoked outside a render pass).
    - cwd isn't inside a git repo, or has no ``origin`` remote.
    - Remote URL doesn't match any host in ``_HOST_URL_PATTERNS``
      *and* no usable fallback template is set in the environment.
    - ``_commit_for`` declines it: not SHA-shaped, or — when the commit
      list is readable — not an unambiguous abbreviation of a commit
      reachable from a remote-tracking ref.

    The static host map is consulted first; the fallback template
    fills in for self-hosted forges (in-house GitLab etc.). Not
    memoized itself: its ``git`` costs are (the remote per cwd, the
    commit list per cwd), and what remains is a prefix search.
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
    commit = _commit_for(cwd, sha)
    if commit is None:
        return None
    try:
        return template.format(host=host, path=path, sha=commit)
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
    """Drop the remote lookups and commit lists this module holds.

    Useful for tests that mock subprocess and need each test to start
    from a clean cache state, and for long-running processes (e.g. the
    TUI) that want a changed remote or freshly fetched commits seen
    before the lists' own re-read window.
    """
    _git_remote_for.cache_clear()
    with _remote_commits_lock:
        _remote_commits_by_cwd.clear()
