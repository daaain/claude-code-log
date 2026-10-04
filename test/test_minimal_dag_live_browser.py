"""The minimal theme's branches under a real watch, end to end (P7b).

work/minimal-theme-dag.md P7b; dev-docs/minimal-theme.md § 8. The other DAG
browser tests simulate live updates in-page; these drive the real thing: a
session that grows on disk (``test/dag_live_fixture.py``), converted by
the watch and fetched by ``live_update.js`` over http, on both pages a
reader can follow live:

- the **session page**, served by a real ``claude-code-log serve --watch
  --theme minimal`` subprocess (its re-conversion keeps the combined page
  as it was at startup, by design);
- the **combined page**, kept current by the in-process equivalent of
  ``claude-code-log watch --combined yes --theme minimal`` (the same
  ``convert_jsonl_to`` call, with its own entry store), served by
  ``ArchiveServer``.

Nothing sleeps: every step waits for the page to show the state it expects.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import convert_jsonl_to
from claude_code_log.entry_store import ParsedEntryStore
from claude_code_log.watch import WatchEngine
from test.dag_live_fixture import LANE_A, LANE_C, LIVE_SESSION, LiveDagScript
from test.test_minimal_dag_browser import DOT_Y, RAIL, _numbers

pytestmark = pytest.mark.browser

BRANCHES_KEY = "claude-code-log:branches"
PROJECT = "-tmp-live"
WAIT_MS = 30_000

# One lane as the page shows it.
LANE = """(lane) => {
    const dag = window.claudeLogDag;
    const head = document.querySelector(`[data-lane-id="${lane}"]`);
    const ctl = document.querySelector(`[data-lane-ref="${lane}"]`);
    const pill = ctl && ctl.querySelector('.mn-brun');
    return {
        mode: dag.mode(lane),
        running: dag.running(lane),
        stats: head && head.getAttribute('data-lane-stats'),
        to: head && head.getAttribute('data-lane-to'),
        from: head && head.getAttribute('data-lane-from'),
        state: head && head.getAttribute('data-lane-state'),
        label: ctl && ctl.querySelector('.mn-bfold').getAttribute('data-label'),
        pill: !!pill && getComputedStyle(pill).display !== 'none',
        controls: document.querySelectorAll(`[data-lane-ref="${lane}"]`).length,
        visible: [...document.querySelectorAll('#transcript .message')]
            .filter(el => el.getAttribute('data-lane') === lane && el.checkVisibility()).length,
    };
}"""


class _Lines:
    """A subprocess's stdout, line by line, readable with a timeout."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        assert proc.stdout is not None
        stream = proc.stdout

        def pump() -> None:
            for line in stream:
                self.lines.put(line)

        threading.Thread(target=pump, daemon=True).start()

    def wait_for(self, needle: str, timeout: float = 60) -> str:
        deadline = datetime.now() + timedelta(seconds=timeout)
        while True:
            left = (deadline - datetime.now()).total_seconds()
            if left <= 0:
                raise AssertionError(f"never printed {needle!r}: {self.seen}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            self.seen.append(line)
            if needle in line:
                return line


class LiveHarness:
    """A growing project, a watch converting it and a server serving it."""

    def __init__(self, root: Path, kind: str, base: datetime) -> None:
        self.kind = kind
        self.projects = root / "projects"
        self.project = self.projects / PROJECT
        self.script = LiveDagScript(self.project, base=base)
        self.url = ""
        self._proc: Optional[subprocess.Popen[str]] = None
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self.errors: list[BaseException] = []

    def start(self, stage: str) -> "LiveHarness":
        self.script.run_to(stage)
        if self.kind == "session":
            self._start_serve()
        else:
            self._start_watch()
        return self

    def _start_serve(self) -> None:
        # The real command: startup conversion, then a background watch
        # whose re-conversion is `write_combined=False` + an entry store.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "claude_code_log.cli",
                "serve",
                "--projects-dir",
                str(self.projects),
                "--port",
                "0",
                "--watch",
                "--no-index",
                "--theme",
                "minimal",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        out = _Lines(self._proc)
        line = out.wait_for("/index.html")
        base = line.strip().rsplit("/index.html", 1)[0]
        # Printed after the watcher primed: from here on, appends are seen.
        out.wait_for("watching for changes")
        self.url = f"{base}/{PROJECT}/session-{LIVE_SESSION}.html"

    def _start_watch(self) -> None:
        # What `claude-code-log watch <project> --combined yes --theme
        # minimal` runs per tick (cli.watch's `convert`), driven by the same
        # engine with the test suite's short debounce.
        from claude_code_log.server import ArchiveServer

        store = ParsedEntryStore()

        def convert(_changed: set[Path]) -> None:
            convert_jsonl_to(
                "html",
                self.project,
                silent=True,
                write_combined=True,
                generate_individual_sessions=True,
                entry_store=store,
                theme="minimal",
            )

        convert(set())
        engine = WatchEngine(
            [self.project],
            convert,
            quiet_period=0.1,
            max_latency=0.5,
            poll_interval=0.05,
            on_error=self.errors.append,
        )
        engine.prime()
        self._stop = threading.Event()
        self._thread = engine.run_in_thread(self._stop)
        self._server = ArchiveServer(self.projects, port=0)
        self._server.start()
        self.url = f"{self._server.url}/{PROJECT}/combined_transcripts.html"

    def advance(self, stage: str) -> None:
        self.script.advance(stage)

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._server is not None:
            self._server.stop()
        assert self.errors == [], f"watch conversion failed: {self.errors!r}"


def _recent() -> datetime:
    """A minute ago, to the millisecond: the session reads as live."""
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


@pytest.fixture(params=["session", "combined"])
def live(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    """``live(stage)`` → a running harness at ``stage``, on the page kind
    under test; closed after the test."""
    made: list[LiveHarness] = []

    def make(
        stage: str,
        base: Optional[datetime] = None,
        before: Optional[Callable[[LiveHarness], None]] = None,
    ) -> LiveHarness:
        harness = LiveHarness(tmp_path, request.param, base or _recent())
        made.append(harness)
        if before is not None:
            before(harness)
        return harness.start(stage)

    yield make
    for harness in made:
        harness.close()


def _open(page: Page, url: str) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.evaluate(f"localStorage.removeItem('{BRANCHES_KEY}')")
    page.reload()
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    # The poller's first HEAD adopts the page as its baseline.
    page.wait_for_function("!!window.claudeLogLiveUpdate")
    return errors


def _lane(page: Page, lane: str) -> dict[str, Any]:
    return page.evaluate(LANE, lane)


def _wait(page: Page, expression: str, arg: Any = None) -> None:
    """Wait for ``expression``, then for the update to settle: new cards fade
    in from a few px below (``live-new-in``) and the rail is redrawn once
    they land."""
    page.wait_for_function(expression, arg=arg, timeout=WAIT_MS)
    page.wait_for_function(
        """() => !document.getAnimations()
            .some(a => a.animationName === 'live-new-in' && a.playState !== 'finished')""",
        timeout=WAIT_MS,
    )
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def _parts(page: Page, lane: str) -> dict[str, dict[str, Any]]:
    rail = page.evaluate(RAIL, lane)
    return {p["part"]: p for p in rail["paths"]}


def _main_x(page: Page) -> float:
    return page.evaluate(RAIL, "main")["mainX"]


def _dot_y(page: Page, card: str) -> float:
    return page.evaluate(DOT_Y, "msg-" + card)


def _newest_bottom(page: Page) -> float:
    """The bottom of the lowest visible card, stage-relative."""
    return page.evaluate(
        """() => { const stage = document.querySelector('.mn-stage').getBoundingClientRect();
            return Math.max(...[...document.querySelectorAll('#transcript .message')]
                .filter(el => el.checkVisibility())
                .map(el => el.getBoundingClientRect().bottom - stage.top)); }"""
    )


def _click(page: Page, lane: str, what: str = ".mn-bfold") -> None:
    page.locator(f"[data-lane-ref='{lane}'] {what}").click()


# ---------------------------------------------------------------- (a)


def test_running_sync_agent_grows_then_merges(page: Page, live: Any) -> None:
    """A synchronous agent still running: its lane is not a stub but runs on
    to the newest row with an open end; it grows while the reader has it
    interleaved; its result turns it into the ordinary merge."""
    harness = live("start")
    errors = _open(page, harness.url)

    c = _lane(page, LANE_C)
    assert c["state"] == "open" and c["running"], c
    assert c["pill"], "no 'running' label on the control"
    assert "no result" not in (c["label"] or "")
    parts = _parts(page, LANE_C)
    assert set(parts) == {"fork", "lane", "end"}, parts
    assert parts["lane"]["dashed"], "a folded running lane keeps its dashes"
    end_y = _numbers(parts["end"]["d"])[1]
    spawn_y = _dot_y(page, c["from"])
    assert end_y > spawn_y + 16, "the lane does not carry on below its spawn"
    assert end_y == pytest.approx(_newest_bottom(page) - 7, abs=2)
    # The async agent next to it is open (running) too.
    assert _lane(page, LANE_A)["running"]

    _click(page, LANE_C)
    _wait(page, "(lane) => window.claudeLogDag.mode(lane) === 'interleaved'", LANE_C)
    assert _lane(page, LANE_C)["visible"] == 2

    # The sidechain grows (the first update of a page always swaps).
    harness.advance("c_grows")
    _wait(page, f"() => ({LANE})('{LANE_C}').visible === 4")
    c = _lane(page, LANE_C)
    assert c["mode"] == "interleaved" and c["running"], c
    assert c["stats"].startswith("2 steps"), c
    assert c["label"].startswith(c["stats"]), "the control shows stale stats"

    # Again: now the patch path (the agent's block is the end of the page).
    harness.advance("c_grows_more")
    _wait(page, f"() => ({LANE})('{LANE_C}').visible === 6")
    c = _lane(page, LANE_C)
    assert c["mode"] == "interleaved" and c["running"] and c["controls"] == 1, c
    assert c["stats"].startswith("3 steps") and c["label"].startswith(c["stats"])
    parts = _parts(page, LANE_C)
    assert set(parts) == {"fork", "lane", "end"}
    assert not parts["lane"]["dashed"]
    assert _numbers(parts["end"]["d"])[1] == pytest.approx(
        _newest_bottom(page) - 7, abs=2
    )

    # The result arrives: an ordinary curved merge at the result row.
    harness.advance("c_merges")
    _wait(page, f"() => !!({LANE})('{LANE_C}').to")
    c = _lane(page, LANE_C)
    assert not c["running"] and not c["pill"] and c["state"] is None, c
    assert c["mode"] == "interleaved" and c["controls"] == 1
    assert "12.1k tokens" in c["stats"] and c["label"].startswith(c["stats"])
    parts = _parts(page, LANE_C)
    assert set(parts) == {"fork", "lane", "merge"}, parts
    merge = _numbers(parts["merge"]["d"])
    assert merge[-2] == pytest.approx(_dot_y(page, c["to"]), abs=1.5)
    assert merge[-1] == pytest.approx(_main_x(page), abs=1.5)
    assert errors == []


# ---------------------------------------------------------------- (b)


def test_async_agent_merges_at_the_notification(page: Page, live: Any) -> None:
    """A background agent's lane is open; its <task-notification> arrives
    and the merge connector and the answer appear at that arrival row."""
    harness = live("c_merges")
    errors = _open(page, harness.url)
    a = _lane(page, LANE_A)
    assert a["running"] and a["state"] == "open", a
    _click(page, LANE_A)
    _wait(page, "(lane) => window.claudeLogDag.mode(lane) === 'interleaved'", LANE_A)
    assert set(_parts(page, LANE_A)) == {"fork", "lane", "end"}

    harness.advance("a_merges")
    _wait(page, f"() => !!({LANE})('{LANE_A}').to")
    a = _lane(page, LANE_A)
    assert not a["running"] and a["mode"] == "interleaved", a
    assert "48.4k tokens" in a["stats"]
    note = page.locator(f"#msg-{a['to']}")
    assert "task-notification" in (note.get_attribute("class") or "")
    assert note.is_visible()
    assert "14 files, 412 literals." in note.inner_text()
    parts = _parts(page, LANE_A)
    assert set(parts) == {"fork", "lane", "merge"}, parts
    merge = _numbers(parts["merge"]["d"])
    assert merge[-2] == pytest.approx(_dot_y(page, a["to"]), abs=1.5)
    # The agent's last step sits above the arrival row.
    last = page.evaluate(
        """(lane) => Math.max(...[...document.querySelectorAll('#transcript .message')]
            .filter(el => el.getAttribute('data-lane') === lane && el.checkVisibility())
            .map(el => el.getBoundingClientRect().top))""",
        LANE_A,
    )
    assert last < note.bounding_box()["y"]  # type: ignore[index]
    assert errors == []


# ---------------------------------------------------------------- (c)

MAIN_CARDS = """() => [...document.querySelectorAll('#transcript .message[data-uuid]')]
    .filter(el => el.getAttribute('data-lane') === 'main' && el.checkVisibility())
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
    .map(el => el.getAttribute('data-uuid'))"""


def test_a_rewind_adds_a_fork_lane_and_main_stays(page: Page, live: Any) -> None:
    """Rewinding while watching: the earliest branch stays main, the new
    one appears as a folded fork lane, and the main line reads as before."""
    harness = live("a_merges")
    errors = _open(page, harness.url)
    before = page.evaluate(MAIN_CARDS)
    prompt = page.locator(".message.user", has_text="Use custom properties").first
    assert prompt.is_visible()

    harness.advance("rewind")
    _wait(page, "() => !!document.querySelector('[data-lane-kind=\"fork\"]')")
    fork = page.evaluate(
        "document.querySelector('[data-lane-kind=\"fork\"]').getAttribute('data-lane-id')"
    )
    # The earliest prompt continues main; the rewound one is the fork.
    assert prompt.is_visible()
    assert prompt.get_attribute("data-lane") == "main"
    assert page.evaluate(MAIN_CARDS) == before, "the main line changed"
    f = _lane(page, fork)
    assert f["mode"] == "folded" and f["visible"] == 0 and f["controls"] == 1, f
    assert not f["running"] and "no result" not in f["label"]
    parts = _parts(page, fork)
    assert set(parts) == {"stub"}, parts
    assert _numbers(parts["stub"]["d"])[1] == pytest.approx(
        _dot_y(page, f["from"]), abs=1.5
    )
    for lane in (LANE_A, LANE_C):
        other = _lane(page, lane)
        assert other["mode"] == "folded" and other["controls"] == 1
        assert set(_parts(page, lane)) == {"fork", "lane", "merge"}
    assert errors == []


# ---------------------------------------------------------------- (d)

ENGINE = """() => ({
    bctls: [...document.querySelectorAll('#transcript [data-spawns]')]
        .map(el => el.querySelectorAll(':scope > .mn-bctls').length),
    chrome: [...document.querySelectorAll('#transcript > .dag-chrome')].map(el => el.getAttribute('data-col')),
    heads: document.querySelectorAll('#transcript .dag-colhead').length,
    rail: document.querySelectorAll('#dag-rail svg').length,
    folded: [...document.querySelectorAll('.message-node > .children')]
        .filter(c => c.style.display === 'none')
        .map(c => c.parentElement.querySelector(':scope > .message').getAttribute('data-uuid')),
    scroll: window.scrollY,
})"""
TAG = "() => document.querySelectorAll('#transcript .message').forEach(el => { el.__probe = 1; })"
KEPT = "() => [...document.querySelectorAll('#transcript .message')].filter(el => el.__probe).length"


def test_reader_choices_survive_patches_and_swaps(page: Page, live: Any) -> None:
    """One lane interleaved, another in a column, a turn folded and the page
    scrolled: every update — patch or swap — keeps all of it, with exactly
    one set of engine controls and column heads, a redrawn rail and fresh
    stats."""
    page.set_viewport_size({"width": 1400, "height": 420})
    harness = live("start")
    errors = _open(page, harness.url)
    _click(page, LANE_A)
    _click(page, LANE_C, ".mn-bcol")
    _wait(page, "(lane) => window.claudeLogDag.mode(lane) === 'column'", LANE_C)
    assert page.evaluate("document.documentElement.scrollHeight") > 600
    page.evaluate("window.scrollTo(0, 180)")

    expected_stats = {
        "c_grows": "2 steps",
        "c_grows_more": "3 steps",
        "c_merges": "12.1k tokens",
    }
    routes: dict[str, str] = {}
    folded_uuid: Optional[str] = None
    for stage in (
        "c_grows",
        "c_grows_more",
        "c_merges",
        "a_merges",
        "rewind",
        "fork_grows",
    ):
        if stage == "rewind":
            # Fold the second turn (its prompt's fold bar) before the rewind
            # moves it under the "rewound" header.
            folded_uuid = page.evaluate(
                """() => { const card = [...document.querySelectorAll('#transcript .message.user')]
                    .find(el => el.textContent.includes('Use custom properties'));
                    card.querySelector(':scope > .fold-bar .fold-bar-section').click();
                    return card.getAttribute('data-uuid'); }"""
            )
        before = page.evaluate(ENGINE)
        page.evaluate(TAG)
        cards = page.evaluate(
            "document.querySelectorAll('#transcript .message').length"
        )
        harness.advance(stage)
        _wait(
            page,
            "(n) => document.querySelectorAll('#transcript .message').length > n",
            cards,
        )
        routes[stage] = "patch" if page.evaluate(KEPT) else "swap"

        state = page.evaluate(ENGINE)
        assert _lane(page, LANE_A)["mode"] == "interleaved", stage
        assert _lane(page, LANE_C)["mode"] == "column", stage
        assert state["bctls"] and all(n == 1 for n in state["bctls"]), (stage, state)
        assert state["chrome"] == ["main", LANE_C], (stage, state)
        assert state["heads"] == 2, (stage, state)
        assert state["rail"] == 1 and _parts(page, LANE_A), stage
        assert state["scroll"] == before["scroll"], (stage, before, state)
        if folded_uuid and stage in ("rewind", "fork_grows"):
            assert folded_uuid in state["folded"], (stage, state)
        c = _lane(page, LANE_C)
        assert c["controls"] == 1
        if stage in expected_stats:
            assert expected_stats[stage] in c["stats"], (stage, c)
            assert c["label"].startswith(c["stats"]), (stage, c)
            meta = page.locator(f".dag-chrome[data-col='{LANE_C}'] .dag-colmeta")
            assert c["stats"] in (meta.get_attribute("data-label") or "")
        # The rail follows the page: A's lane is drawn to its current rows.
        a_parts = _parts(page, LANE_A)
        a_to = _lane(page, LANE_A)["to"]
        if a_to:
            assert _numbers(a_parts["merge"]["d"])[-2] == pytest.approx(
                _dot_y(page, a_to), abs=1.5
            )
        else:
            assert "end" in a_parts
    assert set(routes.values()) == {"patch", "swap"}, routes
    assert errors == []


# ------------------------------------------------- not running forever


def test_an_idle_session_shows_no_running_lane(page: Page, live: Any) -> None:
    """Served, but the session has been quiet for days: an agent without a
    result ended without one — no running label, a fork-like stub."""
    harness = live("start", base=_recent() - timedelta(days=2))
    _open(page, harness.url)
    for lane in (LANE_A, LANE_C):
        info = _lane(page, lane)
        assert info["state"] == "open" and not info["running"], info
        assert not info["pill"] and "no result" in info["label"], info
        assert set(_parts(page, lane)) == {"stub"}


IDLE_S = 30 * 60  # minimal_dag.js RUNNING_IDLE_MS, in seconds


@pytest.mark.parametrize("live", ["session"], indirect=True)
def test_a_running_lane_times_out(page: Page, live: Any) -> None:
    """A session that goes quiet stops showing a running lane, even if
    nothing else changes (the engine re-checks on a timer)."""
    # The newest card is written at base + 7 s: running for ~15 s from now.
    base = datetime.now(timezone.utc) - timedelta(seconds=IDLE_S - 8)
    harness = live("start", base=base)
    _open(page, harness.url)
    assert page.evaluate("window.claudeLogDag.runningIdleMs") == IDLE_S * 1000
    assert _lane(page, LANE_C)["running"], "should start out running"
    # The engine's own timer runs every 30 s; ask its decision directly
    # (relayout re-evaluates it) rather than waiting that out.
    _wait(
        page,
        "(lane) => { window.claudeLogDag.relayout(); return !window.claudeLogDag.running(lane); }",
        LANE_C,
    )
    info = _lane(page, LANE_C)
    assert "no result" in info["label"] and not info["pill"]
    assert set(_parts(page, LANE_C)) == {"stub"}


OLD_SESSION = "dade0000-0000-4000-8000-0000000000c1"


@pytest.mark.parametrize("live", ["combined"], indirect=True)
def test_a_live_session_does_not_wake_an_old_one(page: Page, live: Any) -> None:
    """A combined page holds every session of the project: an old session's
    agent that never returned stays ended-without-a-result while another
    session on the same page is live (recency is per session)."""

    def add_old_session(harness: LiveHarness) -> None:
        old = LiveDagScript(
            harness.project,
            base=_recent() - timedelta(days=2),
            session=OLD_SESSION,
            suffix="old",
            uid_space=7,
        )
        old.run_to("start")

    harness = live("start", before=add_old_session)
    _open(page, harness.url)
    for lane in (LANE_A, LANE_C):
        assert _lane(page, lane)["running"], lane
        stale = _lane(page, lane + "old")
        assert stale["state"] == "open" and not stale["running"], stale
        assert "no result" in stale["label"]


def test_a_static_page_never_shows_a_running_lane(page: Page, tmp_path: Path) -> None:
    """From file:// nothing can update the page, so an open lane is shown
    as ended without a result, however recent."""
    script = LiveDagScript(tmp_path / "projects" / PROJECT, base=_recent())
    script.run_to("start")
    convert_jsonl_to("html", script.project, silent=True, theme="minimal")
    page.goto((script.project / f"session-{LIVE_SESSION}.html").as_uri())
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    for lane in (LANE_A, LANE_C):
        info = _lane(page, lane)
        assert info["state"] == "open" and not info["running"], info
        assert "no result" in info["label"]
        assert set(_parts(page, lane)) == {"stub"}
