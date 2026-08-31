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

        # The follow toggle belongs to the page's floating-button stack and
        # is revealed once polling starts, so it carries the unseen count
        # rather than being built from scratch on first arrival.
        follow = page.locator("#followUpdates")
        assert follow.count() == 1
        assert "live-active" in (follow.get_attribute("class") or "")
        assert follow.get_attribute("data-unseen") not in (None, "0")

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

    # ---- patching ----------------------------------------------------
    #
    # The first update of a session always swaps: the hashes a patch
    # compares against are taken from parsed markup, and the first update
    # is where they are first taken. So every test below appends twice —
    # once to seed, once to exercise the patch. Asserting only that the
    # new message arrived would pass just as well with the patch disabled
    # entirely, so these assert on *element identity*, which the swap
    # necessarily destroys and the patch necessarily keeps.

    _TAG_CARDS = (
        "() => { let n = 0;"
        " document.querySelectorAll('#transcript .message').forEach(el => {"
        "   el.__probe = true; n++; });"
        " return n; }"
    )
    _COUNT_TAGGED = (
        "() => { const els = [...document.querySelectorAll('#transcript .message')];"
        " return { total: els.length, kept: els.filter(e => e.__probe).length }; }"
    )

    def test_an_append_patches_rather_than_rebuilding_the_page(
        self, page, live_archive
    ) -> None:
        """A swap replaces every node in the container, so no element on
        screen survives it. A patch touches only what changed."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("patch-seed", "PATCH-SEED"))
        _wait_for(page, "() => document.body.innerText.includes('PATCH-SEED')")

        tagged = page.evaluate(self._TAG_CARDS)
        assert tagged > 10, "fixture too small to distinguish patch from swap"

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("patch-two", "PATCH-TWO"))
        _wait_for(page, "() => document.body.innerText.includes('PATCH-TWO')")

        after = page.evaluate(self._COUNT_TAGGED)
        assert after["total"] == tagged + 1, "the appended card did not arrive"
        # A swap scores exactly 0. A patch keeps everything but the few
        # cards whose own markup really changed — in practice the ancestors
        # whose fold bar counts their descendants.
        assert after["kept"] > 0, "every card was rebuilt: the patch did not run"
        assert after["kept"] >= tagged - 5, (
            f"patch replaced more than expected: {tagged - after['kept']} cards"
        )

    def test_a_patch_leaves_existing_timestamps_localised(
        self, page, live_archive
    ) -> None:
        """Timestamp localisation rewrites innerHTML, so a rebuilt card
        comes back with a raw ISO string and has to be converted again.
        Across a growing session that is the whole page, every update."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("ts-seed", "TS-SEED"))
        _wait_for(page, "() => document.body.innerText.includes('TS-SEED')")
        _wait_for(
            page,
            "() => { const t = document.querySelector('#transcript .timestamp"
            "[data-timestamp]'); return t && t.childElementCount > 0; }",
        )
        page.evaluate(
            "() => { document.querySelector('#transcript .timestamp[data-timestamp]')"
            ".__probe = true; }"
        )

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("ts-two", "TS-TWO"))
        _wait_for(page, "() => document.body.innerText.includes('TS-TWO')")

        first = page.evaluate(
            "() => { const t = document.querySelector('#transcript .timestamp"
            "[data-timestamp]');"
            " return t && { kept: !!t.__probe, localised: t.childElementCount > 0 }; }"
        )
        assert first["kept"], "the first timestamp's element was rebuilt"
        assert first["localised"], "the first timestamp lost its localisation"

    def test_the_swap_is_the_fallback_when_the_ids_move(
        self, page, live_archive
    ) -> None:
        """The patch is only valid while the card ids still mean what they
        meant. Out-of-order arrivals renumber the positional `msg-d-N`
        ids — measured at 2 of 47 growth steps across three real sessions —
        and the update must fall back rather than patch against stale
        identities."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("fallback-seed", "FALLBACK-SEED"))
        _wait_for(page, "() => document.body.innerText.includes('FALLBACK-SEED')")

        # Control: with the ids intact, this same append patches. Without
        # it, `kept == 0` below would prove only that *something* swapped —
        # which is also what a permanently broken patch looks like.
        page.evaluate(self._TAG_CARDS)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("fallback-ctl", "FALLBACK-CTL"))
        _wait_for(page, "() => document.body.innerText.includes('FALLBACK-CTL')")
        assert page.evaluate(self._COUNT_TAGGED)["kept"] > 0, (
            "control failed: the patch did not run even with the ids intact"
        )

        # Renumbering, simulated at its effect: the live tree's id sequence
        # no longer matches the one the next render will carry.
        page.evaluate(self._TAG_CARDS)
        page.evaluate(
            "() => { const els = [...document.querySelectorAll('#transcript .message')];"
            " els[Math.floor(els.length / 2)].id = 'msg-d-999999'; }"
        )

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("fallback-two", "FALLBACK-TWO"))
        _wait_for(page, "() => document.body.innerText.includes('FALLBACK-TWO')")

        after = page.evaluate(self._COUNT_TAGGED)
        assert after["kept"] == 0, "the patch ran against a renumbered tree"
        # And the fallback did the job properly, not just safely.
        assert page.evaluate(
            "() => !!document.querySelector('#transcript .message.live-new')"
        ), "the swap did not tag the new card"

    def test_following_leaves_the_newest_card_clear_of_the_viewport_edge(
        self, page, live_archive
    ) -> None:
        """`scrollIntoView({block: 'end'})` aligns the card's bottom edge
        with the viewport's, which measures as a 0px gap and reads as the
        message being cut off. The padding under `body.live-following` is
        what gives the scroll somewhere to go."""
        base, project, jsonl = live_archive
        self._open(page, base, project)

        page.click("#followUpdates")
        assert page.evaluate(
            "() => document.body.classList.contains('live-following')"
        ), "clicking the toggle did not engage follow mode"

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("follow-1", "FOLLOW-ONE"))
        _wait_for(page, "() => document.body.innerText.includes('FOLLOW-ONE')")
        # The scroll is smooth, so let it land.
        page.wait_for_timeout(1200)

        gap = page.evaluate(
            "() => { const c = document.getElementById('transcript');"
            " const r = c.lastElementChild.getBoundingClientRect();"
            " return Math.round(window.innerHeight - r.bottom); }"
        )
        assert gap > 20, f"newest card sits {gap}px from the viewport bottom"

        # And turning it off puts the page back the way it was.
        page.click("#followUpdates")
        assert not page.evaluate(
            "() => document.body.classList.contains('live-following')"
        )

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
        # The follow toggle is in the markup of every transcript page, so
        # that a served page and the same file on disk render identically.
        # It must stay hidden here: `file://` can never poll, and a visible
        # control would promise something it cannot do.
        follow = page.locator("#followUpdates")
        assert follow.count() == 1
        assert "live-active" not in (follow.get_attribute("class") or "")
        assert not follow.is_visible()
