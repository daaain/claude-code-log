"""Tests for the byte-bounded memo caches on the pure render leaves.

The caches exist because a project conversion formats every message twice
(once for its combined page, once for its ``session-*.html``). They are
only safe as long as the memoized functions really are pure with respect
to their keys — the interesting case being Markdown, whose SHA-linkifier
plugin makes output depend on the active render repo cwd. These tests pin
both the eviction mechanics and that keying contract.
"""

import pytest

from claude_code_log import render_cache
from claude_code_log.git_remote import render_with_repo_context
from claude_code_log.html.renderer_code import highlight_code_with_pygments
from claude_code_log.html.utils import render_markdown, render_user_markdown
from claude_code_log.render_cache import ByteBoundedCache


@pytest.fixture(autouse=True)
def _clean_caches():
    """Each test starts from empty module-level caches and leaves them empty."""
    render_cache.clear_all()
    yield
    render_cache.clear_all()


class TestByteBoundedCache:
    def test_round_trips_a_value(self):
        cache = ByteBoundedCache(budget_bytes=1024)
        assert cache.get("k") is None
        cache.put("k", "value")
        assert cache.get("k") == "value"
        assert cache.stats()["hits"] == 1
        assert cache.stats()["misses"] == 1

    def test_evicts_least_recently_used_when_over_budget(self):
        # The per-entry ceiling is 1/8 of the budget, so a full cache
        # holds 8 max-size entries: 8 * 50 exactly fills a 400-byte budget.
        cache = ByteBoundedCache(budget_bytes=400)
        keys = "abcdefgh"
        for key in keys:
            cache.put(key, key * 50)
        assert cache.stats()["entries"] == len(keys)

        # Touch "a" so "b" becomes the least-recently-used entry.
        assert cache.get("a") is not None
        cache.put("i", "i" * 50)

        assert cache.get("a") is not None, "recently used entry must survive"
        assert cache.get("b") is None, "least-recently-used entry is evicted"
        assert cache.get("i") is not None

    def test_refuses_entries_larger_than_the_per_entry_ceiling(self):
        # One oversized value must not be admitted, or it would evict
        # every cheap entry behind it and thrash the cache to 0% hits.
        cache = ByteBoundedCache(budget_bytes=1000)
        cache.put("small", "s" * 10)
        cache.put("huge", "h" * 900)

        assert cache.get("huge") is None
        assert cache.get("small") is not None

    def test_budget_of_zero_disables_the_cache(self):
        cache = ByteBoundedCache(budget_bytes=0)
        assert not cache.enabled
        cache.put("k", "value")
        assert cache.get("k") is None

    def test_reinserting_a_key_does_not_double_count_its_bytes(self):
        cache = ByteBoundedCache(budget_bytes=1000)
        cache.put("k", "x" * 50)
        cache.put("k", "y" * 50)
        assert cache.stats()["bytes"] == 50
        assert cache.get("k") == "y" * 50


class TestEnvironmentConfiguration:
    def test_cache_mb_env_var_sets_the_budget(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_LOG_RENDER_CACHE_MB", "4")
        cache = ByteBoundedCache()
        assert cache.enabled
        assert cache.budget_bytes == 4 * 1024 * 1024

    def test_zero_disables_memoization(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_LOG_RENDER_CACHE_MB", "0")
        assert not ByteBoundedCache().enabled

    def test_unparseable_value_falls_back_to_the_default(self, monkeypatch):
        # An unreadable setting should degrade to the built-in budget,
        # not crash a conversion.
        monkeypatch.setenv("CLAUDE_CODE_LOG_RENDER_CACHE_MB", "not-a-number")
        cache = ByteBoundedCache()
        assert cache.budget_bytes == render_cache.DEFAULT_CACHE_MB * 1024 * 1024


class TestPygmentsMemo:
    def test_repeat_call_hits_the_cache_and_returns_identical_html(self):
        code = "def f():\n    return 1\n"
        first = highlight_code_with_pygments(code, "example.py")
        assert render_cache.pygments_cache.stats()["hits"] == 0

        second = highlight_code_with_pygments(code, "example.py")
        assert second == first
        assert render_cache.pygments_cache.stats()["hits"] == 1

    def test_arguments_participate_in_the_key(self):
        code = "x = 1\n"
        # Same code, different lexer / line-number settings must not
        # collide — each is a distinct rendering.
        variants = [
            highlight_code_with_pygments(code, "a.py"),
            highlight_code_with_pygments(code, "a.txt"),
            highlight_code_with_pygments(code, "a.py", show_linenos=False),
            highlight_code_with_pygments(code, "a.py", linenostart=7),
        ]
        assert len(set(variants)) == len(variants)
        assert render_cache.pygments_cache.stats()["hits"] == 0


class TestMarkdownMemo:
    def test_repeat_call_hits_the_cache(self):
        text = "# Title\n\nSome *body* text.\n"
        first = render_markdown(text)
        second = render_markdown(text)
        assert second == first
        assert render_cache.markdown_cache.stats()["hits"] == 1

    def test_repo_cwd_participates_in_the_key(self, tmp_path):
        """Two repos must not share a Markdown cache entry.

        The SHA-linkifier resolves commit hashes against the active render
        cwd, so identical text can legitimately render different links in
        different projects. Keying on text alone would serve one project's
        commit URLs inside another's page.
        """
        text = "See 5baac35 for details.\n"
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        repo_a.mkdir()
        repo_b.mkdir()

        with render_with_repo_context(str(repo_a)):
            render_markdown(text)
        with render_with_repo_context(str(repo_b)):
            render_markdown(text)

        stats = render_cache.markdown_cache.stats()
        assert stats["hits"] == 0, "distinct repo cwds must not share an entry"
        assert stats["misses"] == 2

        # Re-entering the first context still hits its own entry.
        with render_with_repo_context(str(repo_a)):
            render_markdown(text)
        assert render_cache.markdown_cache.stats()["hits"] == 1

    def test_the_two_renderer_singletons_do_not_share_entries(self):
        """``render_markdown`` and ``render_user_markdown`` are distinct.

        They use separate mistune singletons; sharing a cache entry would
        let one's output be served for the other's call site.
        """
        text = "plain paragraph\n"
        render_markdown(text)
        render_user_markdown(text)
        assert render_cache.markdown_cache.stats()["hits"] == 0
        assert render_cache.markdown_cache.stats()["misses"] == 2
