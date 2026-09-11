"""Tests for the git-commit linkifier (issue #156).

Covers two modules and their integration:

- ``markdown_plugins.make_sha_plugin`` and ``linkify_shas_in_text``:
  unit-level checks of the inline-parser plugin and the
  Markdown-side text substitution helper. Resolver is mocked.

- ``git_remote.resolve_sha`` + ``render_with_repo_context``: end-to-
  end against the project's own git repo (cheap, deterministic, no
  network — uses local remote-tracking refs only).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from claude_code_log.git_remote import (
    _parse_git_url,
    canonical_cwd_from_messages,
    clear_resolver_caches,
    render_with_repo_context,
    resolve_sha,
    resolve_sha_for_current_render,
)
from claude_code_log.html.utils import render_markdown, render_user_markdown
from claude_code_log.markdown_plugins import (
    SHA_PATTERN,
    linkify_shas_in_text,
    make_codespan_sha_plugin,
    make_sha_plugin,
)


# ---------------------------------------------------------------------------
# Fixture: a deterministic resolver that maps known SHAs to URLs.


_KNOWN_URLS = {
    "abc1234": "https://example.com/abc1234",
    "deadbeefcafe": "https://example.com/deadbeefcafe",
}


def _mock_resolve(sha: str) -> Optional[str]:
    return _KNOWN_URLS.get(sha)


# ---------------------------------------------------------------------------
# SHA_PATTERN regex


class TestShaPattern:
    def test_matches_short_sha(self):
        assert re.fullmatch(SHA_PATTERN, "abc1234")

    def test_matches_full_sha(self):
        assert re.fullmatch(SHA_PATTERN, "0" * 40)

    def test_rejects_too_short(self):
        # 6 chars: below the 7-char minimum.
        assert re.fullmatch(SHA_PATTERN, "abc123") is None

    def test_rejects_too_long(self):
        # 41 chars: above the 40-char maximum.
        assert re.fullmatch(SHA_PATTERN, "0" * 41) is None

    def test_rejects_non_hex(self):
        assert re.fullmatch(SHA_PATTERN, "abcg123") is None

    def test_rejects_uppercase(self):
        # Pattern is lowercase-only; git short SHAs are conventionally
        # lowercase. Tighten if real-world data shows uppercase.
        assert re.fullmatch(SHA_PATTERN, "ABC1234") is None


# ---------------------------------------------------------------------------
# make_sha_plugin: HTML mistune integration


def _render_with_plugin(text: str, resolve=_mock_resolve) -> str:
    """Build a fresh mistune renderer with our plugin and render *text*."""
    import mistune

    md = mistune.create_markdown(plugins=[make_sha_plugin(resolve)])
    return str(md(text)).strip()


class TestShaPluginInline:
    def test_emits_link_for_resolvable_sha(self):
        out = _render_with_plugin("See abc1234 in the diff")
        assert '<a href="https://example.com/abc1234">abc1234</a>' in out

    def test_passes_through_unresolved_sha(self):
        out = _render_with_plugin("Local-only commit ffffeee")
        assert "ffffeee" in out
        assert "<a" not in out

    def test_fires_inside_emphasis(self):
        # Confirms main's research finding: the inline plugin recurses
        # through `parse_emphasis` so registered rules fire inside
        # *…* and **…**.
        out = _render_with_plugin("**before abc1234 after**")
        assert "<strong>" in out
        assert '<a href="https://example.com/abc1234">' in out

    def test_does_not_fire_inside_codespan(self):
        out = _render_with_plugin("`abc1234` stays code")
        assert "<code>abc1234</code>" in out
        assert "<a" not in out

    def test_does_not_fire_inside_fenced_block(self):
        out = _render_with_plugin("```\nabc1234\n```")
        assert "abc1234" in out
        assert "<a" not in out

    def test_does_not_double_wrap_existing_link(self):
        # The existing-link guard (state.in_link) keeps us from emitting
        # a nested <a> when the SHA appears inside a Markdown link.
        out = _render_with_plugin("[abc1234](http://manual.example/x)")
        assert out.count("<a ") == 1
        assert "manual.example" in out

    def test_pluralized_sha_does_not_match(self):
        # 'abc12347' (8 chars) IS a valid SHA shape, but the trailing
        # punctuation cases below shouldn't break the match.
        out = _render_with_plugin("Commit abc1234.")
        assert '<a href="https://example.com/abc1234">abc1234</a>' in out


# ---------------------------------------------------------------------------
# make_codespan_sha_plugin: HTML mistune integration for `sha` codespans


def _render_with_both_plugins(text: str, resolve=_mock_resolve) -> str:
    """Build a fresh mistune renderer with *both* SHA plugins."""
    import mistune

    md = mistune.create_markdown(
        plugins=[
            make_sha_plugin(resolve),
            make_codespan_sha_plugin(resolve),
        ]
    )
    return str(md(text)).strip()


class TestCodespanShaPlugin:
    def test_wraps_codespan_sha_in_link(self):
        out = _render_with_both_plugins("See `abc1234` here")
        assert '<a href="https://example.com/abc1234"><code>abc1234</code></a>' in out

    def test_unresolved_codespan_sha_stays_plain_code(self):
        # Local-only SHA: codespan preserved, no link emitted.
        out = _render_with_both_plugins("Local `ffffeee` commit")
        assert "<code>ffffeee</code>" in out
        assert "<a" not in out

    def test_codespan_with_non_sha_body_unchanged(self):
        out = _render_with_both_plugins("Call `hello` first")
        assert "<code>hello</code>" in out
        assert "<a" not in out

    def test_codespan_with_mixed_body_unchanged(self):
        # `git show abc1234`: body isn't *exactly* a SHA, so we don't
        # fire — exactly as the bare-SHA plugin doesn't either.
        out = _render_with_both_plugins("Run `git show abc1234`")
        assert "<code>git show abc1234</code>" in out
        assert "<a" not in out

    def test_codespan_sha_inside_emphasis(self):
        # Bold codespan-SHA: <strong><a><code>…</code></a></strong>.
        out = _render_with_both_plugins("Bold **`abc1234`** here")
        assert "<strong>" in out
        assert '<a href="https://example.com/abc1234"><code>abc1234</code></a>' in out

    def test_codespan_sha_inside_existing_link_preserves_code(self):
        # `[\`abc1234\`](url)`: don't double-wrap, but the in_link
        # branch still emits a codespan token, so the link content
        # is monospaced (not raw backtick text).
        out = _render_with_both_plugins("[`abc1234`](http://manual.example/x)")
        assert out.count("<a ") == 1
        assert "manual.example" in out
        assert "<code>abc1234</code>" in out

    def test_codespan_sha_inside_fenced_block_unchanged(self):
        out = _render_with_both_plugins("```\n`abc1234`\n```")
        assert "<a" not in out
        assert "abc1234" in out


# ---------------------------------------------------------------------------
# linkify_shas_in_text: Markdown-side text substitution


class TestLinkifyShasInText:
    def test_substitutes_resolvable(self):
        out = linkify_shas_in_text("See abc1234 here", _mock_resolve)
        assert out == "See [abc1234](https://example.com/abc1234) here"

    def test_leaves_unresolved_alone(self):
        out = linkify_shas_in_text("Local commit ffffeee", _mock_resolve)
        assert out == "Local commit ffffeee"

    def test_handles_multiple_shas(self):
        out = linkify_shas_in_text("First abc1234 then deadbeefcafe", _mock_resolve)
        assert "[abc1234](https://example.com/abc1234)" in out
        assert "[deadbeefcafe](https://example.com/deadbeefcafe)" in out

    def test_empty_text_returns_unchanged(self):
        assert linkify_shas_in_text("", _mock_resolve) == ""

    # -- Negative-context tests (regression for monk's review on PR #156) --
    #
    # The HTML side's plugin gets these skips for free from mistune's
    # inline parser; the Markdown side has to enforce them manually
    # via the tokenizer in ``_linkify_inline`` / ``_linkify_block_tokens``.
    # Mirrors the parity contract checked by
    # ``TestShaPluginInline.test_does_not_fire_inside_codespan`` etc.

    def test_skips_inside_inline_codespan(self):
        out = linkify_shas_in_text("Run `git show abc1234` now", _mock_resolve)
        assert out == "Run `git show abc1234` now"

    def test_skips_inside_double_backtick_codespan(self):
        # CommonMark allows ``…`` to embed single backticks; the SHA
        # inside still must not be substituted.
        out = linkify_shas_in_text("``code abc1234 in span`` here", _mock_resolve)
        assert out == "``code abc1234 in span`` here"

    def test_skips_inside_fenced_block_backtick(self):
        out = linkify_shas_in_text("```\nabc1234\n```", _mock_resolve)
        assert out == "```\nabc1234\n```"

    def test_skips_inside_fenced_block_tilde(self):
        out = linkify_shas_in_text("~~~\nabc1234\n~~~", _mock_resolve)
        assert out == "~~~\nabc1234\n~~~"

    def test_skips_inside_indented_code_block(self):
        out = linkify_shas_in_text("    abc1234 indented", _mock_resolve)
        assert out == "    abc1234 indented"

    def test_documents_tab_indent_gap(self):
        # CommonMark treats a leading tab as 4-space-equivalent → an
        # indented code block. Our block tokenizer gates strictly on
        # space-only indent (``line.lstrip(" ")``), so a tab-prefixed
        # SHA does get linkified — in violation of CommonMark. This
        # test pins the current (incorrect-but-documented) behaviour
        # so any future fix flags it explicitly. Revisit if real-world
        # transcripts show tab-indented prose; until then, the cost of
        # widening the indent detector isn't worth it.
        out = linkify_shas_in_text("\tabc1234 tab-indented", _mock_resolve)
        assert out == "\t[abc1234](https://example.com/abc1234) tab-indented"

    def test_skips_existing_markdown_link(self):
        # The HTML plugin's ``state.in_link`` guard's text-helper
        # equivalent: a SHA already inside a ``[text](url)`` must not
        # be double-wrapped.
        out = linkify_shas_in_text("[abc1234](manual.example/x)", _mock_resolve)
        assert out == "[abc1234](manual.example/x)"

    def test_substitutes_around_codespan(self):
        # Prose before / after a codespan still gets substituted; the
        # SHA inside the codespan is preserved verbatim.
        out = linkify_shas_in_text(
            "Before abc1234 then `inline abc1234` after abc1234",
            _mock_resolve,
        )
        assert (
            out == "Before [abc1234](https://example.com/abc1234) "
            "then `inline abc1234` after [abc1234](https://example.com/abc1234)"
        )

    def test_substitutes_around_fenced_block(self):
        out = linkify_shas_in_text(
            "Mention abc1234 then\n```\nabc1234 inside\n```\nthen abc1234 again",
            _mock_resolve,
        )
        assert (
            out == "Mention [abc1234](https://example.com/abc1234) then\n"
            "```\nabc1234 inside\n```\n"
            "then [abc1234](https://example.com/abc1234) again"
        )

    def test_unmatched_backtick_still_substitutes_following_prose(self):
        # A lone ``` ` ``` doesn't open a codespan (no matching close);
        # SHAs after it should still get substituted.
        out = linkify_shas_in_text("Lone ` then abc1234", _mock_resolve)
        assert out == "Lone ` then [abc1234](https://example.com/abc1234)"

    def test_lone_open_bracket_terminates(self):
        # Regression: a ``[`` that doesn't open a valid ``[text](url)``
        # link must be emitted as a literal char. Earlier tokenizer
        # stalled here because the prose-accumulator stopped on the
        # same ``[`` it was meant to consume → infinite loop.
        out = linkify_shas_in_text("Label [INFO] abc1234", _mock_resolve)
        assert out == "Label [INFO] [abc1234](https://example.com/abc1234)"

    def test_bracket_without_closing_paren_still_substitutes(self):
        # ``[text]`` with no following ``(url)`` is not a Markdown link
        # — neither does mistune treat it as one on the HTML side
        # (no matching reference definition) so the SHA plugin fires
        # on the prose inside. The Markdown helper mirrors that: the
        # brackets become literal characters around a substituted SHA.
        out = linkify_shas_in_text("see [abc1234] note", _mock_resolve)
        assert out == "see [[abc1234](https://example.com/abc1234)] note"

    # -- Codespan-wrapped SHA → link (parity with
    # ``make_codespan_sha_plugin`` on the HTML side) --

    def test_codespan_only_sha_becomes_linked_codespan(self):
        # ``\`abc1234\``` body is exactly a SHA → wrap the *whole*
        # span (backticks included) in a link target. The resulting
        # ``[\`abc1234\`](url)`` is valid Markdown that renders as
        # ``<a><code>abc1234</code></a>`` — same as the HTML plugin.
        out = linkify_shas_in_text("See `abc1234` here", _mock_resolve)
        assert out == "See [`abc1234`](https://example.com/abc1234) here"

    def test_unresolved_codespan_sha_stays_opaque(self):
        # Local-only commit: resolver returns None → codespan stays
        # as-is, no link wrapping (no broken URLs in the transcript).
        out = linkify_shas_in_text("Local `ffffeee` commit", _mock_resolve)
        assert out == "Local `ffffeee` commit"

    def test_codespan_with_mixed_body_unchanged(self):
        # Single backticks but body isn't *exactly* a SHA → stays
        # opaque, same contract as the existing
        # ``test_skips_inside_inline_codespan`` case.
        out = linkify_shas_in_text("`git show abc1234`", _mock_resolve)
        assert out == "`git show abc1234`"

    def test_codespan_sha_with_double_backticks_unchanged(self):
        # ``\`\`sha\`\``` is a valid CommonMark codespan but multi-
        # backtick: we only rewrite the single-backtick form. Spans
        # stay opaque, consistent with the conservative scope of
        # ``CODESPAN_SHA_PATTERN``.
        out = linkify_shas_in_text("``abc1234``", _mock_resolve)
        assert out == "``abc1234``"

    def test_bold_codespan_sha_becomes_bold_link(self):
        # ``**\`abc1234\`**``: the ``*`` falls through the prose
        # accumulator; the matched-backtick branch fires on the
        # inner span. Result is a bold link round-tripping to
        # ``<strong><a><code>abc1234</code></a></strong>``.
        out = linkify_shas_in_text("Bold **`abc1234`** here", _mock_resolve)
        assert out == "Bold **[`abc1234`](https://example.com/abc1234)** here"


# ---------------------------------------------------------------------------
# git_remote URL parsing


class TestParseGitUrl:
    def test_https_with_dot_git(self):
        assert _parse_git_url("https://github.com/owner/repo.git") == (
            "github.com",
            "owner/repo",
        )

    def test_https_no_dot_git(self):
        assert _parse_git_url("https://github.com/owner/repo") == (
            "github.com",
            "owner/repo",
        )

    def test_ssh(self):
        assert _parse_git_url("git@github.com:owner/repo.git") == (
            "github.com",
            "owner/repo",
        )

    def test_https_with_token(self):
        assert _parse_git_url("https://x-token@github.com/owner/repo.git") == (
            "github.com",
            "owner/repo",
        )

    def test_https_with_subgroup(self):
        # Multi-segment paths (GitLab subgroups) preserved verbatim.
        assert _parse_git_url("https://gitlab.com/group/sub/repo.git") == (
            "gitlab.com",
            "group/sub/repo",
        )

    def test_rejects_local_path(self):
        assert _parse_git_url("/srv/git/repo.git") is None

    def test_rejects_empty(self):
        assert _parse_git_url("") is None

    def test_rejects_no_owner(self):
        assert _parse_git_url("git@github.com:repo.git") is None

    # -- Multi-forge URL shapes the parser must handle uniformly --

    def test_gitlab_https(self):
        assert _parse_git_url("https://gitlab.com/group/project.git") == (
            "gitlab.com",
            "group/project",
        )

    def test_gitlab_ssh_with_subgroup(self):
        # GitLab supports arbitrary subgroup nesting; the path captures
        # everything between the host and the optional .git suffix.
        assert _parse_git_url("git@gitlab.com:group/sub/project.git") == (
            "gitlab.com",
            "group/sub/project",
        )

    def test_self_hosted_gitlab(self):
        # Self-hosted GitLab: host name is arbitrary (e.g.
        # git.bct-technology.com). Parser doesn't classify hosts —
        # just splits. Classification happens in resolve_sha against
        # the static map + env fallback.
        assert _parse_git_url(
            "git@git.bct-technology.com:BCT/claudecode/repoindexer.git"
        ) == ("git.bct-technology.com", "BCT/claudecode/repoindexer")

    def test_bitbucket_https(self):
        assert _parse_git_url("https://bitbucket.org/workspace/repo.git") == (
            "bitbucket.org",
            "workspace/repo",
        )


# ---------------------------------------------------------------------------
# canonical_cwd_from_messages


class _M:
    def __init__(self, cwd: str = ""):
        self.cwd = cwd


class TestCanonicalCwd:
    def test_picks_only_cwd(self):
        assert canonical_cwd_from_messages([_M("/a"), _M("/a")]) == "/a"

    def test_picks_most_common(self):
        msgs = [_M("/a"), _M("/b"), _M("/b"), _M("/c")]
        assert canonical_cwd_from_messages(msgs) == "/b"

    def test_returns_none_when_no_cwds(self):
        assert canonical_cwd_from_messages([_M(""), _M("")]) is None

    def test_returns_none_for_empty_list(self):
        assert canonical_cwd_from_messages([]) is None

    def test_skips_messages_without_cwd_attr(self):
        class NoCwd:
            pass

        assert canonical_cwd_from_messages([NoCwd(), _M("/x")]) == "/x"


# ---------------------------------------------------------------------------
# render_with_repo_context: ContextVar plumbing


class TestRenderRepoContext:
    def setup_method(self):
        clear_resolver_caches()

    def teardown_method(self):
        clear_resolver_caches()

    def test_outside_context_resolver_returns_none(self):
        # No active context → resolver returns None for any input.
        assert resolve_sha_for_current_render("abc1234") is None

    def test_context_is_reset_on_exit(self):
        with render_with_repo_context("/some/repo"):
            pass
        assert resolve_sha_for_current_render("abc1234") is None


# ---------------------------------------------------------------------------
# Forge templates: static map + env-var fallback
#
# The resolver's URL-shape logic is unit-tested here by stubbing the
# two git-backed helpers (``_git_remote_for``, ``_commit_for``). This
# isolates the host-classification + template-substitution logic
# from the actual git plumbing — ``TestRemoteCommitResolution`` covers
# commit lookup against throwaway repositories, and the integration
# class below end-to-end resolution against this repo's real origin/main.


class _StubResolverEnv:
    """Monkeypatch fixture: pin the git-backed helpers to known
    answers so resolve_sha tests just exercise the URL-shape logic.

    The two patched functions correspond to the two git boundaries
    inside resolve_sha:
    - ``_git_remote_for`` → (host, path) parsing
    - ``_commit_for`` → which commit id the link carries
    """

    def __init__(self, monkeypatch, host: str, path: str, full_sha: str):
        import claude_code_log.git_remote as gr

        clear_resolver_caches()
        monkeypatch.setattr(gr, "_git_remote_for", lambda _cwd: (host, path))
        monkeypatch.setattr(gr, "_commit_for", lambda _cwd, _sha: full_sha)


class TestStaticForgeMap:
    """``_HOST_URL_PATTERNS`` covers github / gitlab / bitbucket out of the box."""

    def teardown_method(self):
        clear_resolver_caches()

    def test_github_url(self, monkeypatch):
        _StubResolverEnv(monkeypatch, "github.com", "owner/repo", "0" * 40)
        url = resolve_sha("/fake/cwd", "abc1234")
        assert url == "https://github.com/owner/repo/commit/" + "0" * 40

    def test_gitlab_url_uses_dash_commit_segment(self):
        # GitLab's URL shape is ``host/path/-/commit/sha`` (the
        # ``/-/`` is the namespace separator). Pinning here so a
        # well-meaning refactor of the static map doesn't silently
        # collapse it back to ``/commit/``.
        # No subprocess needed since we go direct via the template.
        from claude_code_log.git_remote import _HOST_URL_PATTERNS

        assert (
            _HOST_URL_PATTERNS["gitlab.com"]
            == "https://gitlab.com/{path}/-/commit/{sha}"
        )

    def test_gitlab_url_end_to_end(self, monkeypatch):
        _StubResolverEnv(monkeypatch, "gitlab.com", "group/sub/repo", "f" * 40)
        url = resolve_sha("/fake/cwd", "fff1234")
        assert url == "https://gitlab.com/group/sub/repo/-/commit/" + "f" * 40

    def test_bitbucket_url_uses_commits_plural(self):
        # Bitbucket's URL shape is ``host/path/commits/sha`` (plural).
        # Same pinning rationale as the GitLab case above.
        from claude_code_log.git_remote import _HOST_URL_PATTERNS

        assert (
            _HOST_URL_PATTERNS["bitbucket.org"]
            == "https://bitbucket.org/{path}/commits/{sha}"
        )

    def test_bitbucket_url_end_to_end(self, monkeypatch):
        _StubResolverEnv(monkeypatch, "bitbucket.org", "ws/repo", "1" * 40)
        url = resolve_sha("/fake/cwd", "1111111")
        assert url == "https://bitbucket.org/ws/repo/commits/" + "1" * 40

    def test_unknown_host_no_fallback_returns_none(self, monkeypatch):
        # Self-hosted forge with no env-var fallback → None.
        monkeypatch.delenv("CLAUDE_CODE_LOG_GIT_LINK", raising=False)
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "a" * 40)
        assert resolve_sha("/fake/cwd", "aaa1234") is None


class TestFallbackTemplate:
    """``CLAUDE_CODE_LOG_GIT_LINK`` covers self-hosted forges."""

    def teardown_method(self):
        clear_resolver_caches()

    def test_fallback_substitutes_host_path_sha(self, monkeypatch):
        monkeypatch.setenv(
            "CLAUDE_CODE_LOG_GIT_LINK",
            "https://{host}/{path}/-/commit/{sha}",
        )
        _StubResolverEnv(monkeypatch, "git.bct-technology.com", "BCT/x/repo", "b" * 40)
        url = resolve_sha("/fake/cwd", "bbb1234")
        assert url == "https://git.bct-technology.com/BCT/x/repo/-/commit/" + "b" * 40

    def test_static_map_wins_over_fallback(self, monkeypatch):
        # Even with a "broken" fallback set, the static map's
        # github.com entry takes precedence — guard against a
        # refactor accidentally reversing the lookup order.
        monkeypatch.setenv(
            "CLAUDE_CODE_LOG_GIT_LINK",
            "https://WRONG/{host}/{path}/{sha}",
        )
        _StubResolverEnv(monkeypatch, "github.com", "owner/repo", "c" * 40)
        url = resolve_sha("/fake/cwd", "ccc1234")
        assert url == "https://github.com/owner/repo/commit/" + "c" * 40
        assert "WRONG" not in url

    def test_fallback_missing_sha_placeholder_is_silent_skip(self, monkeypatch):
        # Defence-in-depth: the CLI handler errors loudly on a
        # template without {sha}, but if the env var is set directly
        # with a broken template, the resolver degrades to "no link"
        # rather than crashing rendering.
        monkeypatch.setenv("CLAUDE_CODE_LOG_GIT_LINK", "https://nope/{path}")
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "d" * 40)
        assert resolve_sha("/fake/cwd", "ddd1234") is None

    def test_empty_fallback_is_silent_skip(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_LOG_GIT_LINK", "")
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "e" * 40)
        assert resolve_sha("/fake/cwd", "eee1234") is None

    def test_fallback_with_only_sha_placeholder(self, monkeypatch):
        # Minimal valid template: only ``{sha}``. Documents that
        # ``{host}`` / ``{path}`` are optional from the resolver's
        # standpoint — a user could supply a fully-qualified URL
        # stub if they had reason to.
        monkeypatch.setenv(
            "CLAUDE_CODE_LOG_GIT_LINK", "https://example.test/commit/{sha}"
        )
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "9" * 40)
        url = resolve_sha("/fake/cwd", "999abcd")
        assert url == "https://example.test/commit/" + "9" * 40

    def test_fallback_unknown_placeholder_is_silent_skip(self, monkeypatch):
        # Regression for monk's blocking finding on this PR. A user
        # who typos ``{hsot}`` (instead of ``{host}``) passes the
        # ``{sha}``-presence check in ``_fallback_template()`` but
        # would crash ``template.format()`` with KeyError. The
        # resolver wraps that in try/except and degrades to None.
        # The CLI path catches this loudly via the placeholder
        # whitelist (see TestGitLinkTemplateValidation below); this
        # test guards the env-var-only path.
        monkeypatch.setenv(
            "CLAUDE_CODE_LOG_GIT_LINK", "https://{hsot}/{path}/-/commit/{sha}"
        )
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "8" * 40)
        assert resolve_sha("/fake/cwd", "888abcd") is None

    def test_fallback_positional_placeholder_is_silent_skip(self, monkeypatch):
        # ``{0}`` would raise IndexError at format time — same
        # silent-skip degradation as the KeyError case.
        monkeypatch.setenv("CLAUDE_CODE_LOG_GIT_LINK", "https://{0}/{sha}")
        _StubResolverEnv(monkeypatch, "git.internal.example", "team/repo", "7" * 40)
        assert resolve_sha("/fake/cwd", "777abcd") is None


class TestGitLinkTemplateValidation:
    """Unit tests for the CLI-side ``_validate_git_link_template`` helper.

    Loud-error path for users who pass ``--git-link`` directly. The
    env-var-only path (no CLI) instead silently degrades via the
    resolver's try/except — see ``TestFallbackTemplate`` above.
    """

    def test_missing_sha_raises_usage_error(self):
        import click
        from claude_code_log.cli import _validate_git_link_template

        with pytest.raises(click.UsageError, match=r"must contain a \{sha\}"):
            _validate_git_link_template("https://example.test/no-placeholders")

    def test_unknown_placeholder_raises_usage_error(self):
        # The typo-catching case monk flagged. ``{hsot}`` ≠ ``{host}``.
        import click
        from claude_code_log.cli import _validate_git_link_template

        with pytest.raises(click.UsageError, match=r"unknown placeholder.*hsot"):
            _validate_git_link_template("https://{hsot}/{path}/-/commit/{sha}")

    def test_positional_placeholder_raises_usage_error(self):
        # ``{0}`` parses as a placeholder named ``"0"`` — not in the
        # whitelist, so it's flagged as unknown.
        import click
        from claude_code_log.cli import _validate_git_link_template

        with pytest.raises(click.UsageError, match=r"unknown placeholder"):
            _validate_git_link_template("https://{0}/{sha}")

    def test_anonymous_positional_placeholder_raises_usage_error(self):
        # CR-flagged contract hole: ``{}`` parses as a placeholder with
        # field == "" and would crash at ``.format()`` time. Catch it
        # at CLI validation so users get a precise error instead of a
        # silent skip (env-var path) or a delayed crash (resolver).
        import click
        from claude_code_log.cli import _validate_git_link_template

        with pytest.raises(click.UsageError, match=r"anonymous positional"):
            _validate_git_link_template("https://example.test/{}/commit/{sha}")

    def test_all_three_placeholders_passes(self):
        from claude_code_log.cli import _validate_git_link_template

        _validate_git_link_template("https://{host}/{path}/-/commit/{sha}")
        # No exception → pass.

    def test_only_sha_passes(self):
        from claude_code_log.cli import _validate_git_link_template

        _validate_git_link_template("https://example.test/commit/{sha}")

    def test_host_and_sha_no_path_passes(self):
        from claude_code_log.cli import _validate_git_link_template

        _validate_git_link_template("https://{host}/commit/{sha}")


class TestGitLinkCliOption:
    """``--git-link`` flag end-to-end wiring: validation surfaces, env-var sync."""

    def teardown_method(self):
        # Don't leak the env var across tests.
        os.environ.pop("CLAUDE_CODE_LOG_GIT_LINK", None)

    def test_missing_sha_placeholder_raises_usage_error(self):
        # End-to-end: confirms the CLI hooks the validator in. The
        # validator unit tests above cover the validation logic
        # itself; this is the integration smoke.
        from click.testing import CliRunner
        from claude_code_log.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--git-link",
                "https://example.test/commit/no-placeholder",
                "/dev/null",
            ],
        )
        # Click usage errors exit with 2.
        assert result.exit_code == 2
        assert "must contain a {sha} placeholder" in result.output


# ---------------------------------------------------------------------------
# Commit lookup: one ``git rev-list --remotes`` per cwd (issue #327)
#
# Throwaway repositories rather than this one, so the pushed / local-only
# split is known exactly: ``origin/main`` is set with ``update-ref``, no
# network and no real remote involved.


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Repo:
    """A repository whose ``origin`` is github.com/owner/repo, with two
    commits on ``origin/main`` and one local-only commit on top."""

    def __init__(self, path: Path, *init_args: str):
        self.path = path
        _git(path, "init", "-q", *init_args)
        _git(path, "remote", "add", "origin", "https://github.com/owner/repo.git")
        self.pushed = [_commit(path, "one"), _commit(path, "two")]
        _git(path, "update-ref", "refs/remotes/origin/main", self.pushed[-1])
        self.local = _commit(path, "local only")

    @property
    def cwd(self) -> str:
        return str(self.path)

    def absent_prefix(self, candidate: str) -> str:
        """``candidate``, after checking no commit here starts with it."""
        assert not any(c.startswith(candidate) for c in [*self.pushed, self.local]), (
            f"{candidate} happens to prefix a commit in the test repo"
        )
        return candidate


def _url(sha: str) -> str:
    return "https://github.com/owner/repo/commit/" + sha


class _GitCalls:
    """Count ``git`` subcommands the resolver runs, passing them through."""

    def __init__(self, monkeypatch, fail: Optional[str] = None):
        import claude_code_log.git_remote as gr

        self.subcommands: list[str] = []
        self.fail = fail
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            # argv is ["git", "-C", cwd, <subcommand>, ...]
            self.subcommands.append(argv[3])
            if argv[3] == self.fail:
                raise subprocess.TimeoutExpired(argv, 30)
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(gr.subprocess, "run", run)

    def count(self, subcommand: str) -> int:
        return self.subcommands.count(subcommand)


class TestRemoteCommitResolution:
    def setup_method(self):
        clear_resolver_caches()

    def teardown_method(self):
        clear_resolver_caches()

    def test_pushed_commit_resolves_from_short_or_full_sha(self, tmp_path):
        repo = _Repo(tmp_path)
        for commit in repo.pushed:
            assert resolve_sha(repo.cwd, commit[:7]) == _url(commit)
            assert resolve_sha(repo.cwd, commit) == _url(commit)

    def test_local_only_commit_stays_plain(self, tmp_path):
        repo = _Repo(tmp_path)
        assert resolve_sha(repo.cwd, repo.local[:7]) is None
        assert resolve_sha(repo.cwd, repo.local) is None

    def test_tokens_that_are_not_commits_stay_plain(self, tmp_path):
        repo = _Repo(tmp_path)
        for token in ("deadbee", "1234567", "a2be84b6ebd150622"):
            assert resolve_sha(repo.cwd, repo.absent_prefix(token)) is None

    def test_one_rev_list_answers_every_candidate(self, tmp_path, monkeypatch):
        """The #327 cost: candidates, found or not, spawn no ``git``.

        Before, each distinct candidate ran ``git branch -r --contains``
        (plus ``rev-parse`` when found) — and over a third of real
        candidates are not commits, so most of that proved a negative.
        """
        repo = _Repo(tmp_path)
        calls = _GitCalls(monkeypatch)
        candidates = [
            repo.pushed[0][:7],
            repo.pushed[1],
            repo.local[:9],
            *(repo.absent_prefix(f"{n:07d}") for n in range(10)),
            repo.absent_prefix("deadbee"),
        ]
        for candidate in candidates:
            resolve_sha(repo.cwd, candidate)
        assert calls.count("rev-list") == 1
        assert calls.subcommands == ["config", "rev-list"]

    def test_repository_with_no_remote_refs_links_nothing(self, tmp_path):
        """Nothing fetched, so nothing is pushed, so nothing links."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "remote", "add", "origin", "https://github.com/owner/repo")
        commit = _commit(tmp_path, "never fetched")
        assert resolve_sha(str(tmp_path), commit[:7]) is None
        assert resolve_sha(str(tmp_path), "deadbee") is None

    def test_unreadable_list_links_nothing_until_a_read_succeeds(
        self, tmp_path, monkeypatch
    ):
        """A failed ``rev-list`` leaves every token plain text — not
        linked unvalidated, which would make each non-commit token a
        dead link — and a later stale miss retries the read."""
        import claude_code_log.git_remote as gr

        repo = _Repo(tmp_path)
        calls = _GitCalls(monkeypatch, fail="rev-list")
        for token in (repo.pushed[0][:7], repo.pushed[1], "deadbee"):
            assert resolve_sha(repo.cwd, token) is None
        assert calls.count("rev-list") == 1

        calls.fail = None
        start = gr._now()
        monkeypatch.setattr(
            gr, "_now", lambda: start + gr._REMOTE_COMMITS_MAX_AGE_SECONDS + 1
        )
        assert resolve_sha(repo.cwd, repo.pushed[0][:7]) == _url(repo.pushed[0])
        assert calls.count("rev-list") == 2

    def test_only_sha_shaped_tokens_link(self, tmp_path):
        """The shape contract holds even for tokens the prefix search
        would accept: uppercase hex and abbreviations under 7 chars."""
        repo = _Repo(tmp_path)
        pushed = repo.pushed[0]
        # Uppercasing only changes a prefix with a letter in it; an
        # all-digit 7-char prefix (~4% of SHAs) is a valid token as is.
        with_letter = next(
            (pushed[:n] for n in range(7, 41) if not pushed[:n].isdigit()), pushed
        )
        assert with_letter.upper() != with_letter, "all-digit SHA"
        assert resolve_sha(repo.cwd, with_letter) == _url(pushed)
        for token in (with_letter.upper(), pushed[:6], pushed[:7] + " "):
            assert resolve_sha(repo.cwd, token) is None

    def test_commit_fetched_later_is_found_once_the_list_is_stale(
        self, tmp_path, monkeypatch
    ):
        import claude_code_log.git_remote as gr

        repo = _Repo(tmp_path)
        assert resolve_sha(repo.cwd, repo.pushed[0][:7]) is not None
        # "Fetch" a new commit after the list was read.
        fetched = _commit(tmp_path, "fetched later")
        _git(tmp_path, "update-ref", "refs/remotes/origin/main", fetched)
        calls = _GitCalls(monkeypatch)

        # Within the trust window a miss does not re-read: that is what
        # keeps non-commit tokens from spawning a walk each.
        assert resolve_sha(repo.cwd, fetched[:7]) is None
        assert calls.count("rev-list") == 0

        start = gr._now()
        monkeypatch.setattr(
            gr, "_now", lambda: start + gr._REMOTE_COMMITS_MAX_AGE_SECONDS + 1
        )
        # A hit never re-reads, stale or not ...
        assert resolve_sha(repo.cwd, repo.pushed[0][:7]) is not None
        assert calls.count("rev-list") == 0
        # ... a miss on a stale list re-reads once, and finds the commit.
        assert resolve_sha(repo.cwd, fetched[:7]) == _url(fetched)
        assert calls.count("rev-list") == 1

    def test_sha256_repository(self, tmp_path):
        try:
            repo = _Repo(tmp_path, "--object-format=sha256")
        except subprocess.CalledProcessError:
            pytest.skip("git without SHA-256 repository support")
        assert len(repo.pushed[0]) == 64
        assert resolve_sha(repo.cwd, repo.pushed[0][:7]) == _url(repo.pushed[0])
        assert resolve_sha(repo.cwd, repo.local[:7]) is None

    def test_held_lists_are_bounded_per_cwd(self, monkeypatch):
        import claude_code_log.git_remote as gr

        reads: list[str] = []

        def read(cwd: str) -> gr._RemoteCommits:
            reads.append(cwd)
            return gr._RemoteCommits(
                ids=b"", width=20, read_at=gr._now(), trusted_for=60.0
            )

        monkeypatch.setattr(gr, "_read_remote_commits", read)
        monkeypatch.setattr(gr, "_REMOTE_COMMITS_MAX_CWDS", 2)
        for cwd in ("a", "b", "a", "c", "a", "b"):
            gr._remote_commits(cwd)
        # "a" is refreshed by use, so "c" evicts "b", which is re-read.
        assert reads == ["a", "b", "c", "b"]


class TestRemoteCommitsFind:
    """Prefix lookup in the sorted id list, on synthetic ids."""

    @staticmethod
    def _commits(*hexes: str):
        from claude_code_log.git_remote import _RemoteCommits

        return _RemoteCommits(
            ids=b"".join(sorted(bytes.fromhex(h) for h in hexes)),
            width=20,
            read_at=0.0,
            trusted_for=float("inf"),
        )

    def test_ambiguous_prefix_is_unresolved(self):
        a = "abcdef0" + "1" * 33
        b = "abcdef0" + "2" * 33
        commits = self._commits("0" * 40, a, b, "f" * 40)
        assert commits.find("abcdef0") is None
        assert commits.find("abcdef01") == a
        assert commits.find("abcdef02") == b

    def test_first_last_and_absent(self):
        low, mid, high = "0" * 40, "5" * 40, "f" * 40
        commits = self._commits(high, mid, low)
        assert commits.find("0000000") == low
        assert commits.find("5555555") == mid
        assert commits.find("fffffff") == high
        assert commits.find("1111111") is None
        assert commits.find("5555556") is None

    def test_odd_length_prefix_matches_on_the_nibble(self):
        commits = self._commits("abcdef7" + "0" * 33, "abcdef8" + "0" * 33)
        assert commits.find("abcdef7") == "abcdef7" + "0" * 33
        assert commits.find("abcdef8") == "abcdef8" + "0" * 33

    def test_empty_and_unreadable_lists_find_nothing(self):
        from claude_code_log.git_remote import _RemoteCommits

        assert self._commits().find("abcdef0") is None
        unreadable = _RemoteCommits(
            ids=None, width=20, read_at=0.0, trusted_for=float("inf")
        )
        assert unreadable.find("abcdef0") is None


# ---------------------------------------------------------------------------
# Integration: the project's own git repo
#
# This repo has the commit 7c2e6f6 on origin/main (the sidechain
# filter dashed-border commit, used as a real fixture in main's task
# description). If the test environment doesn't have origin set to
# the daaain/claude-code-log GitHub remote (e.g. a fork), the test
# skips.


def _project_repo_origin_is_daaain_repo() -> bool:
    """Skip integration tests if origin doesn't point at the canonical repo."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=Path(__file__).parent.parent,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return "daaain/claude-code-log" in result.stdout


_KNOWN_LOCAL_SHA = "7c2e6f6"  # On origin/main as of writing.


@pytest.mark.skipif(
    not _project_repo_origin_is_daaain_repo(),
    reason="origin not set to daaain/claude-code-log; skipping integration",
)
class TestIntegrationLocalRepo:
    def setup_method(self):
        clear_resolver_caches()

    def teardown_method(self):
        clear_resolver_caches()

    def test_resolve_sha_returns_github_url(self):
        cwd = str(Path(__file__).parent.parent)
        url = resolve_sha(cwd, _KNOWN_LOCAL_SHA)
        assert url is not None
        assert url.startswith("https://github.com/daaain/claude-code-log/commit/")
        # Full 40-char SHA in the URL.
        assert len(url.rsplit("/", 1)[-1]) == 40

    def test_resolve_sha_returns_none_for_unknown(self):
        cwd = str(Path(__file__).parent.parent)
        # Plausible-looking SHA that doesn't exist in the repo.
        assert resolve_sha(cwd, "deadbeefcafe1234567890abcdef1234567890ab") is None

    def test_html_render_with_repo_context_produces_anchor(self):
        cwd = str(Path(__file__).parent.parent)
        with render_with_repo_context(cwd):
            html = render_markdown(f"See commit {_KNOWN_LOCAL_SHA} for details.")
        assert '<a href="https://github.com/daaain/claude-code-log/commit/' in html
        assert f">{_KNOWN_LOCAL_SHA}</a>" in html

    def test_user_html_render_with_repo_context_produces_anchor(self):
        cwd = str(Path(__file__).parent.parent)
        with render_with_repo_context(cwd):
            html = render_user_markdown(f"Check {_KNOWN_LOCAL_SHA} please")
        assert '<a href="https://github.com/daaain/claude-code-log/commit/' in html

    def test_markdown_text_helper_with_repo_context(self):
        cwd = str(Path(__file__).parent.parent)
        with render_with_repo_context(cwd):
            md = linkify_shas_in_text(
                f"diff at {_KNOWN_LOCAL_SHA} introduces it",
                resolve_sha_for_current_render,
            )
        assert (
            f"[{_KNOWN_LOCAL_SHA}](https://github.com/daaain/claude-code-log/commit/"
            in md
        )

    def test_codespan_sha_becomes_linked_codespan(self):
        # Updated contract (codespan-SHA feature): a single-backtick
        # codespan whose body is exactly a resolvable SHA gets the
        # codespan preserved *and* wrapped in a link. The earlier
        # contract — codespans stay opaque — was inverted on purpose
        # so authors who write ``\`5baac35\``` to typographically
        # quote a SHA still get a clickable commit link.
        cwd = str(Path(__file__).parent.parent)
        with render_with_repo_context(cwd):
            html = render_markdown(f"`{_KNOWN_LOCAL_SHA}` is a code span")
        assert f"<code>{_KNOWN_LOCAL_SHA}</code>" in html
        assert '<a href="https://github.com/daaain/claude-code-log/commit/' in html
        # Mixed-body codespan still stays opaque (regression guard for
        # the "only exact SHAs" half of the contract).
        with render_with_repo_context(cwd):
            mixed = render_markdown(f"`git show {_KNOWN_LOCAL_SHA}` should stay code")
        assert "<a" not in mixed

    def test_no_context_produces_plain_text(self):
        # Without entering the context, even a real reachable SHA
        # renders as plain text. This is the behavioural baseline:
        # rendering paths that don't bind a cwd must not surprise
        # the user with surprise links.
        html = render_markdown(f"See commit {_KNOWN_LOCAL_SHA} for details.")
        assert "<a " not in html
        assert _KNOWN_LOCAL_SHA in html
