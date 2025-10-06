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
            mock_print.assert_any_call(
                "HTML file combined_transcripts.html is current, skipping regeneration"
            )

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
            # Check that session file regeneration was skipped
            printed_calls = [str(call) for call in mock_print.call_args_list]
            session_skip_found = any(
                "Session file" in call and "skipping regeneration" in call
                for call in printed_calls
            )
            assert session_skip_found, (
                f"Expected session skip message, got: {printed_calls}"
            )

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
        """Test that index.html is regenerated when any project's JSONL files change."""
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
        original_mtime = index_file.stat().st_mtime

        # Verify index was generated with project data
        assert "project1" in original_content
        assert "project2" in original_content

        # Wait to ensure different modification time
        time.sleep(0.1)

        # Second run: No changes, should skip regeneration
        with patch("builtins.print") as mock_print:
            process_projects_hierarchy(projects_dir)
            mock_print.assert_any_call("Index HTML is current, skipping regeneration")

        # Verify file wasn't regenerated
        assert index_file.stat().st_mtime == original_mtime

        # Third run: Modify JSONL file in project1, should regenerate index
        time.sleep(1.1)  # Ensure > 1.0 second difference for cache detection
        new_message = '{"type":"summary","summary":"This project now has updated content for index regeneration test.","leafUuid":"msg_011","timestamp":"2025-07-03T16:25:00Z"}\n'
        with open(jsonl1, "a", encoding="utf-8") as f:
            f.write(new_message)

        # Should regenerate index
        process_projects_hierarchy(projects_dir)

        # Verify index was regenerated
        assert index_file.stat().st_mtime > original_mtime

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
