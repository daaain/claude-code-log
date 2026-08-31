"""Tests for the watch engine's detection and debounce.

All of it runs against a fake clock and a hand-driven `tick()`. Nothing
sleeps: a watcher tested with real timing is a flaky-test generator, and
these are the tests that have to stay trustworthy when the conversion
behind them gets slower.
"""

from pathlib import Path

import pytest

from claude_code_log.watch import (
    DEFAULT_MAX_LATENCY,
    DEFAULT_QUIET_PERIOD,
    WatchEngine,
    scan,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def settle(self) -> None:
        """Advance clear of the quiet period.

        Deliberately not `advance(DEFAULT_QUIET_PERIOD)`: that lands the
        comparison exactly on the threshold, where binary floating point
        decides it (1000.0 + 0.3 - 1000.0 == 0.29999999999995453). Real
        clocks never sit on the boundary; tests shouldn't either.
        """
        self.advance(DEFAULT_QUIET_PERIOD * 2)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "s1.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    return d


def build(project: Path, clock: FakeClock) -> tuple[WatchEngine, list[set[Path]]]:
    fired: list[set[Path]] = []
    engine = WatchEngine([project], fired.append, clock=clock)
    engine.prime()
    return engine, fired


def append(path: Path, text: str = '{"type":"user"}\n') -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


class TestDetection:
    def test_priming_does_not_fire_on_an_existing_archive(self, project: Path) -> None:
        clock = FakeClock()
        engine, fired = build(project, clock)
        clock.advance(10)
        assert engine.tick() is None
        assert fired == []

    def test_an_append_is_detected(self, project: Path) -> None:
        clock = FakeClock()
        engine, fired = build(project, clock)
        append(project / "s1.jsonl")

        assert engine.tick() is None, "first sighting starts the quiet period"
        clock.settle()
        assert engine.tick() == {project / "s1.jsonl"}
        assert fired == [{project / "s1.jsonl"}]

    def test_a_new_file_is_detected(self, project: Path) -> None:
        clock = FakeClock()
        engine, _fired = build(project, clock)
        (project / "s2.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

        engine.tick()
        clock.settle()
        assert engine.tick() == {project / "s2.jsonl"}

    def test_a_deletion_is_detected(self, project: Path) -> None:
        clock = FakeClock()
        engine, _fired = build(project, clock)
        (project / "s1.jsonl").unlink()

        engine.tick()
        clock.settle()
        assert engine.tick() == {project / "s1.jsonl"}

    def test_agent_sidecars_are_watched(self, project: Path) -> None:
        """A sidecar can appear without the trunk transcript changing (#213)."""
        clock = FakeClock()
        engine, _fired = build(project, clock)
        sub = project / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.meta.json").write_text("{}", encoding="utf-8")

        engine.tick()
        clock.settle()
        assert engine.tick() == {sub / "agent-abc.meta.json"}

    def test_our_own_temp_files_are_ignored(self, project: Path) -> None:
        """Atomic writes leave a dot-prefixed temp beside the output.

        Treating it as a change would make the loop feed itself: convert,
        see the temp, convert again, forever.
        """
        clock = FakeClock()
        engine, fired = build(project, clock)
        (project / ".session-x.html.999.tmp").write_text("x", encoding="utf-8")

        clock.advance(DEFAULT_MAX_LATENCY * 2)
        assert engine.tick() is None
        assert fired == []

    def test_generated_output_is_not_watched(self, project: Path) -> None:
        """Output lands in the watched tree; only sources may trigger."""
        clock = FakeClock()
        engine, fired = build(project, clock)
        (project / "session-x.html").write_text("<html>", encoding="utf-8")
        (project / "combined_transcripts.html").write_text("<html>", encoding="utf-8")

        clock.advance(DEFAULT_MAX_LATENCY * 2)
        assert engine.tick() is None
        assert fired == []

    def test_scan_skips_a_file_that_vanishes(self, tmp_path: Path) -> None:
        assert scan([tmp_path / "does-not-exist"]) == {}


class TestDebounce:
    def test_a_burst_produces_one_conversion(self, project: Path) -> None:
        """The turn shape: several entries land in quick succession."""
        clock = FakeClock()
        engine, fired = build(project, clock)

        for _ in range(5):
            append(project / "s1.jsonl")
            engine.tick()
            clock.advance(DEFAULT_QUIET_PERIOD / 2)  # never quiet

        assert fired == [], "still inside the burst"
        clock.settle()
        engine.tick()
        assert len(fired) == 1
        assert engine.stats.conversions == 1

    def test_max_latency_forces_delivery_during_a_long_stream(
        self, project: Path
    ) -> None:
        """An unbroken stream must still surface, not starve."""
        clock = FakeClock()
        engine, fired = build(project, clock)

        elapsed = 0.0
        while elapsed < DEFAULT_MAX_LATENCY:
            append(project / "s1.jsonl")
            engine.tick()
            step = DEFAULT_QUIET_PERIOD / 2
            clock.advance(step)
            elapsed += step

        engine.tick()
        assert fired, "max_latency did not force a delivery"

    def test_all_changed_paths_in_a_burst_are_delivered_together(
        self, project: Path
    ) -> None:
        clock = FakeClock()
        engine, fired = build(project, clock)

        append(project / "s1.jsonl")
        engine.tick()
        clock.advance(DEFAULT_QUIET_PERIOD / 2)
        (project / "s2.jsonl").write_text("{}\n", encoding="utf-8")
        engine.tick()
        clock.settle()
        delivered = engine.tick()

        assert delivered == {project / "s1.jsonl", project / "s2.jsonl"}
        assert len(fired) == 1

    def test_pending_is_cleared_after_delivery(self, project: Path) -> None:
        clock = FakeClock()
        engine, _fired = build(project, clock)
        append(project / "s1.jsonl")
        engine.tick()
        clock.settle()
        engine.tick()

        assert engine.pending_paths == frozenset()
        clock.advance(DEFAULT_MAX_LATENCY * 2)
        assert engine.tick() is None, "a delivered change must not re-fire"


class TestErrors:
    def test_a_failing_conversion_does_not_stop_the_watch(self, project: Path) -> None:
        clock = FakeClock()
        seen: list[BaseException] = []
        calls: list[int] = []

        def boom(_paths: set[Path]) -> None:
            calls.append(1)
            raise RuntimeError("render exploded")

        engine = WatchEngine([project], boom, clock=clock, on_error=seen.append)
        engine.prime()

        for _ in range(2):
            append(project / "s1.jsonl")
            engine.tick()
            clock.settle()
            engine.tick()

        assert len(calls) == 2, "the watch kept going after the first failure"
        assert len(seen) == 2
        assert engine.stats.errors == 2

    def test_an_interrupt_is_not_a_failed_conversion(self, project: Path) -> None:
        """Ctrl+C must stop the watch, not be reported and slept off.

        `claude-code-log watch` runs the loop on the main thread, so an
        interrupt lands wherever the thread happens to be — and on an
        active project that is usually inside the conversion, not the
        sleep. Handing it to `on_error` would print "conversion failed"
        and carry on, leaving the operator pressing Ctrl+C until one
        happened to land in `stop.wait`.
        """
        clock = FakeClock()
        seen: list[BaseException] = []

        def interrupted(_paths: set[Path]) -> None:
            raise KeyboardInterrupt

        engine = WatchEngine([project], interrupted, clock=clock, on_error=seen.append)
        engine.prime()
        append(project / "s1.jsonl")
        engine.tick()
        clock.settle()
        with pytest.raises(KeyboardInterrupt):
            engine.tick()
        assert not seen, "the interrupt was reported as a conversion failure"

    def test_without_a_handler_the_error_propagates(self, project: Path) -> None:
        """Silently swallowing by default would hide real breakage."""
        clock = FakeClock()

        def boom(_paths: set[Path]) -> None:
            raise RuntimeError("render exploded")

        engine = WatchEngine([project], boom, clock=clock)
        engine.prime()
        append(project / "s1.jsonl")
        engine.tick()
        clock.settle()
        with pytest.raises(RuntimeError, match="render exploded"):
            engine.tick()


class TestLoop:
    """`run`/`run_in_thread` are what `serve --watch` will use.

    These are the only tests here that use a real clock — the loop's job
    *is* to wait — so they keep the interval tiny and assert on an event
    rather than on elapsed time.
    """

    def test_run_in_thread_delivers_and_stops(self, project: Path) -> None:
        import threading

        delivered = threading.Event()
        engine = WatchEngine(
            [project],
            lambda _paths: delivered.set(),
            poll_interval=0.01,
            quiet_period=0.0,
            max_latency=0.0,
        )
        # Prime before starting the thread: otherwise the baseline is
        # taken at whatever moment the loop thread gets scheduled, and an
        # append landing before that is absorbed into it and never seen.
        engine.prime()
        stop = threading.Event()
        thread = engine.run_in_thread(stop)
        try:
            append(project / "s1.jsonl")
            assert delivered.wait(timeout=10), "the loop never delivered the append"
        finally:
            stop.set()
            thread.join(timeout=10)
        assert not thread.is_alive(), "the loop did not stop when asked"
