"""Tests for combined transcript link functionality in session HTML generation."""

import tempfile
from pathlib import Path
from types import SimpleNamespace


from claude_code_log.cache import CacheManager
from claude_code_log.html.renderer import generate_session_html


class TestCombinedTranscriptLink:
    """Test the combined transcript link functionality in session HTML generation."""

    def test_no_combined_link_without_cache_manager(self):
        """Test that no combined transcript link appears without cache manager."""
        messages = []
        session_id = "test-session-123"

        html = generate_session_html(messages, session_id, "Test Session")

        assert "← View All Sessions (Combined Transcript)" not in html
        assert 'href="combined_transcripts.html"' not in html

    def test_no_combined_link_with_empty_cache(self):
        """Test that no combined transcript link appears with empty cache."""
        messages = []
        session_id = "test-session-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_manager = CacheManager(Path(tmpdir), "1.0.0")
            # Empty cache - no project data

            html = generate_session_html(
                messages, session_id, "Test Session", cache_manager
            )

            assert "← View All Sessions (Combined Transcript)" not in html

    def test_combined_link_with_valid_cache(self):
        """Test that combined transcript link appears with valid cache data."""
        messages = []
        session_id = "test-session-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_manager = CacheManager(Path(tmpdir), "1.0.0")
            # Mock project data with sessions
            mock_project_data = SimpleNamespace()
            mock_project_data.sessions = {session_id: object()}
            cache_manager.get_cached_project_data = lambda: mock_project_data  # type: ignore

            html = generate_session_html(
                messages, session_id, "Test Session", cache_manager
            )

            # Verify combined transcript link elements are present.
            # generate_session_html renders at the library default (FULL), so
            # the same-variant combined link carries FULL's suffix (.hook, the
            # --depth name for full depth; #159) — not the bare default file.
            assert "← View All Sessions (Combined Transcript)" in html
            assert 'href="combined_transcripts.hook.html"' in html

            # Verify the navigation structure
            assert '<div class="navigation">' in html
            assert (
                '<a href="combined_transcripts.hook.html"'
                ' class="combined-transcript-link">' in html
            )

    def test_combined_link_exception_handling(self):
        """Test that exceptions in cache access are handled gracefully."""
        messages = []
        session_id = "test-session-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_manager = CacheManager(Path(tmpdir), "1.0.0")
            # Mock cache manager that will raise exception
            cache_manager.get_cached_project_data = lambda: exec(  # type: ignore
                'raise Exception("Test exception")'
            )

            # Should not crash and should not show combined link
            html = generate_session_html(
                messages, session_id, "Test Session", cache_manager
            )

            assert "← View All Sessions (Combined Transcript)" not in html

    def test_combined_link_css_styling(self):
        """Test that combined transcript link includes proper CSS classes."""
        messages = []
        session_id = "test-session-123"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_manager = CacheManager(Path(tmpdir), "1.0.0")
            mock_project_data = SimpleNamespace()
            mock_project_data.sessions = {session_id: object()}
            cache_manager.get_cached_project_data = lambda: mock_project_data  # type: ignore

            html = generate_session_html(
                messages, session_id, "Test Session", cache_manager
            )

            # Verify CSS classes are applied
            assert 'class="navigation"' in html
            assert 'class="combined-transcript-link"' in html

    def test_combined_link_with_session_title(self):
        """Test that combined transcript link works with custom session title."""
        messages = []
        session_id = "test-session-123"
        custom_title = "Custom Session Title"

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_manager = CacheManager(Path(tmpdir), "1.0.0")
            mock_project_data = SimpleNamespace()
            mock_project_data.sessions = {session_id: object()}
            cache_manager.get_cached_project_data = lambda: mock_project_data  # type: ignore

            html = generate_session_html(
                messages, session_id, custom_title, cache_manager
            )

            # Verify link is present and title is used
            assert "← View All Sessions (Combined Transcript)" in html
            assert f"<title>{custom_title}</title>" in html


class TestCombinedLinkFollowsTheFileNotTheFlag:
    """The back-link tracks whether the combined page exists on disk.

    It used to track ``--combined`` instead, which made a rendered page
    depend on the *run's* mode -- something the freshness check knows
    nothing about. So the state stuck: a page rendered by a
    ``--combined no`` run (every watch tick) kept no link for good, and a
    later full conversion saw a current file and skipped it. An archive
    ended up with some session pages linking and some not, decided only
    by which run happened to render each.

    Migration 013 records the state per page so a genuine transition
    regenerates; these tests pin both halves.
    """

    FIXTURE = (
        Path(__file__).parent / "test_data" / "real_projects" / "-experiments-ideas"
    )
    LINK = "← View All Sessions (Combined Transcript)"

    def _project(self, tmp_path: Path) -> Path:
        import shutil

        work = tmp_path / self.FIXTURE.name
        shutil.copytree(self.FIXTURE, work)
        return work

    def _pages(self, work: Path) -> list[Path]:
        pages = sorted(work.glob("session-*.html"))
        assert pages, "conversion produced no session files"
        return pages

    def _linked(self, work: Path) -> bool:
        return all(
            self.LINK in p.read_text(encoding="utf-8") for p in self._pages(work)
        )

    def _convert(self, work: Path, write_combined: bool) -> None:
        from claude_code_log.converter import convert_jsonl_to

        convert_jsonl_to("html", work, silent=True, write_combined=write_combined)

    def test_no_link_when_no_combined_page_has_ever_been_written(
        self, tmp_path: Path
    ) -> None:
        work = self._project(tmp_path)
        self._convert(work, write_combined=False)

        assert not (work / "combined_transcripts.html").exists()
        assert not self._linked(work)

    def test_link_appears_once_a_combined_page_exists(self, tmp_path: Path) -> None:
        """The regression: a full run after a `--combined no` run must fix it."""
        work = self._project(tmp_path)
        self._convert(work, write_combined=False)
        assert not self._linked(work)

        self._convert(work, write_combined=True)

        assert (work / "combined_transcripts.html").exists()
        assert self._linked(work), (
            "session pages kept their link-less state after a full conversion "
            "wrote the combined page they should link to"
        )

    def test_link_survives_a_watch_style_run_without_rewriting(
        self, tmp_path: Path
    ) -> None:
        """`--combined no` over an existing combined page changes nothing.

        The page it points at is still there and still served, so the link
        stays valid -- and the pages must not be rewritten for it.
        """
        work = self._project(tmp_path)
        self._convert(work, write_combined=True)
        before = {p: p.stat().st_mtime_ns for p in self._pages(work)}

        self._convert(work, write_combined=False)

        assert self._linked(work)
        assert {p: p.stat().st_mtime_ns for p in self._pages(work)} == before

    def test_link_is_dropped_when_the_combined_page_goes_away(
        self, tmp_path: Path
    ) -> None:
        work = self._project(tmp_path)
        self._convert(work, write_combined=True)
        assert self._linked(work)

        (work / "combined_transcripts.html").unlink()
        self._convert(work, write_combined=False)

        assert not self._linked(work), (
            "session pages still link to a combined page that no longer exists"
        )
