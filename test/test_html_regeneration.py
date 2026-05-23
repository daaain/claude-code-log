#!/usr/bin/env python3
"""Tests for HTML regeneration when JSONL files change."""

import time
from pathlib import Path
from unittest.mock import patch


from claude_code_log.converter import (
    convert_jsonl_to_html,
    process_projects_hierarchy,
    ensure_fresh_cache,
)
from claude_code_log.cache import CacheManager, get_library_version


class TestHtmlRegeneration:
    """Test that HTML files are regenerated when JSONL files change."""

    def test_combined_transcript_regeneration_on_jsonl_change(self, tmp_path):
        """Test that combined_transcripts.html is regenerated when JSONL files change."""
        # Setup: Create a project directory with JSONL data
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Copy test data
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # First run: Generate HTML
        output_file = convert_jsonl_to_html(project_dir)
        assert output_file.exists()
        original_content = output_file.read_text(encoding="utf-8")
        original_mtime = output_file.stat().st_mtime

        # Verify HTML was generated
        assert "Claude Transcripts" in original_content

        # Wait to ensure different modification time
        time.sleep(0.1)

        # Second run: No changes, should skip regeneration
        with patch("builtins.print") as mock_print:
            convert_jsonl_to_html(project_dir)
            # Check for either the per-file skip message (legacy) or the early-exit message (new)
            printed_calls = [str(call) for call in mock_print.call_args_list]
            skip_found = any(
                "skipping regeneration" in call or "All HTML files are current" in call
                for call in printed_calls
            )
            assert skip_found, f"Expected skip message, got: {printed_calls}"

        # Verify file wasn't regenerated
        assert output_file.stat().st_mtime == original_mtime

        # Third run: Modify JSONL file, should regenerate
        time.sleep(1.1)  # Ensure > 1.0 second difference for cache detection
        new_message = '{"type":"user","timestamp":"2025-07-03T16:15:00Z","parentUuid":null,"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session","version":"1.0.0","uuid":"new_msg","message":{"role":"user","content":[{"type":"text","text":"This is a new message to test regeneration."}]}}\n'
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Should regenerate without explicit print check since it should happen silently
        convert_jsonl_to_html(project_dir)

        # Verify file was regenerated
        assert output_file.stat().st_mtime > original_mtime
        new_content = output_file.read_text(encoding="utf-8")
        assert "This is a new message to test regeneration" in new_content

    def test_individual_session_regeneration_on_jsonl_change(self, tmp_path):
        """Test that individual session HTML files are regenerated when JSONL files change."""
        # Setup: Create a project directory with JSONL data
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Copy test data
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # First run: Generate HTML with individual sessions
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        # Find the session HTML file
        session_files = list(project_dir.glob("session-*.html"))
        assert len(session_files) == 1
        session_file = session_files[0]

        original_mtime = session_file.stat().st_mtime

        # Wait to ensure different modification time
        time.sleep(0.1)

        # Second run: No changes, should skip regeneration
        with patch("builtins.print") as mock_print:
            convert_jsonl_to_html(project_dir, generate_individual_sessions=True)
            # Check for either per-file skip message (legacy) or early-exit message (new)
            printed_calls = [str(call) for call in mock_print.call_args_list]
            skip_found = any(
                "skipping regeneration" in call or "All HTML files are current" in call
                for call in printed_calls
            )
            assert skip_found, f"Expected skip message, got: {printed_calls}"

        # Verify file wasn't regenerated
        assert session_file.stat().st_mtime == original_mtime

        # Third run: Modify JSONL file, should regenerate
        time.sleep(1.1)  # Ensure > 1.0 second difference for cache detection
        new_message = '{"type":"assistant","timestamp":"2025-07-03T16:20:00Z","parentUuid":null,"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session","version":"1.0.0","uuid":"new_assistant_msg","requestId":"req_new","message":{"id":"new_assistant_msg","type":"message","role":"assistant","model":"claude-3-sonnet-20240229","content":[{"type":"text","text":"I can help you test session regeneration!"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":15,"output_tokens":10}}}\n'
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Should regenerate
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        # Verify session file was regenerated
        assert session_file.stat().st_mtime > original_mtime
        new_content = session_file.read_text(encoding="utf-8")
        assert "I can help you test session regeneration" in new_content

    def test_projects_index_regeneration_on_jsonl_change(self, tmp_path):
        """The projects index is always regenerated on every run, so that
        toggling variant flags (e.g. `--compact`) refreshes its links —
        and so that JSONL edits are picked up too. This test pins both
        behaviours: a no-op re-run still writes the file (always-regen
        contract); a JSONL edit shows up in the new content."""
        # Setup: Create projects hierarchy
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        project1 = projects_dir / "project1"
        project1.mkdir()
        project2 = projects_dir / "project2"
        project2.mkdir()

        # Copy test data to projects
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl1 = project1 / "test1.jsonl"
        jsonl1.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        jsonl2 = project2 / "test2.jsonl"
        jsonl2.write_text(
            (test_data_dir / "edge_cases.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        # First run: Generate index
        index_file = process_projects_hierarchy(projects_dir)
        assert index_file.exists()
        original_content = index_file.read_text(encoding="utf-8")
        original_mtime_ns = index_file.stat().st_mtime_ns

        # Verify index was generated with project data
        assert "project1" in original_content
        assert "project2" in original_content

        # Wait to ensure different modification time
        time.sleep(0.1)

        # Second run: No source changes — but per the always-regenerate
        # contract, the index file is rewritten anyway. The stale
        # "Index ... is current, skipping regeneration" log line is
        # gone; assert its absence so the contract can't silently
        # regress.
        with patch("builtins.print") as mock_print:
            process_projects_hierarchy(projects_dir, silent=False)
        for call in mock_print.call_args_list:
            args = call.args
            if args and isinstance(args[0], str):
                assert "skipping regeneration" not in args[0], (
                    "Index regeneration should no longer be skipped on no-op runs."
                )
        # File was rewritten (mtime advanced).
        assert index_file.stat().st_mtime_ns > original_mtime_ns

        # Third run: Modify JSONL file in project1; index picks up the
        # new content.
        time.sleep(1.1)  # > 1.0s ensures the cache's mtime tier sees the change.
        new_message = '{"type":"summary","summary":"This project now has updated content for index regeneration test.","leafUuid":"msg_011","timestamp":"2025-07-03T16:25:00Z"}\n'
        with open(jsonl1, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Capture before re-run so we can prove the index *content*
        # actually changed (not just its mtime). The projects index
        # surfaces project-level metadata (counts, date ranges, session
        # AI titles) — not raw message text — so a string match on the
        # appended message body isn't a reliable signal. A direct
        # content comparison catches a "byte-equivalent rewrite" stale
        # bug that an mtime check alone would silently pass.
        pre_change_content = index_file.read_text(encoding="utf-8")
        post_change_mtime_ns = index_file.stat().st_mtime_ns
        process_projects_hierarchy(projects_dir)
        assert index_file.stat().st_mtime_ns > post_change_mtime_ns
        post_change_content = index_file.read_text(encoding="utf-8")
        assert post_change_content != pre_change_content, (
            "Regenerated index should reflect the appended JSONL entry "
            "(project metadata: message count, date range, session "
            "summary). An mtime bump alone wouldn't catch a "
            "stale-content regression."
        )

    def test_cache_update_detection(self, tmp_path):
        """Test that cache updates are properly detected and used to trigger regeneration."""
        # Setup: Create a project directory with JSONL data
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Copy test data
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # Initialize cache manager
        library_version = get_library_version()
        cache_manager = CacheManager(project_dir, library_version)

        # First run: Use ensure_fresh_cache to populate cache properly
        cache_was_updated = ensure_fresh_cache(project_dir, cache_manager)
        assert cache_was_updated is True  # Cache should be updated on first run

        # Verify cache has been populated
        cached_project_data = cache_manager.get_cached_project_data()
        assert cached_project_data is not None
        assert cached_project_data.total_message_count > 0

        # Second run: Cache exists, no file changes, should return False for cache update
        # No changes should mean no cache update
        cache_was_updated = ensure_fresh_cache(project_dir, cache_manager)
        assert cache_was_updated is False

        # Modify JSONL file
        time.sleep(1.1)  # Ensure > 1.0 second difference for cache detection
        new_message = '{"type":"user","timestamp":"2025-07-03T16:30:00Z","parentUuid":null,"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session","version":"1.0.0","uuid":"cache_test_msg","message":{"role":"user","content":[{"type":"text","text":"Testing cache update detection."}]}}\n'
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Now cache should detect the change
        cache_was_updated = ensure_fresh_cache(project_dir, cache_manager)
        assert cache_was_updated is True

    def test_force_regeneration_with_cache_update(self, tmp_path):
        """Test that HTML regeneration is forced when cache_was_updated is True, even with same version."""
        # Setup: Create a project directory with JSONL data
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Copy test data
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # First run: Generate HTML
        output_file = convert_jsonl_to_html(project_dir)
        original_mtime = output_file.stat().st_mtime

        # Verify the HTML contains the version comment
        content = output_file.read_text(encoding="utf-8")
        library_version = get_library_version()
        assert f"Generated by claude-code-log v{library_version}" in content

        # Wait and modify JSONL file (this should trigger cache update and regeneration)
        time.sleep(1.1)  # Ensure > 1.0 second difference for cache detection
        new_message = '{"type":"user","timestamp":"2025-07-03T16:35:00Z","parentUuid":null,"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session","version":"1.0.0","uuid":"force_regen_msg","message":{"role":"user","content":[{"type":"text","text":"This should force regeneration despite same version."}]}}\n'
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Should regenerate because cache was updated (not because of version change)
        convert_jsonl_to_html(project_dir)

        # Verify file was regenerated
        assert output_file.stat().st_mtime > original_mtime
        new_content = output_file.read_text(encoding="utf-8")
        assert "This should force regeneration despite same version" in new_content

    def test_single_file_mode_regeneration_behavior(self, tmp_path):
        """Test that single file mode doesn't use cache but still respects version checks."""
        # Setup: Create a single JSONL file
        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = tmp_path / "single_test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # First run: Generate HTML for single file
        output_file = convert_jsonl_to_html(jsonl_file)
        assert output_file.exists()
        original_mtime = output_file.stat().st_mtime

        # Second run: Should skip regeneration based on version (not cache)
        time.sleep(0.1)
        with patch("builtins.print") as mock_print:
            convert_jsonl_to_html(jsonl_file)
            mock_print.assert_any_call(
                "HTML file single_test.html is current, skipping regeneration"
            )

        # Verify file wasn't regenerated (same mtime)
        assert output_file.stat().st_mtime == original_mtime

        # Modify file - should NOT auto-regenerate in single file mode because there's no cache
        time.sleep(0.1)
        new_message = '{"type":"user","timestamp":"2025-07-03T16:40:00Z","parentUuid":null,"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session","version":"1.0.0","uuid":"single_file_msg","message":{"role":"user","content":[{"type":"text","text":"Single file mode test."}]}}\n'
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Single file mode doesn't have cache, so it should still skip based on version
        with patch("builtins.print") as mock_print:
            convert_jsonl_to_html(jsonl_file)
            mock_print.assert_any_call(
                "HTML file single_test.html is current, skipping regeneration"
            )

        # Verify file wasn't regenerated (this is expected behavior for single file mode)
        assert output_file.stat().st_mtime == original_mtime


class TestIncrementalHtmlCache:
    """Test incremental HTML regeneration using html_cache table."""

    def test_html_cache_tracks_generation(self, tmp_path):
        """Test that html_cache table correctly tracks when HTML was generated."""
        # Setup project with JSONL
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # Generate HTML
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        # Check that html_cache has entries
        cache_manager = CacheManager(project_dir, get_library_version())
        combined_cache = cache_manager.get_html_cache("combined_transcripts.html")

        assert combined_cache is not None
        assert combined_cache.html_path == "combined_transcripts.html"
        assert combined_cache.source_session_id is None  # Combined has no session
        assert combined_cache.message_count > 0

        # Check session HTML cache
        session_files = list(project_dir.glob("session-*.html"))
        assert len(session_files) >= 1

        session_file = session_files[0]
        session_id = session_file.stem.replace("session-", "")
        session_cache = cache_manager.get_html_cache(session_file.name)

        assert session_cache is not None
        assert session_cache.source_session_id == session_id
        assert session_cache.message_count > 0

    def test_incremental_regeneration_only_updates_changed_sessions(self, tmp_path):
        """Test that only sessions with changes are regenerated."""
        # Setup project with two JSONL files (different sessions)
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Create two sessions in separate files
        session1 = (
            '{"type":"user","timestamp":"2025-01-01T10:00:00Z","parentUuid":null,'
            '"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"session1",'
            '"version":"1.0.0","uuid":"msg1","message":{"role":"user",'
            '"content":[{"type":"text","text":"Session 1 message"}]}}\n'
        )
        session2 = (
            '{"type":"user","timestamp":"2025-01-01T11:00:00Z","parentUuid":null,'
            '"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"session2",'
            '"version":"1.0.0","uuid":"msg2","message":{"role":"user",'
            '"content":[{"type":"text","text":"Session 2 message"}]}}\n'
        )

        (project_dir / "session1.jsonl").write_text(session1, encoding="utf-8")
        (project_dir / "session2.jsonl").write_text(session2, encoding="utf-8")

        # First run: Generate all HTML
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        session1_html = project_dir / "session-session1.html"
        session2_html = project_dir / "session-session2.html"
        assert session1_html.exists()
        assert session2_html.exists()

        session1_mtime = session1_html.stat().st_mtime
        session2_mtime = session2_html.stat().st_mtime

        # Wait and modify only session1
        time.sleep(1.1)  # Ensure > 1.0 second difference
        new_msg = (
            '{"type":"user","timestamp":"2025-01-01T10:05:00Z","parentUuid":"msg1",'
            '"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"session1",'
            '"version":"1.0.0","uuid":"msg1b","message":{"role":"user",'
            '"content":[{"type":"text","text":"New message in session 1"}]}}\n'
        )
        with open(project_dir / "session1.jsonl", "a", encoding="utf-8") as f:
            f.write(new_msg)

        # Second run: Should only regenerate session1
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        # Session 1 should be regenerated (newer mtime)
        assert session1_html.stat().st_mtime > session1_mtime

        # Session 2 should NOT be regenerated (same mtime)
        assert session2_html.stat().st_mtime == session2_mtime

    def test_html_cache_detects_library_version_change(self, tmp_path):
        """Test that HTML is regenerated when library version changes.

        Uses version values past all current ``breaking_changes`` rules so
        the cache itself isn't invalidated — exercising the HTML
        staleness ``version_mismatch`` path specifically.
        """
        from unittest.mock import patch

        # Setup project
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        test_data_dir = Path(__file__).parent / "test_data"
        jsonl_file = project_dir / "test.jsonl"
        jsonl_file.write_text(
            (test_data_dir / "representative_messages.jsonl").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        # Build the cache + HTML at a version past all breaking rules so
        # our compatibility-window changes don't accidentally invalidate
        # the cache here. Patch the symbol at every call-site (the name
        # is imported into multiple modules at startup).
        with (
            patch(
                "claude_code_log.cache.get_library_version",
                return_value="999.999.998",
            ),
            patch(
                "claude_code_log.converter.get_library_version",
                return_value="999.999.998",
            ),
        ):
            convert_jsonl_to_html(project_dir)

            cache_manager = CacheManager(project_dir, "999.999.998")

            # Check staleness with same version
            is_stale, reason = cache_manager.is_html_stale("combined_transcripts.html")
            assert not is_stale
            assert reason == "up_to_date"

        # Create new cache manager with a higher (but still post-rule) version
        cache_manager_new = CacheManager(project_dir, "999.999.999")
        is_stale, reason = cache_manager_new.is_html_stale("combined_transcripts.html")
        assert is_stale
        assert reason == "version_mismatch"

    def test_html_cache_message_count_validation(self, tmp_path):
        """Test that session HTML regenerates when message count changes."""
        # Setup project
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        session_msg = (
            '{"type":"user","timestamp":"2025-01-01T10:00:00Z","parentUuid":null,'
            '"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session",'
            '"version":"1.0.0","uuid":"msg1","message":{"role":"user",'
            '"content":[{"type":"text","text":"Test message"}]}}\n'
        )
        (project_dir / "test.jsonl").write_text(session_msg, encoding="utf-8")

        # Generate HTML
        convert_jsonl_to_html(project_dir, generate_individual_sessions=True)

        cache_manager = CacheManager(project_dir, get_library_version())

        # Check session is not stale
        is_stale, reason = cache_manager.is_html_stale(
            "session-test_session.html", "test_session"
        )
        assert not is_stale

        # Add a new message (increases count)
        time.sleep(1.1)
        new_msg = (
            '{"type":"user","timestamp":"2025-01-01T10:01:00Z","parentUuid":"msg1",'
            '"isSidechain":false,"userType":"human","cwd":"/tmp","sessionId":"test_session",'
            '"version":"1.0.0","uuid":"msg2","message":{"role":"user",'
            '"content":[{"type":"text","text":"Second message"}]}}\n'
        )
        with open(project_dir / "test.jsonl", "a", encoding="utf-8") as f:
            f.write(new_msg)

        # Update cache to reflect new message
        ensure_fresh_cache(project_dir, cache_manager)

        # Now session should be stale (message count changed)
        is_stale, reason = cache_manager.is_html_stale(
            "session-test_session.html", "test_session"
        )
        assert is_stale
        assert reason == "session_updated"
