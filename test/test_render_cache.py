"""Tests for the byte-bounded memo caches on the pure render leaves.

The caches exist because a project conversion formats every message twice
(once for its combined page, once for its ``session-*.html``). They are
only safe as long as the memoized functions really are pure with respect
to their keys — the interesting case being Markdown, whose SHA-linkifier
plugin makes output depend on the active render repo cwd. These tests pin
both the eviction mechanics and that keying contract.
"""

import os

import pytest

from claude_code_log import converter, render_cache
from claude_code_log.models import DEFAULT_DEPTH
from claude_code_log import render_pool as render_pool_module
from claude_code_log.render_pool import (
    RENDER_JOBS_ENV,
    memory_capped_workers,
    resolve_render_jobs,
)
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


class TestRenderJobsEnvironment:
    """The render fan-out is opt-in; ``--jobs`` alone must not enable it."""

    def test_unset_means_serial(self, monkeypatch):
        monkeypatch.delenv(RENDER_JOBS_ENV, raising=False)
        assert resolve_render_jobs(None) == 1

    def test_auto_means_cpu_count(self, monkeypatch):
        monkeypatch.setenv(RENDER_JOBS_ENV, "auto")
        assert resolve_render_jobs(None) == max(1, os.cpu_count() or 1)

    def test_explicit_worker_count(self, monkeypatch):
        monkeypatch.setenv(RENDER_JOBS_ENV, "3")
        assert resolve_render_jobs(None) == 3

    def test_unparseable_or_empty_means_serial(self, monkeypatch):
        # Off is the safe reading of a broken setting: the fan-out is a
        # performance trade, never a correctness requirement.
        for value in ("", "   ", "yes", "-1"):
            monkeypatch.setenv(RENDER_JOBS_ENV, value)
            assert resolve_render_jobs(None) == 1, value

    def test_explicit_argument_overrides_the_environment(self, monkeypatch):
        monkeypatch.setenv(RENDER_JOBS_ENV, "8")
        assert resolve_render_jobs(2) == 2
        monkeypatch.delenv(RENDER_JOBS_ENV, raising=False)
        assert resolve_render_jobs(2) == 2


class TestRenderPoolCreation:
    """`_make_render_pool` is the gate; everything downstream assumes it."""

    @staticmethod
    def _make(tmp_path, monkeypatch=None, **overrides):
        # Pin available memory so these assertions are about the gate being
        # tested, not about how much RAM the machine running them has.
        if monkeypatch is not None:
            monkeypatch.setattr(
                render_pool_module, "_available_memory_bytes", lambda: 64 * 1024**3
            )
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        kwargs = dict(
            format="html",
            input_path=project,
            effective_output_dir=project,
            cache_manager=object(),
            message_count=1_000_000,
            from_date=None,
            to_date=None,
            depth=DEFAULT_DEPTH,
            compact=False,
            no_timestamps=False,
            no_recaps=False,
            image_export_mode=None,
            archive_search_link=None,
            render_jobs=None,
        )
        kwargs.update(overrides)
        return converter._make_render_pool(**kwargs)  # type: ignore[arg-type]

    def test_no_pool_without_the_env_var(self, tmp_path, monkeypatch):
        """The whole point of the default: no worker processes at all."""
        monkeypatch.delenv(RENDER_JOBS_ENV, raising=False)
        assert self._make(tmp_path) is None

    def test_env_var_creates_a_pool(self, tmp_path, monkeypatch):
        monkeypatch.setenv(RENDER_JOBS_ENV, "2")
        assert self._make(tmp_path, monkeypatch) is not None

    def test_no_pool_when_memory_is_too_tight(self, tmp_path, monkeypatch):
        """A machine that can't hold a second copy renders serially.

        This is the guard that stops a large archive from driving the box
        into swap; the fan-out is an optimisation, never a requirement.
        """
        monkeypatch.setenv(RENDER_JOBS_ENV, "4")
        monkeypatch.setattr(
            render_pool_module, "_available_memory_bytes", lambda: 256 * 1024**2
        )
        assert self._make(tmp_path) is None

    def test_small_projects_stay_serial_even_when_enabled(self, tmp_path, monkeypatch):
        """Below the crossover the fan-out is a measured regression."""
        monkeypatch.setenv(RENDER_JOBS_ENV, "4")
        assert (
            self._make(
                tmp_path,
                monkeypatch,
                message_count=converter._MIN_MESSAGES_FOR_RENDER_POOL - 1,
            )
            is None
        )

    def test_referenced_images_stay_serial(self, tmp_path, monkeypatch):
        """Concurrent renders would collide on images/image_NNNN.png."""
        monkeypatch.setenv(RENDER_JOBS_ENV, "4")
        assert self._make(tmp_path, monkeypatch, image_export_mode="referenced") is None

    def test_no_pool_without_a_cache_manager(self, tmp_path, monkeypatch):
        """Workers reload from cache; without one they'd re-parse every file."""
        monkeypatch.setenv(RENDER_JOBS_ENV, "4")
        assert self._make(tmp_path, monkeypatch, cache_manager=None) is None

    def test_no_pool_for_single_file_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv(RENDER_JOBS_ENV, "4")
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("")
        assert self._make(tmp_path, monkeypatch, input_path=transcript) is None


class TestMemoryCap:
    """Peak memory is workers x project size, so the cap is load-bearing.

    Each worker holds the project's whole transcript (~3x its bytes on
    disk). Without a cap, `auto` on a large archive exhausts RAM and drives
    the machine into swap, where it pegs every core and stops responding —
    a much worse outcome than rendering serially.
    """

    @staticmethod
    def _with_memory(monkeypatch, available_bytes):
        monkeypatch.setattr(
            render_pool_module, "_available_memory_bytes", lambda: available_bytes
        )

    def test_plentiful_memory_grants_the_request(self, monkeypatch):
        self._with_memory(monkeypatch, 64 * 1024**3)
        assert memory_capped_workers(8, 100 * 1024**2) == 8

    def test_tight_memory_falls_back_to_serial(self, monkeypatch):
        # 2GB available against a 1GB project (~3GB per worker).
        self._with_memory(monkeypatch, 2 * 1024**3)
        assert memory_capped_workers(8, 1024**3) == 1

    def test_bigger_projects_get_fewer_workers(self, monkeypatch):
        self._with_memory(monkeypatch, 32 * 1024**3)
        small = memory_capped_workers(16, 50 * 1024**2)
        large = memory_capped_workers(16, 2 * 1024**3)
        assert small > large >= 1

    def test_concurrent_projects_divide_the_budget(self, monkeypatch):
        """The two pool levels multiply, so the budget splits before it's spent."""
        self._with_memory(monkeypatch, 32 * 1024**3)
        alone = memory_capped_workers(16, 200 * 1024**2)
        shared = memory_capped_workers(16, 200 * 1024**2, concurrent_projects=8)
        assert shared < alone

    def test_unknown_memory_is_conservative(self, monkeypatch):
        self._with_memory(monkeypatch, None)
        assert memory_capped_workers(16, 100 * 1024**2) == 2

    def test_a_request_of_one_stays_one(self, monkeypatch):
        self._with_memory(monkeypatch, 64 * 1024**3)
        assert memory_capped_workers(1, 1) == 1
