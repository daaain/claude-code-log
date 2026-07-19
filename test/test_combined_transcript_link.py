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
            # --depth name for full detail; #159) — not the bare default file.
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
