// Keep this page current while the session it shows is still running.
//
// Only active over http(s): a page loaded from file:// cannot fetch
// anything at all — not itself, not a sibling, not even a HEAD (verified
// in Chromium; script tags are the only channel a file:// page has). So
// this is a `serve` feature, and the generated HTML stays exactly as
// useful from file:// as it was before.
//
// The shape, and why:
//
//   * The server never renders. `serve --watch` re-runs the ordinary
//     conversion and the files on disk stay canonical, so this page just
//     re-fetches its own URL. A conditional GET makes the idle case free
//     (304 in ~1ms) and needs no endpoint of its own.
//   * We swap #transcript rather than reloading. A reload loses fold
//     state and re-parses a document that can reach tens of MB; a swap
//     keeps scroll position for free, because everything above the
//     viewport is untouched.
//   * We do not patch in individual messages. New entries do not always
//     belong at the end (a transcript's appends are not in timestamp
//     order), one entry can render as several cards, and the `msg-d-N`
//     anchors are positional — inserting anywhere but the tail renumbers
//     them and breaks the fork/tool-pair links already on the page.
//     Replacing the whole container sidesteps all three.
(function () {
    'use strict';

    if (location.protocol !== 'http:' && location.protocol !== 'https:') return;

    // A conditional GET costs ~1ms and 304s while nothing changes, so the
    // interval is set by how fresh the page should feel, not by load.
    const POLL_MS = 1000;
    const container = () => document.getElementById('transcript');
    if (!container()) return;

    // The page's identity as of the last poll. `Last-Modified` alone is
    // not enough: HTTP dates have **one-second granularity**, so two
    // conversions inside the same second produce an identical header and
    // the second update is invisible. Observed directly — a third append
    // never arrived until length was added to the comparison.
    //
    // (This is the same trap as the cache's mtime tolerance, one layer up,
    // and it has the same fix: compare the size too. `Content-Length` is
    // exact, free, and already on the HEAD response.)
    let lastStamp = null;
    let stopped = false;
    let following = false;

    // ---- state that a swap would otherwise destroy -----------------------

    // A key that survives a re-render, for every card that can hold state.
    //
    // `data-uuid` is stable but is NOT unique per card (one entry can
    // render as sibling text + tool_use cards), so it is paired with its
    // ordinal among cards sharing it. Two kinds of card have no uuid at
    // all and are exactly the ones that fold: **session headers** (keyed
    // by `data-session-id`) and fork points. Missing the session header
    // is not a corner case — on a single-session page it is the only
    // foldable node there is.
    //
    // The `id` (`msg-d-N`) is unique but positional, so it is the last
    // resort rather than the first choice: it is correct for appends at
    // the tail and wrong the moment something lands earlier in the tree.
    function stableKeys(root) {
        const seen = new Map();
        const keys = new Map();
        root.querySelectorAll('.message, .fork-point').forEach(el => {
            const uuid = el.getAttribute('data-uuid');
            const session = el.getAttribute('data-session-id');
            let base;
            if (uuid) base = 'u:' + uuid;
            else if (session) base = 's:' + session;
            else base = 'p:' + (el.id || 'anon');
            const n = seen.get(base) || 0;
            seen.set(base, n + 1);
            keys.set(el, base + '#' + n);
        });
        return keys;
    }

    // The children container a card's fold bar controls: a *sibling* of
    // the card inside the shared `.message-node`, not a descendant.
    function childrenOf(el) {
        const node = el.closest('.message-node');
        return node ? node.querySelector(':scope > .children') : null;
    }

    function captureState(root) {
        const folds = new Map();
        const keys = stableKeys(root);
        keys.forEach((key, el) => {
            const children = childrenOf(el);
            if (children) folds.set(key, children.style.display);
        });
        const details = new Map();
        root.querySelectorAll('details').forEach((d, i) => details.set(i, d.open));
        return { folds, details, keys: new Set(keys.values()) };
    }

    function restoreState(root, state) {
        stableKeys(root).forEach((key, el) => {
            if (!state.folds.has(key)) return;
            const children = childrenOf(el);
            if (!children) return;
            children.style.display = state.folds.get(key);
            // Keep the fold bar's arrows honest about what it is showing.
            const bar = el.querySelector(':scope > .fold-bar');
            if (!bar) return;
            const folded = children.style.display === 'none';
            bar.querySelectorAll('.fold-bar-section').forEach(section => {
                section.classList.toggle('folded', folded);
            });
        });
        // `<details>` has no stable identity of its own; index order is the
        // best available and is exact for the common case (appends at the
        // tail leave every earlier disclosure at the same index).
        const all = root.querySelectorAll('details');
        state.details.forEach((open, i) => {
            if (all[i]) all[i].open = open;
        });
    }

    function markNew(root, previousKeys) {
        let count = 0;
        stableKeys(root).forEach((key, el) => {
            if (previousKeys.has(key) || !el.classList.contains('message')) return;
            el.classList.add('live-new');
            count += 1;
        });
        return count;
    }

    // ---- the update ------------------------------------------------------

    function announce(added) {
        let pill = document.getElementById('live-update-pill');
        if (!pill) {
            pill = document.createElement('button');
            pill.id = 'live-update-pill';
            pill.className = 'floating-btn live-update-pill';
            pill.addEventListener('click', () => {
                following = !following;
                if (following) scrollToEnd();
                render();
            });
            document.body.appendChild(pill);
        }
        pill.dataset.following = following ? 'yes' : 'no';
        pill.unseen = (pill.unseen || 0) + added;
        render();

        function render() {
            if (following) pill.unseen = 0;
            pill.textContent = following
                ? '⏬ following'
                : (pill.unseen ? `⏬ ${pill.unseen} new` : '⏬ follow');
            pill.title = following
                ? 'Following new messages — click to stop'
                : 'Scroll to new messages as they arrive';
        }
    }

    function scrollToEnd() {
        const c = container();
        if (c) c.lastElementChild?.scrollIntoView({ block: 'end', behavior: 'smooth' });
    }

    async function applyUpdate(html) {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const next = doc.getElementById('transcript');
        const current = container();
        if (!next || !current) return;

        const before = captureState(current);
        current.replaceWith(next);
        restoreState(next, before);
        const added = markNew(next, before.keys);

        // Everything that decorated the old markup after load.
        if (window.claudeLogRehydrate) window.claudeLogRehydrate(next);

        // The title carries the message/token counts, and the session nav
        // its summaries; both go stale otherwise.
        const nextTitle = doc.getElementById('title');
        const title = document.getElementById('title');
        if (nextTitle && title) title.innerHTML = nextTitle.innerHTML;

        if (added) announce(added);
        if (following) scrollToEnd();
    }

    async function poll() {
        if (stopped) return;
        try {
            const head = await fetch(location.href, { method: 'HEAD', cache: 'no-store' });
            const stamp = [
                head.headers.get('Last-Modified') || '',
                head.headers.get('Content-Length') || '',
                head.headers.get('ETag') || '',
            ].join('|');
            if (lastStamp === null) {
                lastStamp = stamp;
            } else if (stamp !== lastStamp) {
                lastStamp = stamp;
                const res = await fetch(location.href, { cache: 'no-store' });
                if (res.ok) await applyUpdate(await res.text());
            }
        } catch (err) {
            // A dropped server is the normal end of a watch session, not an
            // error worth shouting about. Keep polling: `serve` may come back.
            console.debug('live update poll failed', err);
        }
    }

    // Don't poll a page nobody is looking at.
    function schedule() {
        if (document.hidden) return;
        poll();
    }
    setInterval(schedule, POLL_MS);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) poll();
    });
    poll();

    window.claudeLogLiveUpdate = {
        poll,
        stop() { stopped = true; },
        setFollowing(v) { following = !!v; },
    };
})();
