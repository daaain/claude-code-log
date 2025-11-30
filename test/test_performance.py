#!/usr/bin/env python3
"""Performance tests for renderer with sample session data.

Uses the sample session in test/test_data/real_projects/-src-deep-manifest/
which represents a moderate complexity session (~1.2MB, ~325 messages).

Also includes benchmarking tests for all real projects that output results
to GitHub Actions Job Summary when running in CI.
"""

import os
import time
from pathlib import Path
from typing import List

import pytest

from claude_code_log.models import TranscriptEntry
from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html

# Path to realistic test data
REAL_PROJECTS_DIR = Path(__file__).parent / "test_data" / "real_projects"


@pytest.mark.slow
class TestRenderPerformance:
    """Test that rendering completes in reasonable time."""

    @pytest.fixture
    def sample_session_path(self) -> Path:
        """Path to the sample session data."""
        return (
            Path(__file__).parent / "test_data" / "real_projects" / "-src-deep-manifest"
        )

    def test_render_performance_under_threshold(
        self, sample_session_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Test that rendering the sample session completes within threshold.

        The ~1.2MB sample session with ~325 messages should render in under 5 seconds.
        This includes loading, parsing, and HTML generation but not disk I/O for
        writing the output file.
        """
        # Enable timing output (goes to stderr) - monkeypatch ensures cleanup
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

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


@pytest.mark.benchmark
class TestBenchmarkAllProjects:
    """Benchmark all real projects and output to GitHub Job Summary."""

    @pytest.fixture(scope="class")
    def real_projects_path(self) -> Path:
        """Return path to realistic test projects."""
        if not REAL_PROJECTS_DIR.exists():
            pytest.skip(
                "Real projects test data not found. Run: ./scripts/setup_test_data.sh"
            )
        return REAL_PROJECTS_DIR

    def test_benchmark_all_projects(self, real_projects_path: Path) -> None:
        """Benchmark rendering performance for all real projects.

        This test measures render time for each project and outputs results
        to GitHub Actions Job Summary when running in CI (GITHUB_STEP_SUMMARY env var).
        """
        results: List[dict] = []

        for project_dir in sorted(real_projects_path.iterdir()):
            if not project_dir.is_dir():
                continue

            # Load all JSONL files for this project
            messages: List[TranscriptEntry] = []
            total_size = 0
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                total_size += jsonl_file.stat().st_size
                messages.extend(load_transcript(jsonl_file))

            if not messages:
                continue

            # Measure render time
            start_time = time.perf_counter()
            html = generate_html(messages, project_dir.name)
            render_time = time.perf_counter() - start_time

            # Calculate metrics
            size_mb = total_size / (1024 * 1024)
            msg_count = len(messages)
            html_size_mb = len(html) / (1024 * 1024) if html else 0
            avg_ms_per_msg = (render_time / msg_count * 1000) if msg_count else 0
            throughput_mb_s = size_mb / render_time if render_time > 0 else 0

            results.append(
                {
                    "project": project_dir.name,
                    "messages": msg_count,
                    "input_mb": size_mb,
                    "output_mb": html_size_mb,
                    "render_time_s": render_time,
                    "avg_ms_per_msg": avg_ms_per_msg,
                    "throughput_mb_s": throughput_mb_s,
                }
            )

        # Output to console
        print("\n" + "=" * 80)
        print("PERFORMANCE BENCHMARK RESULTS")
        print("=" * 80)
        print(
            f"{'Project':<45} {'Msgs':>6} {'In MB':>7} {'Out MB':>7} "
            f"{'Time':>7} {'ms/msg':>7} {'MB/s':>7}"
        )
        print("-" * 80)

        total_messages = 0
        total_input_mb = 0.0
        total_time = 0.0

        for r in results:
            print(
                f"{r['project']:<45} {r['messages']:>6} {r['input_mb']:>7.2f} "
                f"{r['output_mb']:>7.2f} {r['render_time_s']:>6.2f}s "
                f"{r['avg_ms_per_msg']:>6.1f} {r['throughput_mb_s']:>7.1f}"
            )
            total_messages += r["messages"]
            total_input_mb += r["input_mb"]
            total_time += r["render_time_s"]

        print("-" * 80)
        overall_avg_ms = (total_time / total_messages * 1000) if total_messages else 0
        overall_throughput = total_input_mb / total_time if total_time > 0 else 0
        print(
            f"{'TOTAL':<45} {total_messages:>6} {total_input_mb:>7.2f} "
            f"{'':>7} {total_time:>6.2f}s "
            f"{overall_avg_ms:>6.1f} {overall_throughput:>7.1f}"
        )
        print("=" * 80)

        # Write to GitHub Job Summary if available
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            self._write_github_summary(summary_path, results)

        # Basic assertion to ensure test passes
        assert len(results) > 0, "Should have benchmarked at least one project"
        assert all(r["render_time_s"] < 30 for r in results), (
            "All projects should render in under 30 seconds"
        )

    def _write_github_summary(self, summary_path: str, results: List[dict]) -> None:
        """Write benchmark results to GitHub Actions Job Summary."""
        with open(summary_path, "a") as f:
            f.write("\n## 📊 Performance Benchmark Results\n\n")
            f.write(
                "| Project | Messages | Input (MB) | Output (MB) | "
                "Time (s) | ms/msg | MB/s |\n"
            )
            f.write(
                "|:--------|-------:|-------:|-------:|-------:|-------:|-------:|\n"
            )

            total_messages = 0
            total_input_mb = 0.0
            total_time = 0.0

            for r in results:
                # Shorten project name for table
                short_name = r["project"]
                if len(short_name) > 35:
                    short_name = "..." + short_name[-32:]

                f.write(
                    f"| {short_name} | {r['messages']} | {r['input_mb']:.2f} | "
                    f"{r['output_mb']:.2f} | {r['render_time_s']:.2f} | "
                    f"{r['avg_ms_per_msg']:.1f} | {r['throughput_mb_s']:.1f} |\n"
                )
                total_messages += r["messages"]
                total_input_mb += r["input_mb"]
                total_time += r["render_time_s"]

            # Add totals row
            overall_avg_ms = (
                (total_time / total_messages * 1000) if total_messages else 0
            )
            overall_throughput = total_input_mb / total_time if total_time > 0 else 0
            f.write(
                f"| **TOTAL** | **{total_messages}** | **{total_input_mb:.2f}** | "
                f"- | **{total_time:.2f}** | "
                f"**{overall_avg_ms:.1f}** | **{overall_throughput:.1f}** |\n"
            )
            f.write("\n")
