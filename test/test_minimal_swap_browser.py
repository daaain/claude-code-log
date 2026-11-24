"""A live update's swap in the minimal theme, and what it costs the browser.

A swap (live_update.js's fallback when a patch can't apply) puts a fresh
``#transcript`` in place of the old one, which every rehydrate hook and then
the DAG engine decorate. On a large page each full layout is expensive,
in WebKit most of all, so a swap should lay the page out once: the
engine's grid.

**Many rows at once** (minimal_dag.js, relayout). Every card is a row of the transcript's grid, placed by an inline
``grid-row``. A swap (live_update.js's fallback when a patch can't apply)
hands the engine a fresh transcript whose every row must be written. WebKit
restyles each of those cards on its own once the page has been laid out,
and the cost grows with the square of the count: measured in Safari 27 on a
2,465-card session, the rail's first measurement after the writes took 14 to
17 s, against 0.06 s in Chromium. Written while the grid is off (no
``dag-on`` on the stage), the same writes cost one full restyle.

Chromium shows no slowdown, so the order of the writes is what is checked
there; the time is checked in WebKit, where it is installed (CI's `engines`
job has it), on the sample project: its 1,436 cards took 49 s to swap in
Playwright's WebKit 27.2 before, 1.3 s after. Card count alone doesn't
bring it on: 1,600 plain question/answer cards swapped in 0.7 s before.

**Nothing measures before the grid.** Session summaries (minimal.js) are
measured for their "+ more" toggle; measured as the hooks run, before the
engine has placed the new rows, that laid the whole page out once more just
to throw it away: 0.92 s of a 1.57 s swap of the 2,465-card session in
WebKit, 0.22 s of 0.48 s in Chromium.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import load_directory_transcripts, load_transcript
from claude_code_log.html.renderer import HtmlRenderer
from test.conftest import launch_engine
from test.test_follow_from_disk import _append, _archive, _has_text
from test.test_live_update import _entry, _reply

pytestmark = pytest.mark.browser

TEST_DATA = Path(__file__).parent / "test_data"

SAMPLE = TEST_DATA / "real_projects" / "-Users-dain-workspace-claude-code-log-sample"

# The transcript as the page was served, before the engine wrote anything:
# what a swap brings in. The listener is added before the page's own, so it
# runs before the engine's start().
_KEEP_SERVED = """
document.addEventListener('DOMContentLoaded', () => {
    const t = document.getElementById('transcript');
    if (t) window.__served = t.outerHTML;
});
"""

# Records every class change on the stage and every inline style change on
# a card, in order, then returns them with whether the stage had `dag-on`
# at each point.
_RECORD = """() => {
    const stage = document.querySelector('.mn-stage');
    window.__records = [];
    window.__recorder = new MutationObserver(list => window.__records.push(...list));
    window.__recorder.observe(document.documentElement, {
        subtree: true, attributes: true, attributeOldValue: true,
        attributeFilter: ['class', 'style'],
    });
}"""

_COLLECT = """() => {
    const stage = document.querySelector('.mn-stage');
    window.__records.push(...window.__recorder.takeRecords());
    window.__recorder.disconnect();
    const records = window.__records;
    // The stage's class after each of its own records: the next record's old
    // value, or what it is now.
    const stageRecords = records.filter(r => r.target === stage && r.attributeName === 'class');
    const has = cls => / dag-on /.test(' ' + (cls || '') + ' ');
    let on = stageRecords.length ? has(stageRecords[0].oldValue) : has(stage.className);
    let seen = 0, toggles = 0;
    const rowWrites = [];
    records.forEach(r => {
        if (r.target === stage && r.attributeName === 'class') {
            seen++;
            const after = seen < stageRecords.length ? stageRecords[seen].oldValue : stage.className;
            if (has(after) !== on) toggles++;
            on = has(after);
        } else if (r.attributeName === 'style' && r.target.classList.contains('message')
                   && (r.target.getAttribute('style') || '').includes('grid-row')
                   && !(r.oldValue || '').includes('grid-row')) {
            rowWrites.push(on);
        }
    });
    return {rowWrites: rowWrites.length, whileOn: rowWrites.filter(Boolean).length,
            toggles, onAtEnd: stage.classList.contains('dag-on')};
}"""

# live_update.js's swap: the served transcript in place of the current one,
# then the rehydrate hooks (the engine's relayout among them).
_SWAP = """() => {
    const next = new DOMParser().parseFromString(window.__served, 'text/html')
        .getElementById('transcript');
    document.getElementById('transcript').replaceWith(next);
    window.claudeLogRehydrate(next);
}"""

# Every layout read of the stage or the page as a whole, and whether the engine had placed
# the transcript's rows by then (a swapped-in transcript has none).
_WATCH_READS = """() => {
    window.__reads = [];
    const record = (name, el) => {
        const stage = document.querySelector('.mn-stage');
        const page = el === document.documentElement || el === document.body;
        if (!stage || !(page || stage.contains(el))) return;
        const cards = document.querySelectorAll('#transcript .message');
        window.__reads.push({
            name, cls: String(el.className).slice(0, 40),
            placed: cards.length > 0 && [...cards].every(c => c.style.gridRow),
            stack: (new Error().stack || '').split('\\n').slice(2, 4).join(' | '),
        });
    };
    [[Element.prototype, ['clientWidth', 'clientHeight', 'scrollWidth', 'scrollHeight']],
     [HTMLElement.prototype, ['offsetTop', 'offsetLeft', 'offsetWidth', 'offsetHeight']]]
        .forEach(([proto, names]) => names.forEach(name => {
            const d = Object.getOwnPropertyDescriptor(proto, name);
            Object.defineProperty(proto, name, Object.assign({}, d, {
                get() { record(name, this); return d.get.call(this); },
            }));
        }));
    ['getBoundingClientRect', 'getClientRects'].forEach(name => {
        const f = Element.prototype[name];
        Element.prototype[name] = function () { record(name, this); return f.apply(this, arguments); };
    });
}"""

# A patch's worth: one new card at the end.
_APPEND_ONE = """() => {
    // The innermost last node: one card, no children of its own.
    const nodes = document.querySelectorAll('#transcript .message-node');
    const last = nodes[nodes.length - 1];
    const copy = last.cloneNode(true);
    copy.querySelectorAll('[data-uuid]').forEach(el => el.setAttribute('data-uuid', 'appended-' + el.getAttribute('data-uuid')));
    copy.querySelectorAll('[id]').forEach(el => (el.id = 'appended-' + el.id));
    copy.querySelectorAll('.message').forEach(el => el.style.removeProperty('grid-row'));
    last.after(copy);
    window.claudeLogRehydrate(copy);
}"""


def _page(tmp_path: Path, pairs: int) -> Path:
    """A minimal-theme session of ``pairs`` question/answer turns."""
    jsonl = tmp_path / "bulk.jsonl"
    lines: list[str] = []
    parent: str | None = None
    for i in range(pairs):
        lines.append(_entry(f"q-{i}", f"question {i} " + "padding " * 20, parent))
        lines.append(_reply(f"a-{i}", f"answer {i} " + "padding " * 20, f"q-{i}"))
        parent = f"a-{i}"
    jsonl.write_text("".join(lines), encoding="utf-8")
    return _render(load_transcript(jsonl, silent=True), None, tmp_path / "bulk.html")


def _render(entries: Any, tree: Any, out: Path) -> Path:
    renderer = HtmlRenderer()
    renderer.theme = "minimal"
    out.write_text(
        renderer.generate(entries, out.stem, session_tree=tree), encoding="utf-8"
    )
    return out


def _open(page: Any, html: Path) -> None:
    page.add_init_script(_KEEP_SERVED)
    page.goto(html.as_uri())
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    # Laid out, as a reader's page is by the time anything changes.
    page.evaluate("() => document.body.offsetHeight")


def _settle(page: Any) -> None:
    """The engine relayouts in a microtask after the rehydrate hooks, so by
    the time the swap's evaluate returns; two frames let anything it
    scheduled run too."""
    page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def test_a_swap_writes_its_rows_with_the_grid_off(page: Any, tmp_path: Path) -> None:
    _open(page, _page(tmp_path, 200))
    page.evaluate(_RECORD)
    page.evaluate(_SWAP)
    _settle(page)
    got = page.evaluate(_COLLECT)
    assert got["rowWrites"] >= 400, got
    assert got["whileOn"] == 0, got
    assert got["onAtEnd"], got


def test_one_new_card_leaves_the_grid_on(page: Any, tmp_path: Path) -> None:
    """Turning the grid off and on costs a full restyle; a few rows are
    cheaper written in place."""
    _open(page, _page(tmp_path, 200))
    page.evaluate(_RECORD)
    page.evaluate(_APPEND_ONE)
    _settle(page)
    got = page.evaluate(_COLLECT)
    assert 1 <= got["rowWrites"] < 10, got
    assert got["toggles"] == 0, got
    assert got["onAtEnd"], got


def test_a_swap_measures_nothing_before_the_rows_are_placed(
    page: Any, tmp_path: Path
) -> None:
    """The representative fixture has a session summary, which minimal.js
    measures for its "+ more"."""
    jsonl = TEST_DATA / "representative_messages.jsonl"
    _open(page, _render(load_transcript(jsonl, silent=True), None, tmp_path / "r.html"))
    assert page.locator("#transcript .mn-sh-sum").count() > 0
    page.evaluate(_WATCH_READS)
    page.evaluate(_SWAP)
    _settle(page)
    reads = page.evaluate("window.__reads")
    assert any(r["cls"].startswith("mn-sh-sum") for r in reads), reads
    early = [r for r in reads if not r["placed"]]
    assert early == [], early


def test_following_measures_nothing_before_the_rows_are_placed(
    page: Any, tmp_path: Path
) -> None:
    """The real thing, from disk: the first update after pressing follow
    swaps, the next one patches. Following scrolls to the end after each,
    and the scroll reads the page's height: a layout, which must wait for
    the engine to place the new cards."""
    projects, jsonl, html = _archive(tmp_path, theme="minimal")
    page.goto(html.as_uri())
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    page.locator("#followUpdates").click()
    page.wait_for_function("window.claudeLogLiveUpdate.active")
    for marker in ("PLACED-SWAP", "PLACED-PATCH"):
        page.evaluate(_WATCH_READS)
        _append(projects, jsonl, marker, theme="minimal")
        page.wait_for_function(_has_text(marker), timeout=15000)
        _settle(page)
        reads = page.evaluate("window.__reads")
        early = [r for r in reads if not r["placed"]]
        assert early == [], (marker, early)


def test_a_new_card_is_marked_by_paint_alone(page: Any, tmp_path: Path) -> None:
    """In WebKit an opacity or transform animation on a card of the grid lays
    the whole grid out as it starts and as it ends (0.45 s of each update
    on a 2,465-card session); a box-shadow only repaints."""
    projects, jsonl, html = _archive(tmp_path, theme="minimal")
    page.goto(html.as_uri())
    page.wait_for_function("window.claudeLogDag && window.claudeLogDag.timing()")
    page.locator("#followUpdates").click()
    page.wait_for_function("window.claudeLogLiveUpdate.active")
    _append(projects, jsonl, "TINTED", theme="minimal")
    page.wait_for_function(_has_text("TINTED"), timeout=15000)
    animated = page.evaluate(
        """() => document.getAnimations()
            .filter(a => a.effect && a.effect.target && a.effect.target.classList.contains('live-new'))
            .map(a => ({name: a.animationName,
                        props: [...new Set(a.effect.getKeyframes().flatMap(k => Object.keys(k)))]
                            .filter(k => !['offset', 'computedOffset', 'easing', 'composite'].includes(k))}))"""
    )
    assert animated, "the new card is not marked at all"
    assert all(a["props"] == ["boxShadow"] for a in animated), animated


@pytest.mark.engines
def test_a_large_swap_is_quick_in_webkit(playwright: Any, tmp_path: Path) -> None:
    with launch_engine(playwright, "webkit") as browser:
        page = browser.new_page()
        entries, tree = load_directory_transcripts(SAMPLE, silent=True)
        _open(page, _render(entries, tree, tmp_path / "sample.html"))
        start = time.monotonic()
        page.evaluate(_SWAP)
        _settle(page)
        elapsed = time.monotonic() - start
        draw = page.evaluate("() => window.claudeLogDag.timing().draw")
        # 1.3 s with the writes made while the grid is off, 49 s without
        # (an M-series Mac): far enough apart for a slow machine.
        assert elapsed < 10, (elapsed, draw)
