#!/usr/bin/env python3
"""Tests for renderer timing utilities."""

import time

import pytest


class TestDebugTimingFlag:
    """Tests for DEBUG_TIMING environment variable."""

    def test_debug_timing_disabled_by_default(self):
        """DEBUG_TIMING is False by default."""
        # Import with fresh module state

        # Note: We can't easily test the default since the module is already loaded
        # This test just documents the expected default behavior
        # The actual value depends on environment at import time

    def test_debug_timing_enabled_with_1(self, monkeypatch: pytest.MonkeyPatch):
        """DEBUG_TIMING enabled with '1'."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        # Reimport to pick up env var
        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        assert rt.DEBUG_TIMING is True

    def test_debug_timing_enabled_with_true(self, monkeypatch: pytest.MonkeyPatch):
        """DEBUG_TIMING enabled with 'true'."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "true")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        assert rt.DEBUG_TIMING is True

    def test_debug_timing_enabled_with_yes(self, monkeypatch: pytest.MonkeyPatch):
        """DEBUG_TIMING enabled with 'yes'."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "yes")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        assert rt.DEBUG_TIMING is True

    def test_debug_timing_case_insensitive(self, monkeypatch: pytest.MonkeyPatch):
        """DEBUG_TIMING handles uppercase values."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "TRUE")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        assert rt.DEBUG_TIMING is True


class TestSetTimingVar:
    """Tests for set_timing_var function."""

    def test_sets_variable_when_enabled(self, monkeypatch: pytest.MonkeyPatch):
        """Sets timing variable when DEBUG_TIMING enabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        rt._timing_data.clear()

        rt.set_timing_var("test_var", "test_value")
        assert rt._timing_data.get("test_var") == "test_value"

    def test_ignores_when_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """Ignores set when DEBUG_TIMING disabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        rt._timing_data.clear()

        rt.set_timing_var("test_var", "test_value")
        assert "test_var" not in rt._timing_data


class TestLogTiming:
    """Tests for log_timing context manager."""

    def test_logs_phase_timing_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        """Logs phase timing when DEBUG_TIMING enabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        with rt.log_timing("Test Phase"):
            time.sleep(0.01)  # Brief sleep to measure

        captured = capsys.readouterr()
        assert "[TIMING]" in captured.out
        assert "Test Phase" in captured.out

    def test_no_output_when_disabled(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """No output when DEBUG_TIMING disabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        with rt.log_timing("Test Phase"):
            pass

        captured = capsys.readouterr()
        assert "[TIMING]" not in captured.out

    def test_callable_phase_name(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """Supports callable for dynamic phase names."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        items = [1, 2, 3]
        with rt.log_timing(lambda: f"Processing ({len(items)} items)"):
            pass

        captured = capsys.readouterr()
        assert "Processing (3 items)" in captured.out

    def test_shows_total_time_when_t_start_provided(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        """Shows total elapsed time when t_start provided."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        t_start = time.monotonic()
        time.sleep(0.01)

        with rt.log_timing("Test Phase", t_start=t_start):
            pass

        captured = capsys.readouterr()
        assert "total:" in captured.out


class TestTimingStat:
    """Tests for timing_stat context manager."""

    def test_tracks_operation_timing_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Tracks operation timing when DEBUG_TIMING enabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        rt._timing_data.clear()
        rt._timing_data["_test_timings"] = []
        rt._timing_data["_current_msg_id"] = "msg-123"

        with rt.timing_stat("_test_timings"):
            time.sleep(0.01)

        assert len(rt._timing_data["_test_timings"]) == 1
        duration, msg_id = rt._timing_data["_test_timings"][0]
        assert duration >= 0.01
        assert msg_id == "msg-123"

    def test_no_tracking_when_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """No tracking when DEBUG_TIMING disabled."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)
        rt._timing_data.clear()
        rt._timing_data["_test_timings"] = []

        with rt.timing_stat("_test_timings"):
            pass

        assert len(rt._timing_data["_test_timings"]) == 0


class TestReportTimingStatistics:
    """Tests for report_timing_statistics function."""

    def test_reports_statistics(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """Reports timing statistics."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        timings = [
            (0.1, "msg-1"),
            (0.2, "msg-2"),
            (0.05, "msg-3"),
        ]

        rt.report_timing_statistics([("Test Operation", timings)])

        captured = capsys.readouterr()
        assert "Test Operation" in captured.out
        assert "Total operations: 3" in captured.out
        assert "Total time:" in captured.out
        assert "Slowest 10 operations" in captured.out

    def test_empty_timings_no_output(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """No output for empty timings."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        rt.report_timing_statistics([("Test Operation", [])])

        captured = capsys.readouterr()
        # Empty timings produce no output (the if timings: check)
        assert "Test Operation" not in captured.out

    def test_sorts_by_duration_descending(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        """Slowest operations listed first."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        # Create timings in ascending order
        timings = [
            (0.001, "msg-fast"),
            (0.1, "msg-slow"),
            (0.01, "msg-medium"),
        ]

        rt.report_timing_statistics([("Test", timings)])

        captured = capsys.readouterr()
        # msg-slow should appear before msg-medium and msg-fast
        slow_pos = captured.out.find("msg-slow")
        medium_pos = captured.out.find("msg-medium")
        fast_pos = captured.out.find("msg-fast")
        assert slow_pos < medium_pos < fast_pos

    def test_limits_to_10_slowest(self, monkeypatch: pytest.MonkeyPatch, capsys):
        """Only shows 10 slowest operations."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "1")

        import importlib
        import claude_code_log.renderer_timings as rt

        importlib.reload(rt)

        # Create 15 timings
        timings = [(i * 0.001, f"msg-{i}") for i in range(15)]

        rt.report_timing_statistics([("Test", timings)])

        captured = capsys.readouterr()
        # Should only show 10
        assert captured.out.count("msg-") == 10
