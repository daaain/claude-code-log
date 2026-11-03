"""The served page updating itself while a session is still being written.

These are live-server browser tests by necessity: the feature only
activates over http, because a `file://` page cannot fetch anything —
not a sibling, not even itself. The rest of the browser suite runs from
`file://`, so it cannot cover this.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_code_log.converter import process_projects_hierarchy
from claude_code_log.watch import WatchEngine
from test.conftest import collect_page_errors

SESSION_ID = "dddddddd-eeee-ffff-0000-111111111111"


def _entry(uuid: str, text: str, parent: str | None = None) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-08-30T21:00:00Z",
                "parentUuid": parent,
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


def _reply(uuid: str, text: str, parent: str) -> str:
    """An assistant answer to ``parent`` — a second message type, so a
    type filter has something to hide and something to leave."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-30T21:00:01Z",
                "parentUuid": parent,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp/live",
                "sessionId": SESSION_ID,
                "version": "1.0.0",
                "uuid": uuid,
                "requestId": f"req-{uuid}",
                "message": {
                    "id": f"msg-{uuid}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        + "\n"
    )


@contextmanager
def _served(projects: Path, theme: str = "classic"):
    """Serve ``projects`` with a watcher re-rendering it on every change."""
    process_projects_hierarchy(projects, silent=True, theme=theme)

    from claude_code_log.server import ArchiveServer

    engine = WatchEngine(
        [projects],
        lambda _paths: process_projects_hierarchy(projects, silent=True, theme=theme),
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
        yield server.url
    finally:
        stop.set()
        thread.join(timeout=10)
        server.stop()


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
    with _served(projects) as url:
        yield url, project, jsonl


@pytest.fixture(params=["classic", "minimal"])
def conversation_archive(request: pytest.FixtureRequest, tmp_path: Path):
    """Like ``live_archive``, in either theme, with prompts and answers;
    every sixth prompt mentions ``needle`` (a search target)."""
    projects = tmp_path / "projects"
    project = projects / "-tmp-live"
    project.mkdir(parents=True)
    jsonl = project / f"{SESSION_ID}.jsonl"
    lines = []
    for i in range(24):
        needle = "needle " if i % 6 == 0 else ""
        lines.append(
            _entry(f"q-{i}", f"seed question {i} {needle}" + ("padding " * 40))
        )
        lines.append(
            _reply(f"a-{i}", f"seed answer {i} " + ("padding " * 40), f"q-{i}")
        )
    jsonl.write_text("".join(lines), encoding="utf-8")
    with _served(projects, request.param) as url:
        yield url, project, jsonl


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

    # Toggle the first session header's fold bar, reporting the children
    # container's `display` either side of the click. A working control
    # changes it; a dead listener leaves it exactly as it was.
    _CLICK_FOLD = (
        "() => { const bar = document.querySelector('#transcript .message"
        ".session-header > .fold-bar');"
        " const section = bar && bar.querySelector('.fold-bar-section');"
        " if (!section) return null;"
        " const children = section.closest('.message-node')"
        ".querySelector(':scope > .children');"
        " const before = children.style.display;"
        " section.click();"
        " return { before, after: children.style.display,"
        "   folded: section.classList.contains('folded') }; }"
    )

    def test_the_fold_control_still_works_after_an_update(
        self, page, live_archive
    ) -> None:
        """The fold bars were bound per element at load, and a live update
        replaces them: every append re-renders the bar of every ancestor
        (it carries their descendant count), and the swap replaces all of
        them at once. The listeners died with the elements, so one update
        was enough to leave every fold control on the page inert — while
        still *looking* exactly right, which is why nothing caught it.

        Asserting that the state survives an update is not the same
        assertion and passed throughout.
        """
        base, project, jsonl = live_archive
        self._open(page, base, project)

        first = page.evaluate(self._CLICK_FOLD)
        assert first is not None, "fixture has nothing foldable"
        assert first["before"] != first["after"], "the control was dead on load"
        page.evaluate(self._CLICK_FOLD)  # back to unfolded

        # Update one: the swap.
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("fold-1", "FOLD-ONE"))
        _wait_for(page, "() => !!document.querySelector('[data-uuid=\"fold-1\"]')")
        after_swap = page.evaluate(self._CLICK_FOLD)
        assert after_swap["before"] != after_swap["after"], (
            "the fold control stopped responding after the container swap"
        )
        page.evaluate(self._CLICK_FOLD)

        # Update two: the patch, which replaces the header card on its own
        # rather than the whole container.
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("fold-2", "FOLD-TWO"))
        _wait_for(page, "() => !!document.querySelector('[data-uuid=\"fold-2\"]')")
        after_patch = page.evaluate(self._CLICK_FOLD)
        assert after_patch["before"] != after_patch["after"], (
            "the fold control stopped responding after the patch"
        )

    def test_an_update_during_page_load_is_not_absorbed(
        self, page, live_archive
    ) -> None:
        """The first poll cannot adopt whatever the server holds by then.

        It only runs once the document has loaded, and that is exactly the
        window this feature's pages are slow in — tens of MB. A conversion
        finishing in it would become the baseline, so the page would sit
        one update behind until something *else* changed, which for a
        session that has just gone quiet is forever.
        """
        base, project, jsonl = live_archive
        url = f"{base}/{project.name}/session-{SESSION_ID}.html"
        page_file = project / f"session-{SESSION_ID}.html"
        served_stale: list[bool] = []

        def hold(route):
            # Only the navigation: the page's own HEAD and GET share this
            # URL and must reach the server as usual.
            if route.request.resource_type != "document" or served_stale:
                route.continue_()
                return
            # Take the document as it is now, then let the session move on
            # and the watch reconvert *before* handing it to the browser.
            response = route.fetch()
            body = response.body()
            served_stale.append(b"LOAD-RACE-MARKER" not in body)
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(_entry("load-race", "LOAD-RACE-MARKER"))
            deadline = time.time() + 20
            while time.time() < deadline:
                if "LOAD-RACE-MARKER" in page_file.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            route.fulfill(status=response.status, headers=response.headers, body=body)

        page.route(url, hold)
        self._open(page, base, project)
        assert served_stale == [True], "the browser was served the new page after all"

        _wait_for(page, "() => document.body.innerText.includes('LOAD-RACE-MARKER')")

    def test_an_open_timeline_picks_up_new_cards(self, page, live_archive) -> None:
        """The timeline is the one rehydrate hook that reads the whole page.

        It is therefore called once per changed element and coalesced to a
        single rebuild per update — so this asserts the rebuild still
        happens at all, which the coalescing is the only thing standing
        between the timeline and.
        """
        base, project, jsonl = live_archive
        self._open(page, base, project)
        page.locator("#toggleTimeline").click()
        # The library is fetched from a CDN on first open.
        page.wait_for_selector(".vis-item", timeout=30000)
        before = page.locator(".vis-item").count()

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("timeline-1", "TIMELINE-MARKER"))
        _wait_for(page, "() => document.body.innerText.includes('TIMELINE-MARKER')")

        _wait_for(
            page,
            "() => [...document.querySelectorAll('.vis-item')]"
            ".some(el => el.innerText.includes('TIMELINE-MARKER'))",
            timeout=10000,
        )
        assert page.locator(".vis-item").count() > before

    def test_a_replaced_fold_bar_still_describes_its_own_subtree(
        self, page, live_archive
    ) -> None:
        """The card carries the fold bar; the children container carries the
        fold *state*. An update replaces the first and not the second, so
        the bar comes back with the server's default "unfolded" icons over
        a subtree that is still hidden. The next click then folds what is
        already folded and appears to do nothing at all.
        """
        base, project, jsonl = live_archive
        self._open(page, base, project)

        folded = page.evaluate(self._CLICK_FOLD)
        assert folded["after"] == "none", "the first click should fold"
        assert folded["folded"], "the bar did not mark itself folded"

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("desync-1", "DESYNC-ONE"))
        _wait_for(page, "() => !!document.querySelector('[data-uuid=\"desync-1\"]')")

        state = page.evaluate(
            "() => { const bar = document.querySelector('#transcript .message"
            ".session-header > .fold-bar');"
            " const section = bar.querySelector('.fold-bar-section');"
            " const children = section.closest('.message-node')"
            ".querySelector(':scope > .children');"
            " return { hidden: children.style.display === 'none',"
            "   folded: section.classList.contains('folded'),"
            "   icon: section.querySelector('.fold-icon').textContent }; }"
        )
        assert state["hidden"], "the subtree unfolded itself across the update"
        assert state["folded"], "the replaced bar forgot it was folded"
        assert state["icon"] == "⏵", f"the icon disagrees with the subtree: {state}"

        # And it is still a working toggle, not merely a correct label.
        again = page.evaluate(self._CLICK_FOLD)
        assert again["after"] != "none", "the next click did not unfold"

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
        what gives the scroll somewhere to go — 20px of it, which lands the
        card 36px clear once the container's own margin is counted."""
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

    def test_a_same_size_rewrite_inside_one_second_is_seen(
        self, page, tmp_path: Path
    ) -> None:
        """`Last-Modified` + `Content-Length` are both blind to a rewrite
        that changes content without changing size — a re-render where a
        counter, a status word or a timestamp keeps its width — and
        inside one second neither header moves at all. The page must
        still notice, via the server's content digest.

        The page is rewritten directly rather than through a conversion:
        the point is a specific pair of bytes on the wire, and the two
        mtimes are set half a second apart inside one whole second so
        this *is* the same-second case rather than usually being it.
        """
        projects = tmp_path / "projects"
        project = projects / "-tmp-samesize"
        project.mkdir(parents=True)
        (project / f"{SESSION_ID}.jsonl").write_text(
            _entry("only", "MARKER-AAAA"), encoding="utf-8"
        )
        process_projects_hierarchy(projects, silent=True)
        rendered = project / f"session-{SESSION_ID}.html"

        second_ns = 1_700_000_000_000_000_000
        os.utime(rendered, ns=(second_ns, second_ns))
        size_before = rendered.stat().st_size

        from claude_code_log.server import ArchiveServer

        with ArchiveServer(projects, port=0) as server:
            self._open(page, server.url, project)
            _wait_for(page, "() => document.body.innerText.includes('MARKER-AAAA')")

            html = rendered.read_text(encoding="utf-8")
            assert "MARKER-AAAA" in html
            rendered.write_text(
                html.replace("MARKER-AAAA", "MARKER-BBBB"), encoding="utf-8"
            )
            assert rendered.stat().st_size == size_before, (
                "the rewrite has to keep the size for this to test anything"
            )
            os.utime(rendered, ns=(second_ns + 500_000_000, second_ns + 500_000_000))

            _wait_for(page, "() => document.body.innerText.includes('MARKER-BBBB')")

    def test_a_slow_response_cannot_overwrite_a_newer_one(
        self, page, live_archive
    ) -> None:
        """The interval keeps firing while a full GET is in flight, so on a
        page slow enough to fetch — the large page all of this is for — two
        updates can be in the air at once and the *last response* wins.

        Asserting on the *end* state is not enough and was measured to be
        not enough: unserialised, the newest message appeared at 2.0s,
        vanished at 4.0s when the held body landed, and was restored at
        5.0s by the following poll. So this watches for the regression
        itself — a message that was on screen and then was not.

        Simulated at the only thing that matters — an older response
        landing after a newer one — by holding the first full GET the page
        makes and appending again while it is held.
        """
        base, project, jsonl = live_archive
        self._open(page, base, project)

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("race-seed", "RACE-SEED"))
        _wait_for(page, "() => document.body.innerText.includes('RACE-SEED')")

        # Hold the *next* full GET's response for 3s (well past POLL_MS)
        # while letting its HEADs through, so the page keeps noticing
        # changes while the older body is still in the air. Sample the
        # rendered text throughout, so a message that comes and goes is
        # caught rather than averaged away by a final check.
        page.evaluate(
            "() => { const orig = window.fetch;"
            " window.__held = false;"
            " window.__lost = [];"
            " window.fetch = function (input, init) {"
            "   const p = orig.apply(this, arguments);"
            "   if (init && init.method === 'HEAD') return p;"
            "   if (window.__held) return p;"
            "   window.__held = true;"
            "   return p.then(res => new Promise(r => setTimeout(() => r(res), 3000)));"
            " };"
            " const seen = new Set();"
            " setInterval(() => { const t = document.body.innerText;"
            "   for (const m of ['RACE-ONE', 'RACE-TWO']) {"
            "     if (t.includes(m)) seen.add(m);"
            "     else if (seen.has(m)) window.__lost.push(m);"
            "   } }, 100); }"
        )

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("race-one", "RACE-ONE"))
        # The held GET has been issued: its body is the render carrying
        # RACE-ONE but not RACE-TWO.
        _wait_for(page, "() => window.__held === true")

        with jsonl.open("a", encoding="utf-8") as f:
            f.write(_entry("race-two", "RACE-TWO"))

        _wait_for(
            page,
            "() => document.body.innerText.includes('RACE-ONE')"
            " && document.body.innerText.includes('RACE-TWO')",
        )
        # Both are on screen. Wait past the held response, and past the
        # poll after it that would paper over the damage.
        page.wait_for_timeout(4000)

        text = page.evaluate("() => document.body.innerText")
        assert "RACE-ONE" in text, "the newer render dropped an earlier message"
        assert "RACE-TWO" in text, "a stale response overwrote the newer page"
        assert page.evaluate("() => window.__lost") == [], (
            "a message left the page after arriving: a stale response was applied"
        )

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


# The cards a live update brought, by `data-uuid` (both themes render it).
_CARD = "document.querySelector('#transcript .message[data-uuid=\"{}\"]')"


@pytest.mark.browser
class TestLiveUpdateKeepsFilterAndSearch:
    """A live update brings the server's markup: no ``filtered-hidden`` and
    none of the search's classes. The page re-applies an active filter and
    re-runs an active search over it, once per update — on a patch and on
    the wholesale swap — in both themes, and quietly: nothing scrolls,
    unfolds or opens under the reader (dev-docs/minimal-theme.md § 11)."""

    def _open(self, page, base: str, project: Path) -> list[str]:
        """Like ``TestLiveUpdate._open``, but a dropped live-update poll is
        not an error (see ``collect_page_errors``)."""
        url = f"{base}/{project.name}/session-{SESSION_ID}.html"
        errors = collect_page_errors(page, url)
        page.goto(url)
        page.wait_for_selector("#transcript")
        return errors

    _TAG_CARDS = TestLiveUpdate._TAG_CARDS
    _COUNT_TAGGED = TestLiveUpdate._COUNT_TAGGED

    def _prepare_route(self, page, jsonl: Path, route: str) -> None:
        """One update first (the first one after load swaps: the page has no
        card hashes yet), then tag every card, and for ``swap`` renumber one
        so the next update cannot patch (as in
        ``test_the_swap_is_the_fallback_when_the_ids_move``)."""
        self._arrive(page, jsonl, _entry("warm-up", "WARM-UP"), wait_for="warm-up")
        page.evaluate(self._TAG_CARDS)
        if route == "swap":
            page.evaluate(
                "() => { const els = [...document.querySelectorAll('#transcript .message')];"
                " els[Math.floor(els.length / 2)].id = 'msg-d-999999'; }"
            )

    def _check_route(self, page, route: str) -> None:
        kept = page.evaluate(self._COUNT_TAGGED)["kept"]
        if route == "swap":
            assert kept == 0, "the update patched: the swap was not exercised"
        else:
            assert kept > 0, "every card was rebuilt: the patch did not run"

    def _arrive(self, page, jsonl: Path, *entries: str, wait_for: str) -> None:
        with jsonl.open("a", encoding="utf-8") as f:
            f.write("".join(entries))
        _wait_for(page, f"() => !!{_CARD.format(wait_for)}")

    @pytest.mark.parametrize("route", ["patch", "swap"])
    def test_a_filter_hides_what_an_update_brings(
        self, page, conversation_archive, route: str
    ) -> None:
        base, project, jsonl = conversation_archive
        errors = self._open(page, base, project)
        page.click("#filterMessages")
        page.click('.filter-toggle[data-type="assistant"]')
        _wait_for(
            page,
            "() => [...document.querySelectorAll('#transcript .message.assistant')]"
            ".every(el => el.classList.contains('filtered-hidden'))",
        )
        self._prepare_route(page, jsonl, route)

        self._arrive(
            page,
            jsonl,
            _entry("q-new", "FILTER-QUESTION"),
            _reply("a-new", "FILTER-ANSWER", "q-new"),
            wait_for="a-new",
        )
        self._check_route(page, route)
        _wait_for(
            page,
            f"() => {_CARD.format('a-new')}.classList.contains('filtered-hidden')",
            timeout=5000,
        )
        state = page.evaluate(
            "() => ({"
            " shownAnswers: [...document.querySelectorAll('#transcript .message.assistant')]"
            "   .filter(el => el.checkVisibility()).map(el => el.id),"
            f" question: {_CARD.format('q-new')}.checkVisibility(),"
            " hiddenQuestions: document.querySelectorAll("
            "   '#transcript .message.user.filtered-hidden').length,"
            " count: document.querySelector('.filter-toggle[data-type=\"assistant\"] .count')"
            "   .textContent })"
        )
        assert state["shownAnswers"] == [], "an answer shows although filtered out"
        assert state["question"], "the filter hid a prompt it lets through"
        assert state["hiddenQuestions"] == 0
        assert state["count"] == "(0/25)", "the toggle's count missed the update"
        assert errors == []

    @pytest.mark.parametrize("route", ["patch", "swap"])
    def test_a_search_is_rerun_quietly_over_what_an_update_brings(
        self, page, conversation_archive, route: str
    ) -> None:
        base, project, jsonl = conversation_archive
        errors = self._open(page, base, project)
        page.click("#filterMessages")
        page.fill("#searchInput", "needle")
        _wait_for(
            page,
            "() => document.querySelectorAll('.message.search-match').length === 4",
        )
        # Make the last match the current one: a search re-run that is not
        # quiet scrolls back to it (and a re-run from scratch to the first).
        for _ in range(3):
            page.press("#searchInput", "Enter")
        page.wait_for_timeout(800)  # the smooth scroll lands
        assert page.evaluate(
            "document.getElementById('searchResultCount').textContent"
        ).startswith("4 of 4")
        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        assert page.evaluate("window.scrollY") == 0
        matches = page.evaluate(
            "[...document.querySelectorAll('.message.search-match')].map(el => el.dataset.uuid)"
        )
        self._prepare_route(page, jsonl, route)
        page.wait_for_timeout(800)
        assert page.evaluate("window.scrollY") == 0, "the warm-up update moved the page"

        self._arrive(
            page,
            jsonl,
            _entry("q-new", "SEARCH-QUESTION mentions the needle"),
            _reply("a-new", "SEARCH-ANSWER does not", "q-new"),
            wait_for="a-new",
        )
        self._check_route(page, route)
        _wait_for(
            page,
            f"() => {_CARD.format('q-new')}.classList.contains('search-match')"
            f" && {_CARD.format('a-new')}.classList.contains('search-hidden')",
            timeout=5000,
        )
        page.wait_for_timeout(800)  # a stray (smooth) scroll would have landed
        state = page.evaluate(
            "() => ({"
            " matches: [...document.querySelectorAll('.message.search-match')]"
            "   .map(el => el.dataset.uuid),"
            " shownUnmatched: [...document.querySelectorAll("
            "   '#transcript .message:not(.session-header):not(.search-match)')]"
            "   .filter(el => el.checkVisibility()).map(el => el.id),"
            " highlighted: !!document.querySelector("
            "   '#transcript .message[data-uuid=\"q-new\"] .search-highlight'),"
            " count: document.getElementById('searchResultCount').textContent,"
            " scrollY: window.scrollY })"
        )
        assert state["matches"] == matches + ["q-new"]
        assert state["shownUnmatched"] == [], "the search let a non-match through"
        assert state["highlighted"], "the new match is not highlighted"
        # The current match is still the one the reader chose.
        assert state["count"].startswith("4 of 5"), state["count"]
        assert state["scrollY"] == 0, "the refresh moved the page"
        assert errors == []
