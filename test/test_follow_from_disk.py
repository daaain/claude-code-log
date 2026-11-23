"""Following a page opened from disk while ``watch`` rewrites it.

A ``file://`` page cannot fetch anything, so it reads itself through a
hidden frame of itself instead (live_probe.js, live_probe_post.js, and the
disk transport in live_update.js), and only while the reader has pressed
follow. These run from ``file://`` and re-render with the ordinary
conversion, which is all ``watch`` does on a tick.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import process_projects_hierarchy
from test.test_live_update import SESSION_ID, _entry, _reply

# Counts the frames the page creates. Init scripts run in every frame, so
# the counter lives on the top window.
_COUNT_FRAMES = """
(() => {
  if (window.top !== window) return;
  window.__frames = 0;
  const create = Document.prototype.createElement;
  Document.prototype.createElement = function (tag, ...rest) {
    if (String(tag).toLowerCase() === 'iframe') window.__frames++;
    return create.call(this, tag, ...rest);
  };
})();
"""


def _archive(tmp_path: Path, theme: str = "classic") -> tuple[Path, Path, Path]:
    """A converted project; returns (projects dir, transcript, session page)."""
    projects = tmp_path / "projects"
    project = projects / "-tmp-disk"
    project.mkdir(parents=True)
    jsonl = project / f"{SESSION_ID}.jsonl"
    lines = []
    for i in range(20):
        lines.append(_entry(f"q-{i}", f"seed question {i} " + ("padding " * 40)))
        lines.append(
            _reply(f"a-{i}", f"seed answer {i} " + ("padding " * 40), f"q-{i}")
        )
    jsonl.write_text("".join(lines), encoding="utf-8")
    process_projects_hierarchy(projects, silent=True, theme=theme)
    return projects, jsonl, project / f"session-{SESSION_ID}.html"


def _append(projects: Path, jsonl: Path, text: str, theme: str = "classic") -> None:
    """One watch tick's worth: a line lands, the conversion runs.

    Each line continues from the last one, as a session's do: two lines
    sharing a parent would be a rewind, and render as a fork.
    """
    last = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[-1])["uuid"]
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(_entry(f"live-{text}", text, parent=last))
    process_projects_hierarchy(projects, silent=True, theme=theme)


def _errors(page: Any) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors


def _open(page: Any, html: Path) -> list[str]:
    errors = _errors(page)
    page.add_init_script(_COUNT_FRAMES)
    page.goto(html.as_uri())
    page.wait_for_selector("#transcript")
    page.wait_for_load_state("load")
    return errors


# A page the test can hide and show: `document.hidden` as the poller reads
# it, and the event that tells it to look again.
_FAKE_HIDDEN = """() => {
    window.__hidden = false;
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__hidden});
    window.__setHidden = hidden => {
        window.__hidden = hidden;
        document.dispatchEvent(new Event('visibilitychange'));
    };
}"""


# `Date.now()` with an offset the test can move, in every frame of the page
# (the probe frames read it too).
_SHIFTABLE_CLOCK = """
(() => {
  const top = window.top;
  const now = Date.now.bind(Date);
  if (window === top) window.__clockShift = 0;
  Date.now = () => now() + (top.__clockShift || 0);
})();
"""


# Hands every `message` event on the top window to its listeners late, by
# `window.__delayMessages` ms, and has every probe frame report `load` at
# once: an answer the event loop ran after the deadline a load starts.
_DELAY_MESSAGES = """
(() => {
  if (window.top !== window) return;
  window.__delayMessages = 0;
  const create = Document.prototype.createElement;
  Document.prototype.createElement = function (tag, ...rest) {
    const el = create.call(this, tag, ...rest);
    if (String(tag).toLowerCase() === 'iframe' && window.__delayMessages) {
      setTimeout(() => el.dispatchEvent(new Event('load')), 50);
    }
    return el;
  };
  const add = window.addEventListener, remove = window.removeEventListener;
  const late = new Map();
  window.addEventListener = function (type, fn, opts) {
    if (type !== 'message') return add.call(this, type, fn, opts);
    const wrapped = ev => {
      if (window.__delayMessages) setTimeout(() => fn(ev), window.__delayMessages);
      else fn(ev);
    };
    late.set(fn, wrapped);
    return add.call(this, type, wrapped, opts);
  };
  window.removeEventListener = function (type, fn, opts) {
    return remove.call(this, type, (type === 'message' && late.get(fn)) || fn, opts);
  };
})();
"""


def _status(page: Any) -> str:
    """What the follow last announced, through its `role=status` region."""
    return page.evaluate(
        "(document.querySelector('[role=status].live-status') || {}).textContent || ''"
    )


def _has_text(text: str) -> str:
    return f"() => document.getElementById('transcript').textContent.includes({text!r})"


@pytest.mark.browser
class TestFollowFromDisk:
    def test_nothing_is_read_until_follow_is_pressed(self, page, tmp_path) -> None:
        """A page nobody follows pays for two inert scripts and nothing else:
        no frame, no timer reading the file, no errors. Pressing follow again
        stops the reading."""
        _projects, _jsonl, html = _archive(tmp_path)
        errors = _open(page, html)

        follow = page.locator("#followUpdates")
        # Offered from disk too, now that it can do something there.
        assert follow.is_visible()
        time.sleep(2.5)
        assert page.evaluate("window.__frames") == 0, "the page read itself unasked"

        follow.click()
        page.wait_for_function("window.__frames >= 2", timeout=10000)
        follow.click()
        stopped_at = page.evaluate("window.__frames")
        time.sleep(2.5)
        assert page.evaluate("window.__frames") == stopped_at, (
            "still reading after stop"
        )
        assert page.locator("iframe").count() == 0, "a probe frame was left behind"
        assert errors == []

    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_a_rewrite_appears_in_place_while_following(
        self, page, tmp_path, theme
    ) -> None:
        projects, jsonl, html = _archive(tmp_path, theme)
        errors = _open(page, html)
        page.evaluate("window.__stillHere = 'yes'")
        page.locator("#followUpdates").click()

        _append(projects, jsonl, "DISK-MARKER-ONE", theme)
        page.wait_for_function(_has_text("DISK-MARKER-ONE"), timeout=15000)
        _append(projects, jsonl, "DISK-MARKER-TWO", theme)
        page.wait_for_function(_has_text("DISK-MARKER-TWO"), timeout=15000)

        assert page.evaluate("window.__stillHere") == "yes", "the page navigated"
        assert (
            page.evaluate(
                "document.querySelectorAll('#transcript .message.live-new').length"
            )
            >= 2
        ), "the new cards were not marked for the fade-in"
        assert errors == []

    def test_the_first_read_does_not_redo_the_page_on_screen(
        self, page, tmp_path
    ) -> None:
        """Pressing follow on an unchanged page must not rebuild it: the first
        read is compared with what is on screen, not applied blindly."""
        _projects, _jsonl, html = _archive(tmp_path)
        # Old enough that the first probe trusts the mtime ...
        old = time.time() - 60
        os.utime(html, (old, old))
        errors = _open(page, html)
        page.evaluate(
            "document.querySelectorAll('#transcript .message').forEach(e => e.__kept = 1)"
        )
        page.locator("#followUpdates").click()
        page.wait_for_function("window.__frames >= 2", timeout=10000)
        # ... and fresh enough that it reads in full and must compare.
        now = time.time()
        os.utime(html, (now, now))
        page.wait_for_function("window.__frames >= 4", timeout=10000)
        kept = page.evaluate(
            "[...document.querySelectorAll('#transcript .message')].every(e => e.__kept)"
        )
        assert kept, "an unchanged page was rebuilt"
        assert errors == []

    def test_a_rewrite_inside_the_same_second_is_seen(self, page, tmp_path) -> None:
        """``lastModified`` has one-second granularity, so a second rewrite in
        the second of the first leaves it unchanged. The page must read in
        full until the clock is past that second, or it misses the rewrite
        for good (nothing else would ever move the mtime again)."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        page.locator("#followUpdates").click()

        for _attempt in range(3):
            # Start just after a second turns over, to land both writes in it.
            while time.time() % 1 > 0.05:
                time.sleep(0.005)
            _append(projects, jsonl, f"SAME-A-{_attempt}")
            stamp = html.stat()
            page.evaluate("window.claudeLogLiveUpdate.poll()")
            page.wait_for_function(_has_text(f"SAME-A-{_attempt}"), timeout=10000)
            if int(time.time()) != int(stamp.st_mtime):
                continue  # too slow to stay inside the second: try again
            content = html.read_text(encoding="utf-8")
            html.write_text(
                content.replace(f"SAME-A-{_attempt}", f"SAME-B-{_attempt}"),
                encoding="utf-8",
            )
            os.utime(html, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            page.wait_for_function(_has_text(f"SAME-B-{_attempt}"), timeout=10000)
            return
        pytest.skip("could not land two rewrites inside one second on this machine")

    def test_a_same_second_rewrite_while_hidden_is_seen(self, page, tmp_path) -> None:
        """What makes a read safe is *when the render on screen was read*,
        not what the clock says now. Hidden straight after a read, the page
        stops probing; a second rewrite inside the first one's second
        leaves the mtime as it was; shown again seconds later, a probe
        trusting the clock asks whether the file is still at that mtime,
        is told it is, and keeps the older render for good."""
        projects, jsonl, html = _archive(tmp_path)
        # Both clocks are set, not raced: both rewrites carry an mtime a
        # minute ahead, so the read of the first is inside its second
        # however slow the machine; once hidden, the page's clock jumps two
        # minutes, past that second, before it is shown again.
        page.add_init_script(_SHIFTABLE_CLOCK)
        _open(page, html)
        page.evaluate(_FAKE_HIDDEN)
        page.locator("#followUpdates").click()
        ahead = time.time_ns() + 60 * 10**9
        _append(projects, jsonl, "HIDDEN-A")
        os.utime(html, ns=(ahead, ahead))
        page.wait_for_function(_has_text("HIDDEN-A"), timeout=10000)
        page.evaluate("window.__setHidden(true)")
        _append(projects, jsonl, "HIDDEN-B")
        os.utime(html, ns=(ahead, ahead))
        page.evaluate("window.__clockShift = 120000")
        page.evaluate("window.__setHidden(false)")
        page.wait_for_function(_has_text("HIDDEN-B"), timeout=10000)

    def test_a_failed_update_is_read_again(self, page, tmp_path) -> None:
        """An update that throws part-way must not be recorded as the render
        on screen: the next probe would send its mtime, be answered `same`,
        and the page would stay behind until the file changed again. The
        file here is written well in the past, so nothing else forces a
        full read."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        page.evaluate(
            """() => {
                const parse = DOMParser.prototype.parseFromString;
                window.__failed = 0;
                DOMParser.prototype.parseFromString = function (...args) {
                    if (!window.__failed++) throw new Error('one failed update');
                    return parse.apply(this, args);
                };
            }"""
        )
        page.locator("#followUpdates").click()
        _append(projects, jsonl, "FAILED-ONCE")
        past = time.time_ns() - 3600 * 10**9
        os.utime(html, ns=(past, past))
        page.wait_for_function(_has_text("FAILED-ONCE"), timeout=10000)
        assert page.evaluate("window.__failed") >= 2

    def test_an_update_that_fails_part_way_is_not_trusted(self, page, tmp_path) -> None:
        """A patch that throws after editing one card leaves the page half
        way. Neither the render recorded before it nor the card hashes the
        patch compares against describe the page any more: a later render
        that brings the file back to before must be applied in full, not
        found "unchanged" against either."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        page.locator("#followUpdates").click()
        # One good update first, so there is a patch baseline to go stale.
        _append(projects, jsonl, "BASELINE")
        page.wait_for_function(_has_text("BASELINE"), timeout=10000)
        before = html.read_text(encoding="utf-8")
        # The next update edits two cards, and fails at the second, every time.
        page.evaluate(
            """() => {
                const importNode = Document.prototype.importNode;
                window.__imports = 0;
                Document.prototype.importNode = function (...args) {
                    // Every second import: each retry edits one card, then fails.
                    if (++window.__imports % 2 === 0) throw new Error('part-way');
                    return importNode.apply(this, args);
                };
            }"""
        )
        edited = before.replace("seed answer 3 ", "EDITED-3 ").replace(
            "seed answer 5 ", "EDITED-5 "
        )
        assert edited != before
        html.write_text(edited, encoding="utf-8")
        page.wait_for_function(_has_text("EDITED-"), timeout=10000)
        page.wait_for_function("window.__imports >= 2", timeout=10000)
        # Back as it was, a moment later (a fresh mtime).
        time.sleep(1.1)
        html.write_text(before, encoding="utf-8")
        page.wait_for_function(
            "() => !document.getElementById('transcript').textContent.includes('EDITED-')",
            timeout=10000,
        )

    def test_a_restart_after_a_failed_update_still_repairs_the_page(
        self, page, tmp_path
    ) -> None:
        """Pressing follow again after an update failed part-way must not
        take the first-read shortcut: the half-edited page can still match
        the file by card count, last card and title, and would be taken as
        on screen, wrong card and all."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        page.locator("#followUpdates").click()
        _append(projects, jsonl, "BASELINE")
        page.wait_for_function(_has_text("BASELINE"), timeout=10000)
        before = html.read_text(encoding="utf-8")
        page.evaluate(
            """() => {
                const importNode = Document.prototype.importNode;
                window.__imports = 0;
                Document.prototype.importNode = function (...args) {
                    if (++window.__imports % 2 === 0) {
                        // Stop following right here, so no retry can
                        // repair the page before the restart does.
                        window.claudeLogLiveUpdate.setFollowing(false);
                        throw new Error('part-way');
                    }
                    return importNode.apply(this, args);
                };
            }"""
        )
        html.write_text(
            before.replace("seed answer 3 ", "EDITED-3 ").replace(
                "seed answer 5 ", "EDITED-5 "
            ),
            encoding="utf-8",
        )
        page.wait_for_function(_has_text("EDITED-"), timeout=10000)
        page.wait_for_function("window.__imports >= 2", timeout=10000)
        # Following stopped at the failure; put the file back, follow again.
        page.wait_for_function("!window.claudeLogLiveUpdate.active")
        time.sleep(1.1)
        html.write_text(before, encoding="utf-8")
        page.locator("#followUpdates").click()
        page.wait_for_function(
            "() => !document.getElementById('transcript').textContent.includes('EDITED-')",
            timeout=10000,
        )

    def test_a_restart_after_a_good_update_compares_with_it(
        self, page, tmp_path
    ) -> None:
        """Once a read has been applied, the page holds that render, and a
        restart must compare with it, not take the first-read shortcut: an
        edit to an earlier card while follow was off keeps the card count,
        the last card and the title, and would be taken as on screen."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        page.locator("#followUpdates").click()
        _append(projects, jsonl, "APPLIED")
        page.wait_for_function(_has_text("APPLIED"), timeout=10000)
        page.locator("#followUpdates").click()
        page.wait_for_function("!window.claudeLogLiveUpdate.active")
        time.sleep(1.1)
        text = html.read_text(encoding="utf-8")
        html.write_text(text.replace("seed answer 3 ", "WHILE-OFF "), encoding="utf-8")
        page.locator("#followUpdates").click()
        page.wait_for_function(_has_text("WHILE-OFF"), timeout=10000)

    def test_a_same_second_rewrite_before_following_is_seen(
        self, page, tmp_path
    ) -> None:
        """The page as loaded has the same gap: nothing says when within its
        mtime's second the file was read, so the first probe after pressing
        follow reads in full however long ago that second was."""
        projects, jsonl, html = _archive(tmp_path)
        _open(page, html)
        stamp = html.stat()
        _append(projects, jsonl, "BEFORE-FOLLOW")
        os.utime(html, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        time.sleep(2.5)
        page.locator("#followUpdates").click()
        page.wait_for_function(_has_text("BEFORE-FOLLOW"), timeout=10000)

    def test_another_page_cannot_read_it_through_the_probe(
        self, page, tmp_path
    ) -> None:
        """The framed copy posts its transcript only to a parent that names its
        token, which is in that page and nowhere else. A local page that
        frames it learns at most the mtime it already guessed exactly —
        never the content. Not even with a uuid of one of its cards, which
        an archive-search link carries (``?uuid=``, search.py), nor with
        another session page's token."""
        _projects, jsonl, html = _archive(tmp_path)
        page.goto(html.as_uri())
        lm = page.evaluate("document.lastModified")
        uuid = page.evaluate(
            "document.querySelector('#transcript .message[data-uuid]').dataset.uuid"
        )
        own = page.evaluate("window.__cclProbeToken")
        assert len(own) == 32 and own not in jsonl.read_text(encoding="utf-8")
        page.goto("about:blank")
        from claude_code_log.page_key import probe_token

        other = probe_token("another-session")

        attacker = tmp_path / "elsewhere" / "evil.html"
        attacker.parent.mkdir()
        attacker.write_text(
            """<!doctype html><body><script>
window.got = [];
window.addEventListener('message', ev => window.got.push(ev.data));
window.frameIt = (hash) => new Promise(resolve => {
  const f = document.createElement('iframe');
  f.src = %r + '#ccl-probe=' + encodeURIComponent(hash);
  document.body.appendChild(f);
  setTimeout(() => { f.remove(); resolve(); }, 1500);
});
</script></body>"""
            % html.as_uri(),
            encoding="utf-8",
        )
        page.goto(attacker.as_uri())
        guesses = ["~", "~not-a-card", "~" + "0" * 36, lm + "~", "~" + uuid]
        for guess in guesses + [lm + "~" + uuid, "~" + other]:
            page.evaluate("(h) => window.frameIt(h)", guess)
        got = page.evaluate("window.got")
        assert all(m.get("cclProbe") in ("refused", "same") for m in got), got
        assert not any("html" in m for m in got), "the transcript reached another page"
        # Guessing the mtime exactly earns `same`, which carries only that mtime.
        assert {"cclProbe": "same", "lm": lm} in got

    @pytest.mark.parametrize(
        "was,now", [("classic", "minimal"), ("minimal", "classic")]
    )
    def test_a_rewrite_in_another_theme_asks_for_a_reload(
        self, page, tmp_path, was, now
    ) -> None:
        """An update replaces the transcript and the title, not the styles,
        the body's class or the shell around them, which differ by theme. So
        a page that `watch` rewrote in the other theme is not patched into
        this one: following stops and the button says to reload."""
        projects, jsonl, html = _archive(tmp_path, was)
        errors = _open(page, html)
        follow = page.locator("#followUpdates")
        follow.click()
        page.wait_for_function("window.__frames >= 2", timeout=10000)

        _append(projects, jsonl, "OTHER-THEME", now)
        page.wait_for_function(
            "document.getElementById('followUpdates').getAttribute('aria-pressed') === 'false'",
            timeout=15000,
        )
        assert "reload" in (follow.get_attribute("title") or "")
        # Said aloud as well: focus may be in the transcript, not on the button.
        assert "reload" in _status(page)
        assert not page.evaluate(_has_text("OTHER-THEME")), "patched in another theme"
        assert errors == []

    def test_a_page_that_stops_answering_says_so_and_recovers(
        self, page, tmp_path, monkeypatch
    ) -> None:
        """A `watch` that can't read the install key rewrites the session page
        without the probe (page_key.probe_token_or_none), and that page never
        answers: no `refused`, nothing. The button shows the follow as lost,
        the page stops counting as live, and the probes go on, so the follow
        picks up again once the page answers."""
        from claude_code_log import page_key

        projects, jsonl, html = _archive(tmp_path)
        errors = _open(page, html)
        follow = page.locator("#followUpdates")
        follow.click()
        page.wait_for_function("window.__frames >= 2", timeout=10000)
        assert page.evaluate("window.claudeLogLiveUpdate.active")

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("a file where the key's folder would go")
        good_key = os.environ[page_key.ENV_KEY_FILE]
        monkeypatch.setenv(page_key.ENV_KEY_FILE, str(blocker / "page.key"))
        page_key.reset()
        try:
            _append(projects, jsonl, "NO-PROBE")
            assert "ccl-probe-token" not in html.read_text(encoding="utf-8")
            page.wait_for_function(
                "document.getElementById('followUpdates').classList.contains('live-lost')",
                timeout=20000,
            )
            assert "reload" in (follow.get_attribute("title") or "")
            assert "stopped answering" in _status(page)
            assert follow.get_attribute("aria-pressed") == "true", "still trying"
            assert not page.evaluate("window.claudeLogLiveUpdate.active")
        finally:
            monkeypatch.setenv(page_key.ENV_KEY_FILE, good_key)
            page_key.reset()

        _append(projects, jsonl, "PROBE-BACK")
        page.wait_for_function(_has_text("PROBE-BACK"), timeout=20000)
        assert not page.evaluate(
            "document.getElementById('followUpdates').classList.contains('live-lost')"
        )
        assert page.evaluate("window.claudeLogLiveUpdate.active")
        assert "again" in _status(page)
        assert errors == []

    def test_an_answer_that_lands_after_the_deadline_still_counts(
        self, page, tmp_path
    ) -> None:
        """No engine fires a frame's `load` once its page has answered (the
        answer stops the load), but nothing promises that, nor orders the
        posted answer against the timer a `load` starts. Here every frame
        reports `load` at once and every answer reaches the page after the
        one-second deadline: each must still be taken, and no probe counted
        as unanswered for it."""
        page.add_init_script(_DELAY_MESSAGES)
        projects, jsonl, html = _archive(tmp_path)
        errors = _open(page, html)
        page.evaluate("window.__delayMessages = 1200")
        page.locator("#followUpdates").click()

        _append(projects, jsonl, "LATE-ANSWER")
        page.wait_for_function(_has_text("LATE-ANSWER"), timeout=15000)
        time.sleep(5)
        assert not page.evaluate(
            "document.getElementById('followUpdates').classList.contains('live-lost')"
        ), "answers that arrived were counted as missing"
        _append(projects, jsonl, "LATE-AGAIN")
        page.wait_for_function(_has_text("LATE-AGAIN"), timeout=15000)
        assert errors == []

    def test_the_combined_page_answers_no_probe(self, page, tmp_path) -> None:
        """A card's uuid is no secret from someone holding one session (its
        page or its JSONL), and the combined page holds every session's
        cards: if it answered probes, one session's uuid would read them
        all. So only session pages answer; a combined page framed with a
        uuid it does hold gets no answer at all."""
        _projects, _jsonl, html = _archive(tmp_path)
        combined = html.parent / "combined_transcripts.html"
        assert combined.exists()
        page.goto(html.as_uri())
        uuid = page.evaluate(
            "document.querySelector('#transcript .message[data-uuid]').dataset.uuid"
        )
        page.goto("about:blank")
        attacker = tmp_path / "elsewhere" / "evil.html"
        attacker.parent.mkdir()
        attacker.write_text(
            """<!doctype html><body><script>
window.got = [];
window.addEventListener('message', ev => window.got.push(ev.data));
const f = document.createElement('iframe');
f.src = %r + '#ccl-probe=' + encodeURIComponent('~' + %r);
document.body.appendChild(f);
</script></body>"""
            % (combined.as_uri(), uuid),
            encoding="utf-8",
        )
        page.goto(attacker.as_uri())
        page.wait_for_timeout(2000)
        assert page.evaluate("window.got") == []

    def test_the_combined_page_offers_no_follow_from_disk(self, page, tmp_path) -> None:
        """Opened from disk, the combined page is the static page it was
        before following from disk existed: no follow button, no frame."""
        _projects, _jsonl, html = _archive(tmp_path)
        _open(page, html.parent / "combined_transcripts.html")
        page.wait_for_timeout(1500)
        assert not page.locator("#followUpdates").is_visible()
        assert page.evaluate("window.__frames") == 0
        assert not page.evaluate("!!window.claudeLogLiveUpdate")


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["firefox", "webkit"])
def test_following_from_disk_in_other_engines(playwright, tmp_path, engine) -> None:
    """The browser suite runs Chromium; the disk transport leans on behaviour
    that differs by engine (Firefox fires DOMContentLoaded after
    ``window.stop()``; WebKit reports the current time as a file's
    lastModified), so it is checked in each where they are installed."""
    try:
        browser = getattr(playwright, engine).launch()
    except Exception as exc:  # not installed: CI installs Chromium only
        pytest.skip(f"{engine} not available: {exc}")
    try:
        page = browser.new_page()
        projects, jsonl, html = _archive(tmp_path)
        errors = _open(page, html)
        page.locator("#followUpdates").click()
        _append(projects, jsonl, "ENGINE-MARKER")
        page.wait_for_function(_has_text("ENGINE-MARKER"), timeout=15000)
        assert errors == []
    finally:
        browser.close()


@pytest.mark.browser
def test_an_agent_reads_as_running_only_while_the_page_is_followed(
    page, tmp_path
) -> None:
    """A static page cannot tell an agent still at work from one that ended
    without a result, so the minimal theme draws "running" only on a page
    that is kept current. From disk, that is while it is followed."""
    from claude_code_log.converter import convert_jsonl_to
    from test.dag_live_fixture import LANE_C, LIVE_SESSION, LiveDagScript
    from test.test_minimal_dag_live_browser import _recent

    project = tmp_path / "projects" / "-tmp-live"
    LiveDagScript(project, base=_recent()).run_to("start")
    convert_jsonl_to(
        "html",
        project,
        silent=True,
        write_combined=False,
        generate_individual_sessions=True,
        theme="minimal",
    )
    errors = _open(page, project / f"session-{LIVE_SESSION}.html")
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    running = f"window.claudeLogDag.running({LANE_C!r})"
    assert not page.evaluate(running), "a static page drew the agent as running"

    page.locator("#followUpdates").click()
    page.wait_for_function(running, timeout=5000)
    page.locator("#followUpdates").click()
    page.wait_for_function(f"!{running}", timeout=5000)
    assert errors == []
