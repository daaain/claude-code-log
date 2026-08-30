"""The served page updating itself while a session is still being written.

These are live-server browser tests by necessity: the feature only
activates over http, because a `file://` page cannot fetch anything —
not a sibling, not even itself. The rest of the browser suite runs from
`file://`, so it cannot cover this.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.watch import WatchEngine

SESSION_ID = "dddddddd-eeee-ffff-0000-111111111111"


def _entry(uuid: str, text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-08-30T21:00:00Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp/live",
                "sessionId": SESSION_ID,
                "version": "1.0.0",
                "uuid": uuid,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


@pytest.fixture
def live_archive(tmp_path: Path):
    """A served project with a watcher, and a handle to append to it."""
    projects = tmp_path / "projects"
    project = projects / "-tmp-live"
    project.mkdir(parents=True)
    jsonl = project / f"{SESSION_ID}.jsonl"
    # Enough content that the page scrolls, so scroll preservation is
    # actually being tested rather than trivially true.
    jsonl.write_text(
        "".join(
            _entry(f"seed-{i}", f"seed message {i} " + ("padding " * 40))
            for i in range(40)
        ),
        encoding="utf-8",
    )
    process_projects_hierarchy(projects, silent=True)

    from claude_code_log.server import ArchiveServer

    engine = WatchEngine(
        [projects],
        lambda _paths: process_projects_hierarchy(projects, silent=True),
        quiet_period=0.1,
        max_latency=0.5,
        poll_interval=0.05,
        on_error=lambda exc: pytest.fail(f"watch conversion failed: {exc!r}"),
    )
    engine.prime()
    stop = threading.Event()
    thread = engine.run_in_thread(stop)

    server = ArchiveServer(projects, port=0)
    server.start()
    try:
        yield server.url, project, jsonl
    finally:
        stop.set()
        thread.join(timeout=10)
        server.stop()


def _wait_for(page, expression: str, timeout: int = 30000) -> None:
    page.wait_for_function(expression, timeout=timeout)


@pytest.mark.browser
class TestLiveUpdate:
    def _open(self, page, base: str, project: Path):
        errors: list[str] = []
        page.on(
            "console", lambda m: errors.append(m.text) if m.type == "error" else None
        )
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/{project.name}/session-{SESSION_ID}.html")
        page.wait_for_selector("#transcript")
        return errors

    def test_a_new_message_appears_without_navigating(self, page, live_archive) -> None:
        """The whole point: the page updates in place, not by reloading."""
        base, project, jsonl = live_archive
        errors = self._open(page, base, project)

        # A navigation would wipe this; a container swap must not.
        page.evaluate("window.__stillHere = 'yes'")
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("live-1", "LIVE-MARKER-ONE"))

        _wait_for(page, "() => document.body.innerText.includes('LIVE-MARKER-ONE')")
        assert page.evaluate("window.__stillHere") == "yes", "the page navigated"
        assert errors == []

    def test_scroll_position_survives_an_update(self, page, live_archive) -> None:
        base, project, jsonl = live_archive
        self._open(page, base, project)
        page.evaluate("window.scrollTo(0, 600)")
        before = page.evaluate("window.scrollY")
        assert before > 0, "fixture is not tall enough to test scrolling"

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("live-2", "LIVE-MARKER-TWO"))
        _wait_for(page, "() => document.body.innerText.includes('LIVE-MARKER-TWO')")

        assert page.evaluate("window.scrollY") == before

    def test_new_cards_are_tagged_for_the_fade_in(self, page, live_archive) -> None:
        """Transcripts record whole messages, never partial tokens, so a
        card can only ever appear complete. Announcing that arrival is the
        most honest 'streaming' the page can offer."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("live-3", "LIVE-MARKER-THREE"))
        _wait_for(
            page, "() => document.querySelectorAll('.message.live-new').length > 0"
        )

        assert page.locator("#live-update-pill").count() == 1

    def test_timestamps_on_new_cards_are_localised(self, page, live_archive) -> None:
        """The rehydrate contract, end to end: timestamp localisation
        rewrites innerHTML after load, so swapped-in markup would keep raw
        ISO strings unless the hook re-runs over it."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("live-4", "LIVE-MARKER-FOUR"))
        _wait_for(page, "() => document.body.innerText.includes('LIVE-MARKER-FOUR')")

        shown = page.evaluate(
            "() => { const t = [...document.querySelectorAll('.timestamp[data-timestamp]')];"
            " const last = t[t.length - 1];"
            " return last && {text: last.textContent.trim(),"
            "                 raw: last.getAttribute('data-timestamp')}; }"
        )
        assert shown, "no timestamp element found"
        assert shown["text"] != shown["raw"], "the new card kept its raw ISO timestamp"

    def test_fold_state_survives_an_update(self, page, live_archive) -> None:
        """Session headers fold but carry no `data-uuid` — on a
        single-session page the header is the *only* foldable node, so a
        uuid-keyed capture would silently preserve nothing."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        bar = page.locator(".fold-bar-section.fold-one-level").first
        assert bar.count() > 0, "fixture has nothing foldable"
        bar.click()
        page.wait_for_timeout(200)
        folded = page.evaluate(
            "() => [...document.querySelectorAll('.message-node > .children')]"
            ".filter(c => c.style.display === 'none').length"
        )
        assert folded > 0, "clicking the fold bar did not fold anything"

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("live-5", "LIVE-MARKER-FIVE"))
        # innerText cannot see inside a display:none container, so assert
        # on DOM presence.
        _wait_for(page, "() => !!document.querySelector('[data-uuid=\"live-5\"]')")

        still_folded = page.evaluate(
            "() => [...document.querySelectorAll('.message-node > .children')]"
            ".filter(c => c.style.display === 'none').length"
        )
        assert still_folded == folded, "fold state was lost across the update"

    def test_two_updates_inside_one_second_are_both_seen(
        self, page, live_archive
    ) -> None:
        """HTTP dates have one-second granularity, so `Last-Modified`
        alone makes the second of two rapid updates invisible. Observed
        for real before `Content-Length` joined the comparison."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("rapid-1", "RAPID-ONE"))
        _wait_for(page, "() => document.body.innerText.includes('RAPID-ONE')")
        # Immediately, inside the same second as the update just applied.
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("rapid-2", "RAPID-TWO"))
        _wait_for(page, "() => document.body.innerText.includes('RAPID-TWO')")

    def test_the_poller_is_inert_over_file_urls(self, page, tmp_path: Path) -> None:
        """The generated HTML must stay exactly as useful from `file://`.

        A `file://` page cannot fetch anything at all, so the poller has
        to notice and do nothing rather than throw on every interval.
        """
        projects = tmp_path / "projects"
        project = projects / "-tmp-static"
        project.mkdir(parents=True)
        (project / f"{SESSION_ID}.jsonl").write_text(
            _entry("only", "static page"), encoding="utf-8"
        )
        process_projects_hierarchy(projects, silent=True)

        errors: list[str] = []
        page.on(
            "console", lambda m: errors.append(m.text) if m.type == "error" else None
        )
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto((project / f"session-{SESSION_ID}.html").as_uri())
        page.wait_for_selector("#transcript")
        time.sleep(2)  # several poll intervals, had it been active

        assert errors == []
        assert page.locator("#live-update-pill").count() == 0
