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
//     re-fetches its own URL. A HEAD for the page's own metadata makes
//     the idle case free (~1ms, no body) and needs no endpoint of its
//     own; the full GET follows only when that metadata moved.
//   * We never reload. A reload loses fold state and re-parses a
//     document that can reach tens of MB.
//   * When the new render extends the one on screen, we patch the nodes
//     that changed and leave the rest alone (see "patching" below). When
//     it does not, we replace #transcript wholesale — which keeps scroll
//     position for free, because everything above the viewport is
//     untouched, and is the fallback for every shape the patch declines:
//     entries that do not belong at the end (a transcript's appends are
//     not in timestamp order) and the `msg-d-N` renumbering that follows,
//     which would break the fork/tool-pair links already on the page.
(function () {
    'use strict';

    if (location.protocol !== 'http:' && location.protocol !== 'https:') return;

    // A metadata HEAD costs ~1ms and carries no body while nothing
    // changes, so the interval is set by how fresh the page should feel,
    // not by load.
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

    // ---- patching, for the case that is almost always the real one -------
    //
    // Replacing #transcript wholesale costs work proportional to the *page*
    // for a change proportional to the *append*: on a 4MB session page,
    // ~97ms of DOM work plus re-localising all 1,180 timestamps, to show two
    // new cards. It also reconstructs fold and disclosure state from a
    // heuristic key rather than keeping the nodes that already hold it.
    //
    // So when the new render is a pure *extension* of the one on screen —
    // the same cards, in the same order, followed by new ones — we patch
    // instead: replace the handful of cards whose own markup actually
    // changed, insert the new ones, and leave every other node untouched.
    // Measured on the same page: 2 cards inserted, 2 timestamps localised.
    //
    // Anything else falls back to the swap, which is unchanged and stays the
    // definition of correct. Replaying three real sessions through the
    // renderer, 45 of 47 growth steps were pure extensions; the other 2 were
    // out-of-order arrivals that renumbered the positional `msg-d-N` ids, so
    // they take the swap. That ratio is why the fallback is acceptable and
    // why patching the general case is not worth its complexity yet.

    // The hashes the cards on screen were rendered from, keyed by card id.
    // Taken from pristine parsed markup, never from the live DOM: by update
    // time the live tree has been rewritten by decoration (timestamp
    // localisation replaces innerHTML), so a hash taken from it would never
    // match one taken from the server's bytes.
    let cardHashes = null;

    // FNV-1a. A collision would show one stale card, not break the page, and
    // needs a *changed* card to land on its own previous value: 1 in 2^32.
    function hashOf(s) {
        let h = 0x811c9dc5;
        for (let i = 0; i < s.length; i++) {
            h ^= s.charCodeAt(i);
            h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
        }
        return h.toString(36);
    }

    // What belongs to a node itself rather than to its descendants. That is
    // the card — but not only the card: a fork point renders as a box inside
    // `.children` so that folding hides it with the subtree, and on a
    // fork-only slot that box is the node's *only* content and carries its
    // id. Both kinds also hold positional `#msg-d-N` branch links, so
    // treating them as part of the node is what keeps a changed fork point
    // from being missed.
    //
    // The template always emits them after the child nodes, which is what
    // lets `applyOwn` below put replacements back by appending.
    function ownParts(node) {
        const parts = [];
        const card = node.querySelector(':scope > .message');
        if (card) parts.push(card);
        const kids = node.querySelector(':scope > .children');
        if (kids) {
            Array.from(kids.children).forEach(el => {
                if (!el.classList.contains('message-node')) parts.push(el);
            });
        }
        return parts;
    }

    // A node's identity: its card's id, or — for a fork-only slot, which has
    // no card — the fork-point box's.
    function nodeKey(node) {
        const card = node.querySelector(':scope > .message');
        if (card && card.id) return card.id;
        const fork = node.querySelector(':scope > .children > .fork-point[id]');
        return fork ? fork.id : null;
    }

    // Node keys in document order, plus a hash of each node's own markup.
    // A node with no key at all makes the whole update unpatchable, because
    // the extension test below is only meaningful over a complete sequence.
    // `withHashes` is off for the live tree: only its key sequence is
    // wanted there, and hashing it would serialise the whole page — the
    // very cost this is here to avoid. Its hashes would be meaningless
    // anyway, having been taken after decoration rewrote the markup.
    function scanTree(root, withHashes) {
        const ids = [];
        const hashes = new Map();
        let ok = true;
        root.querySelectorAll('.message-node').forEach(node => {
            const key = nodeKey(node);
            if (!key) { ok = false; return; }
            ids.push(key);
            if (withHashes) {
                hashes.set(key, hashOf(ownParts(node).map(el => el.outerHTML).join('')));
            }
        });
        return { ids, hashes, ok };
    }

    // Swap a node's own markup for the new render's, leaving its children
    // alone. The card is replaced in place; the trailing parts are dropped
    // and re-appended, which is correct because the template emits them
    // after the child nodes.
    //
    // Returns the elements it actually put on the page, or null if the node
    // is not a shape it can handle. Returning *those* rather than the node
    // matters: the caller rehydrates what comes back, and a node's subtree
    // is not what changed. The session header is the case that makes this
    // sharp — its fold bar counts descendants, so it is replaced on every
    // single append, and its node is the whole page.
    function applyOwn(liveNode, newNode) {
        const liveCard = liveNode.querySelector(':scope > .message');
        const newCard = newNode.querySelector(':scope > .message');
        if (!!liveCard !== !!newCard) return null;

        const placed = [];
        if (liveCard && newCard) {
            const imported = document.importNode(newCard, true);
            liveCard.replaceWith(imported);
            placed.push(imported);
        }

        const newTrailing = ownParts(newNode).filter(el => !el.classList.contains('message'));
        const liveKids = liveNode.querySelector(':scope > .children');
        if (!liveKids) return newTrailing.length === 0 ? placed : null;
        Array.from(liveKids.children).forEach(el => {
            if (!el.classList.contains('message-node')) el.remove();
        });
        newTrailing.forEach(el => {
            const imported = document.importNode(el, true);
            liveKids.appendChild(imported);
            placed.push(imported);
        });
        return placed;
    }

    // Where a new node belongs in the live tree: inside its parent's
    // `.children`, after the last card already there. Because the id
    // sequence is an extension, every new card follows every existing one in
    // document order, so appending after the last `.message-node` is the
    // right place — and going through `.message-node` rather than the
    // container's last child keeps any trailing junction-link markup last.
    function liveNodeFor(key) {
        const el = document.getElementById(key);
        return el ? el.closest('.message-node') : null;
    }

    function insertNode(newNode, imported) {
        const parentNode = newNode.parentElement
            && newNode.parentElement.closest('.message-node');
        let liveKids;
        if (!parentNode) {
            liveKids = container();
        } else {
            const key = nodeKey(parentNode);
            const holder = key && liveNodeFor(key);
            if (!holder) return false;
            liveKids = holder.querySelector(':scope > .children');
            if (!liveKids) {
                // The parent had no children until now, so it has no
                // container to put them in; take the new one wholesale.
                const newKids = parentNode.querySelector(':scope > .children');
                if (!newKids) return false;
                holder.appendChild(document.importNode(newKids, true));
                return true;
            }
        }
        if (!liveKids) return false;
        const existing = liveKids.querySelectorAll(':scope > .message-node');
        if (existing.length) existing[existing.length - 1].after(imported);
        else liveKids.prepend(imported);
        return true;
    }

    // Returns the number of cards added, or null if this update is not a
    // shape we patch — in which case the caller swaps.
    function tryPatch(nextRoot, next) {
        if (!cardHashes || !next.ok) return null;
        const live = scanTree(container(), false);
        if (!live.ok) return null;

        // A pure extension: every node on screen is still there, with the
        // same key, in the same order. This is what fails when an
        // out-of-order arrival renumbers the positional ids, and it is
        // deliberately an all-or-nothing test — a single mismatch means the
        // ids no longer mean what they meant, so nothing keyed on them is
        // trustworthy.
        if (next.ids.length < live.ids.length) return null;
        for (let i = 0; i < live.ids.length; i++) {
            if (next.ids[i] !== live.ids[i]) return null;
        }

        const changed = [];
        for (const key of live.ids) {
            if (cardHashes.get(key) !== next.hashes.get(key)) changed.push(key);
        }
        // A broad edit is cheaper to apply wholesale than node by node. An
        // append moves only the ancestors' descendant counts, so this stays
        // in single digits in practice.
        if (changed.length > 40) return null;

        // Resolve everything before touching the live DOM, so a shape we
        // cannot handle leaves the page untouched for the swap to redo.
        const edits = [];
        for (const key of changed) {
            const liveNode = liveNodeFor(key);
            const newAnchor = nextRoot.querySelector('[id="' + CSS.escape(key) + '"]');
            const newNode = newAnchor && newAnchor.closest('.message-node');
            if (!liveNode || !newNode) return null;
            edits.push([liveNode, newNode]);
        }

        const known = new Set(live.ids);
        const additions = [];
        for (const key of next.ids) {
            if (known.has(key)) continue;
            const newAnchor = nextRoot.querySelector('[id="' + CSS.escape(key) + '"]');
            const newNode = newAnchor && newAnchor.closest('.message-node');
            if (!newNode) return null;
            // A new node nested inside another new node arrives with it;
            // `next.ids` is in document order, so the outer one comes first.
            if (additions.some(([, outer]) => outer.contains(newNode))) continue;
            additions.push([key, newNode]);
        }

        const fresh = [];
        // Nodes already on screen whose own markup legitimately changed: an
        // ancestor's descendant count, or a `pair_first` class arriving with
        // the other half of a pair. The subtree underneath is kept, and with
        // it every bit of state the card holds.
        for (const [liveNode, newNode] of edits) {
            const placed = applyOwn(liveNode, newNode);
            if (!placed) return null;
            placed.forEach(el => fresh.push(el));
        }

        // New nodes. These carry the fade-in; the ones replaced above
        // deliberately do not, since they were already on screen.
        let added = 0;
        for (const [key, newNode] of additions) {
            const imported = document.importNode(newNode, true);
            if (!insertNode(newNode, imported)) return null;
            added += imported.querySelectorAll('.message').length;
            imported.querySelectorAll('.message').forEach(el => el.classList.add('live-new'));
            fresh.push(imported);
        }

        // Rehydrate over what actually changed, not over the whole tree.
        if (window.claudeLogRehydrate) {
            fresh.forEach(el => window.claudeLogRehydrate(el));
        }
        return added;
    }

    // ---- the update ------------------------------------------------------

    // The toggle is part of the page's floating-button stack rather than
    // something this script builds, so it is styled with the rest of the
    // toolbar and cannot drift from it. It is revealed only here, because
    // reaching this point is the proof that polling is possible at all.
    const followBtn = document.getElementById('followUpdates');
    let unseen = 0;

    function renderFollowBtn() {
        if (!followBtn) return;
        if (following) unseen = 0;
        followBtn.classList.toggle('following', following);
        followBtn.setAttribute('aria-pressed', following ? 'true' : 'false');
        followBtn.dataset.unseen = String(unseen);
        followBtn.title = following
            ? 'Following new messages — click to stop'
            : (unseen
                ? `${unseen} new message${unseen === 1 ? '' : 's'} — click to follow`
                : 'Follow new messages as they arrive');
        document.body.classList.toggle('live-following', following);
    }

    function setFollowing(next) {
        following = !!next;
        renderFollowBtn();
        if (following) scrollToEnd();
    }

    if (followBtn) {
        followBtn.classList.add('live-active');
        followBtn.addEventListener('click', () => setFollowing(!following));
        renderFollowBtn();
    }

    function announce(added) {
        unseen += added;
        renderFollowBtn();
    }

    // Scroll the document to its end rather than aligning the last card,
    // which is what `scrollIntoView({block: 'end'})` did: that puts the
    // card's bottom edge *exactly* on the viewport's, measured at a 0px
    // gap. `body.live-following`'s padding supplies the space this then
    // scrolls into. Both halves are needed — measured on a real page, the
    // padding alone still gives 0px (the alignment ignores it) and a
    // scroll-margin alone gives 25px (there is no room left to give).
    function scrollToEnd() {
        window.scrollTo({
            top: document.documentElement.scrollHeight,
            behavior: 'smooth',
        });
    }

    function swapIn(next, current) {
        const before = captureState(current);
        current.replaceWith(next);
        restoreState(next, before);
        const added = markNew(next, before.keys);
        // Everything that decorated the old markup after load.
        if (window.claudeLogRehydrate) window.claudeLogRehydrate(next);
        return added;
    }

    async function applyUpdate(html) {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const next = doc.getElementById('transcript');
        const current = container();
        if (!next || !current) return;

        // Hashes come from the parsed bytes, before anything is put on the
        // page, and are kept whichever route the update took — the swap is a
        // valid starting point for the next patch.
        const scan = scanTree(next, true);
        let added = tryPatch(next, scan);
        if (added === null) added = swapIn(next, current);
        cardHashes = scan.hashes;

        // The title carries the message/token counts, and the session nav
        // its summaries; both go stale otherwise.
        const nextTitle = doc.getElementById('title');
        const title = document.getElementById('title');
        if (nextTitle && title) title.innerHTML = nextTitle.innerHTML;

        if (added) announce(added);
        if (following) scrollToEnd();
    }

    // One poll at a time. The interval keeps firing while a full GET is in
    // flight, and a page slow enough to fetch — which is exactly the large
    // page all of this is for — would then have two updates racing:
    // whichever *response* lands last wins, so an older render overwrites a
    // newer one and the page loses messages it had already shown. Measured
    // by holding one response for 3s: the newest message appeared at 2.0s,
    // vanished at 4.0s when the stale body landed, and came back at 5.0s.
    //
    // Serialising is what stops it, and skipping a tick costs nothing:
    // `lastStamp` only advances once an update has actually been applied,
    // so the next tick still sees the change. (That ordering is also what
    // bounds the damage above to one second rather than forever — the
    // stale apply rewinds `lastStamp` to its own older value, so the next
    // HEAD finds a difference again. Recording the stamp before the GET
    // instead leaves the page wrong until something else changes.)
    let polling = false;

    async function poll() {
        if (stopped || polling) return;
        polling = true;
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
                const res = await fetch(location.href, { cache: 'no-store' });
                if (res.ok) {
                    await applyUpdate(await res.text());
                    lastStamp = stamp;
                }
            }
        } catch (err) {
            // A dropped server is the normal end of a watch session, not an
            // error worth shouting about. Keep polling: `serve` may come back.
            console.debug('live update poll failed', err);
        } finally {
            polling = false;
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
        setFollowing,
    };
})();
