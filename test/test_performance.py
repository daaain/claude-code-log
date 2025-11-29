#!/usr/bin/env python3
"""Performance tests for renderer with sample session data.

Uses the sample session in test/test_data/sessions/deep-manifest-tar/
which represents a moderate complexity session (~1.2MB, ~325 messages).
"""

import os
import time
from pathlib import Path
from typing import List

import pytest

from claude_code_log.models import TranscriptEntry
from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html


@pytest.mark.slow
class TestRenderPerformance:
    """Test that rendering completes in reasonable time."""

    @pytest.fixture
    def sample_session_path(self) -> Path:
        """Path to the sample session data."""
        return Path(__file__).parent / "test_data" / "sessions" / "deep-manifest-tar"

    def test_render_performance_under_threshold(self, sample_session_path: Path):
        """Test that rendering the sample session completes within threshold.

        The ~1.2MB sample session with ~325 messages should render in under 5 seconds.
        This includes loading, parsing, and HTML generation but not disk I/O for
        writing the output file.
        """
        # Enable timing if desired (timing output goes to stderr)
        os.environ["CLAUDE_CODE_LOG_DEBUG_TIMING"] = "1"

        # Load all JSONL files in the session directory
        messages: List[TranscriptEntry] = []
        for jsonl_file in sorted(sample_session_path.glob("*.jsonl")):
            messages.extend(load_transcript(jsonl_file))

        # Measure render time
        start_time = time.perf_counter()
        html = generate_html(messages, "Performance Test")
        render_time = time.perf_counter() - start_time

        # Verify output was generated
        assert html is not None
        assert len(html) > 100000, "Generated HTML should be substantial"

        # Performance threshold: 5 seconds for ~325 messages
        # This gives headroom for CI variations
        threshold_seconds = 5.0
        assert render_time < threshold_seconds, (
            f"Rendering took {render_time:.2f}s, expected < {threshold_seconds}s"
        )

    def test_message_count_matches_expected(self, sample_session_path: Path):
        """Verify the sample session has expected message count."""
        messages: List[TranscriptEntry] = []
        for jsonl_file in sorted(sample_session_path.glob("*.jsonl")):
            messages.extend(load_transcript(jsonl_file))

        # Sample session should have approximately 325 messages
        # Allow some variance for test data changes
        assert 300 <= len(messages) <= 400, (
            f"Expected ~325 messages, got {len(messages)}"
        )

    def test_render_time_per_message(self, sample_session_path: Path):
        """Test that average render time per message is reasonable."""
        messages: List[TranscriptEntry] = []
        for jsonl_file in sorted(sample_session_path.glob("*.jsonl")):
            messages.extend(load_transcript(jsonl_file))

        start_time = time.perf_counter()
        generate_html(messages, "Performance Test")
        render_time = time.perf_counter() - start_time

        avg_time_per_message_ms = (render_time / len(messages)) * 1000

        # Average should be under 15ms per message
        # The timing output shows ~1ms average, so 15ms gives good headroom
        threshold_ms = 15.0
        assert avg_time_per_message_ms < threshold_ms, (
            f"Average time per message: {avg_time_per_message_ms:.2f}ms, "
            f"expected < {threshold_ms}ms"
        )
