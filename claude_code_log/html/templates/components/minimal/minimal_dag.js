        // Minimal theme (--theme minimal): the DAG engine — branch lanes
        // (sub-agents at any depth, rewind forks) drawn as a git-graph rail
        // beside the main line, each either folded ("Main only", the default)
        // or interleaved with the main line at its real arrival times.
        // work/minimal-theme-dag.md § 1.6 and § 3.5; as built in
        // dev-docs/minimal-theme.md.
        //
        // The server renders every card where it always has (nested
        // `.message-node > .children`, P5's `data-lane*` attributes on the
        // cards). Nothing here moves a DOM node: `dag-on` on the stage turns
        // the wrappers into `display: contents` and #transcript into a
        // one-column grid in which each card gets an inline `grid-row`. Rows
        // are packed as if every lane were interleaved; a folded lane is
        // hidden and its rows are empty, so opening or folding one lane
        // rewrites only that lane's cards. The fold state machine, the
        // filter, search and live updates all keep working on the nested DOM.
        //
        // Ownership (nothing else writes these, this writes nothing else):
        // the `dag-*` classes, inline `grid-row` and `--dag-tag` on cards,
        // the `.mn-bctls` branch controls, `--dag-slots` and the `dag-on`
        // class on the stage, and everything in #dag-rail.
        //
        // A relayout is a pure function of (DOM, lane modes): it walks the
        // tree once, orders the visible lanes' cards by time, packs rows,
        // assigns rail slots, writes what changed, then measures the few
        // rows the rail needs and redraws the SVG. Triggers: one
        // MutationObserver on #transcript (folds, filter, search, live
        // patches), the rehydrate hook (live swaps), a ResizeObserver
        // (redraw only), the toolbar and branch controls.
        (function () {
            'use strict';
            const stage = document.querySelector('.mn-stage');
            const railHost = document.getElementById('dag-rail');
            if (!stage || !railHost) return;

            const BRANCHES_KEY = 'claude-code-log:branches';
            // Global modes. P7 adds 'columns' (and the per-lane 'column' /
            // 'strip' modes): see the marked hooks below and in header.html.
            const GLOBAL_MODES = ['main', 'interleaved'];
            const CAP = 3;              // interleaved lanes per user turn
            const MAX_SLOTS = 6;        // rail slots beside the main line
            const RADIUS = 8;           // fork/merge connector corner
            const STUB = 12;            // a folded fork's stub below its row
            // Forks are always --lF; agents cycle through the other lane colours.
            const AGENT_COLOURS = ['a', 'b', 's', 'p'];
            const DEBUG = /[?&]debug-dag(?:[=&]|$)/.test(window.location.search);
            const phone = window.matchMedia ? window.matchMedia('(max-width: 640px)') : null;

            // ---- state ---------------------------------------------------
            let globalMode = readGlobal();
            const modes = new Map();      // lane id -> 'folded' | 'interleaved'
            const recency = new Map();    // turn d-N -> interleaved lane ids, least recent first
            let lanes = new Map();        // lane id -> record (rebuilt each relayout)
            let started = false;
            let layout = null;            // last relayout's result, for redraws
            let lastTiming = null;
            const pendingReveals = [];

            function readGlobal() {
                try {
                    const stored = window.localStorage.getItem(BRANCHES_KEY);
                    return GLOBAL_MODES.indexOf(stored) >= 0 ? stored : 'main';
                } catch (err) {
                    return 'main';
                }
            }
            function writeGlobal(value) {
                try {
                    window.localStorage.setItem(BRANCHES_KEY, value);
                } catch (err) { /* storage unavailable: the choice lasts this page */ }
            }

            // ---- lanes ---------------------------------------------------
            function readLanes() {
                const transcript = document.getElementById('transcript');
                const found = new Map();
                if (!transcript) return found;
                transcript.querySelectorAll('[data-lane-id]').forEach(function (head) {
                    const id = head.getAttribute('data-lane-id');
                    if (found.has(id)) return;
                    const ts = (head.getAttribute('data-lane-ts') || '').split(' ');
                    found.set(id, {
                        id: id,
                        head: head,
                        kind: head.getAttribute('data-lane-kind') || 'agent',
                        name: head.getAttribute('data-lane-name') || id,
                        tag: head.getAttribute('data-lane-tag') || '',
                        meta: head.getAttribute('data-lane-meta') || '',
                        parent: head.getAttribute('data-lane-parent') || 'main',
                        from: head.getAttribute('data-lane-from') || '',
                        turn: head.getAttribute('data-lane-turn') || '',
                        rank: parseInt(head.getAttribute('data-lane-rank') || '1', 10) || 1,
                        stats: head.getAttribute('data-lane-stats') || '',
                        firstTs: ts[0] ? Date.parse(ts[0]) : NaN,
                    });
                });
                return found;
            }
            function parentOf(id) {
                const lane = lanes.get(id);
                return lane && lane.parent !== 'main' && lanes.has(lane.parent) ? lane.parent : null;
            }
            function ancestorsOf(id) {
                const chain = [];
                let up = parentOf(id);
                while (up && chain.indexOf(up) < 0) {
                    chain.unshift(up);
                    up = parentOf(up);
                }
                return chain;
            }
            function turnOf(id) {
                const lane = lanes.get(id);
                return lane ? lane.turn || ('lane:' + id) : 'lane:' + id;
            }
            function defaultMode(lane, value) {
                return value === 'interleaved' && lane.rank <= CAP ? 'interleaved' : 'folded';
            }

            // Interleave a lane (and, first, the lanes it is nested in), most
            // recent last; past the cap the least recently selected lane of the
            // same turn folds — never one the new lane needs to be visible.
            function select(id) {
                if (!lanes.has(id)) return;
                const chain = ancestorsOf(id).concat([id]);
                const turn = turnOf(id);
                let list = recency.get(turn) || [];
                chain.forEach(function (lane) {
                    modes.set(lane, 'interleaved');
                    list = list.filter(function (x) { return x !== lane; });
                    list.push(lane);
                });
                recency.set(turn, list);
                while (list.length > CAP) {
                    const victim = list.find(function (x) { return chain.indexOf(x) < 0; });
                    if (!victim) break;
                    fold(victim);
                    list = recency.get(turn) || [];
                }
            }
            // Fold a lane and every lane nested in it.
            function fold(id) {
                modes.set(id, 'folded');
                const turn = turnOf(id);
                const list = (recency.get(turn) || []).filter(function (x) { return x !== id; });
                recency.set(turn, list);
                lanes.forEach(function (lane, other) {
                    if (other !== id && ancestorsOf(other).indexOf(id) >= 0 && modes.get(other) === 'interleaved') {
                        fold(other);
                    }
                });
            }
            function applyGlobal(value) {
                recency.clear();
                const byTurn = new Map();
                lanes.forEach(function (lane) {
                    modes.set(lane.id, 'folded');
                    const list = byTurn.get(turnOf(lane.id)) || [];
                    list.push(lane);
                    byTurn.set(turnOf(lane.id), list);
                });
                if (value !== 'interleaved') return;
                byTurn.forEach(function (list) {
                    list.sort(function (a, b) { return a.rank - b.rank; });
                    list.forEach(function (lane) {
                        if (defaultMode(lane, value) === 'interleaved') select(lane.id);
                    });
                });
            }
            // Lanes seen for the first time (page load, a live update) take the
            // global choice — but never push out a lane the reader picked.
            function adoptNewLanes() {
                lanes.forEach(function (lane, id) {
                    if (modes.has(id)) return;
                    modes.set(id, 'folded');
                    if (defaultMode(lane, globalMode) !== 'interleaved') return;
                    const list = recency.get(turnOf(id)) || [];
                    if (list.length < CAP) select(id);
                });
            }
            function isOpen(id, memo) {
                if (id === 'main') return true;
                if (memo.has(id)) return memo.get(id);
                memo.set(id, false); // cycle guard
                const parent = lanes.has(id) ? lanes.get(id).parent : 'main';
                const open = modes.get(id) === 'interleaved' && (parent === 'main' || !lanes.has(parent) || isOpen(parent, memo));
                memo.set(id, open);
                return open;
            }
            // Which segment of the global control describes the current modes?
            function matchingGlobal() {
                let match = null;
                GLOBAL_MODES.forEach(function (value) {
                    let all = true;
                    lanes.forEach(function (lane, id) {
                        if ((modes.get(id) || 'folded') !== defaultMode(lane, value)) all = false;
                    });
                    if (all && match === null) match = value;
                });
                return match;
            }

            // ---- reveal: links, deep links and search open folded lanes ---
            function laneOfElement(el) {
                if (!el || !el.closest) return null;
                const card = el.closest('[data-lane]');
                const lane = card ? card.getAttribute('data-lane') : null;
                return lane && lane !== 'main' ? lane : null;
            }
            // Returns true when a lane had to be opened.
            function revealLanes(el) {
                const lane = laneOfElement(el);
                if (!lane) return false;
                if (!started) {
                    pendingReveals.push(lane);
                    return false;
                }
                if (!lanes.has(lane)) lanes = readLanes();
                if (!lanes.has(lane) || isOpen(lane, new Map())) return false;
                select(lane);
                return true;
            }
            // The classic page assigns these hooks inside its DOMContentLoaded
            // handler; intercepting the assignment wraps them whenever that
            // happens, so search (`?q=`) and archive deep links (`?uuid=`) open
            // the lane holding their target before revealing it.
            function wrapHook(name, wrap) {
                let inner = window[name];
                let wrapped = inner ? wrap(inner) : undefined;
                try {
                    Object.defineProperty(window, name, {
                        configurable: true,
                        get: function () { return wrapped; },
                        set: function (fn) { inner = fn; wrapped = fn ? wrap(fn) : fn; },
                    });
                } catch (err) { /* leave the hook unwrapped */ }
            }
            wrapHook('claudeLogRevealMessage', function (inner) {
                return function (target) {
                    if (revealLanes(target)) relayout();
                    return inner.apply(this, arguments);
                };
            });
            wrapHook('claudeLogRevealMessageByUuid', function (inner) {
                return function (uuid) {
                    if (uuid) {
                        const target = document.querySelector('[data-uuid="' + CSS.escape(uuid) + '"]');
                        if (target && revealLanes(target)) relayout();
                    }
                    return inner.apply(this, arguments);
                };
            });
            function revealHash(scroll) {
                const hash = window.location.hash;
                if (!hash || hash.indexOf('#msg-') !== 0) return;
                const target = document.getElementById(hash.slice(1));
                if (!target) return;
                if (revealLanes(target)) relayout();
                if (scroll && target.scrollIntoView) target.scrollIntoView({ block: 'start' });
            }

            // ---- model: one walk over the nested DOM ----------------------
            function cardTs(el, cache) {
                const hit = cache.get(el);
                if (hit !== undefined) return hit;
                const stamp = el.querySelector('.timestamp[data-timestamp]');
                const value = stamp ? Date.parse(stamp.getAttribute('data-timestamp')) : NaN;
                cache.set(el, value);
                return value;
            }
            const tsCache = new WeakMap();

            function agentDepth(card) {
                const match = /(?:^|\s)agent-depth-(\d+)(?:\s|$)/.exec(card.className);
                if (match) return parseInt(match[1], 10);
                return card.classList.contains('sidechain') ? 1 : 0;
            }
            // A nested group that is not a lane keeps its nested look as one
            // block: teammate threads (spec § 1.6.5), workflow phases (§ 7),
            // old-style sidechains without an agent transcript.
            function isBlockGroup(owner, kid) {
                if (kid.classList.contains('workflow_phase') || kid.classList.contains('workflow_agent')) return true;
                return !!owner && kid.classList.contains('sidechain') && agentDepth(kid) > agentDepth(owner);
            }

            function buildModel() {
                const transcript = document.getElementById('transcript');
                const memo = new Map();
                const model = {
                    items: [],
                    seqs: new Map(),
                    spawns: new Map(),   // lane -> spawn item
                    merges: new Map(),   // lane -> merge item
                    cls: new Map(),      // element -> engine classes wanted
                    spawnCards: [],
                };
                if (!transcript) return model;
                function want(el, cls) {
                    const prev = model.cls.get(el);
                    model.cls.set(el, prev ? prev + ' ' + cls : cls);
                }
                function addItem(el, lane, kind, hidden) {
                    const item = {
                        el: el, lane: lane, kind: kind,
                        hidden: hidden || el.classList.contains('filtered-hidden') || el.classList.contains('search-hidden'),
                        row: -1, ts: NaN,
                    };
                    model.items.push(item);
                    let seq = model.seqs.get(lane);
                    if (!seq) model.seqs.set(lane, seq = []);
                    seq.push(item);
                    if (kind === 'block') return item;
                    const spawns = el.getAttribute('data-spawns');
                    if (spawns) {
                        model.spawnCards.push(item);
                        spawns.split(' ').forEach(function (id) { if (id) model.spawns.set(id, item); });
                    }
                    const merges = el.getAttribute('data-merges');
                    if (merges) merges.split(' ').forEach(function (id) { if (id) model.merges.set(id, item); });
                    return item;
                }
                function walkContainer(container, lane, hidden) {
                    const kids = container.children;
                    for (let i = 0; i < kids.length; i++) {
                        const child = kids[i];
                        const list = child.classList;
                        if (list.contains('message-node')) walkNode(child, lane, hidden);
                        else if (list.contains('fork-point')) addItem(child, child.getAttribute('data-lane') || lane, 'box', hidden);
                    }
                }
                function walkNode(node, lane, hidden) {
                    let card = null;
                    let cardLane = lane;
                    const kids = node.children;
                    for (let i = 0; i < kids.length; i++) {
                        const el = kids[i];
                        const list = el.classList;
                        if (list.contains('message')) {
                            card = el;
                            cardLane = el.getAttribute('data-lane') || lane;
                            // A fork lane's head is its branch header: the
                            // whole node is the lane.
                            const head = el.getAttribute('data-lane-id');
                            if (head && list.contains('session-header') && lanes.has(head) && !isOpen(head, memo)) {
                                want(node, 'dag-hidden');
                                hidden = true;
                            }
                            addItem(el, cardLane, 'card', hidden);
                        } else if (list.contains('children')) {
                            const folded = el.style.display === 'none';
                            const first = el.querySelector(':scope > .message-node > .message');
                            const kidLane = first ? (first.getAttribute('data-lane') || cardLane) : cardLane;
                            if (first && kidLane !== cardLane && kidLane.indexOf('agent-') === 0) {
                                // A sub-agent's transcript: the lane decides.
                                if (card) want(card, 'dag-owner');
                                // Folded, it is still walked (hidden): rows are
                                // packed as if every lane were open, so opening
                                // or folding one never renumbers the others.
                                const shut = lanes.has(kidLane) && !isOpen(kidLane, memo);
                                want(el, shut ? 'dag-hidden' : 'dag-entry');
                                walkContainer(el, kidLane, hidden || shut);
                            } else if (first && isBlockGroup(card, first)) {
                                want(el, 'dag-block');
                                addItem(el, cardLane, 'block', hidden || folded);
                            } else {
                                walkContainer(el, cardLane, hidden || folded);
                            }
                        }
                    }
                }
                walkContainer(transcript, 'main', false);
                model.memo = memo;
                return model;
            }

            // ---- order: each lane keeps its DOM order; lanes merge by time --
            // Every lane takes part, folded or not (a folded lane's rows are
            // empty and take no height), so the order — and every card's row —
            // depends only on the DOM: toggling a lane rewrites nothing else.
            function order(model) {
                const main = model.seqs.get('main') || [];
                const others = [];
                model.seqs.forEach(function (seq, id) { if (id !== 'main') others.push(id); });
                if (!others.length) return main.slice();
                const seqs = [main].concat(others.map(function (id) { return model.seqs.get(id); }));
                const ids = ['main'].concat(others);
                // Timestamps: a card's own, else the previous one in its lane
                // (fork heads: the lane's first time; else its spawn's).
                seqs.forEach(function (seq, k) {
                    let prev = NaN;
                    if (k > 0) {
                        const lane = lanes.get(ids[k]);
                        const spawn = model.spawns.get(ids[k]);
                        if (lane && !isNaN(lane.firstTs)) prev = lane.firstTs;
                        else if (spawn) prev = cardTs(spawn.el, tsCache);
                    }
                    seq.forEach(function (item) {
                        const own = item.kind === 'card' ? cardTs(item.el, tsCache) : NaN;
                        item.ts = isNaN(own) ? prev : own;
                        if (!isNaN(item.ts)) prev = item.ts;
                    });
                });
                const pos = seqs.map(function () { return 0; });
                const index = new Map();
                ids.forEach(function (id, k) { index.set(id, k); });
                const emitted = new Set();
                const active = [0];
                const waiting = new Map(); // spawn item -> [lane slots]
                for (let k = 1; k < ids.length; k++) {
                    const spawn = model.spawns.get(ids[k]);
                    if (spawn && spawn.lane !== ids[k]) {
                        const list = waiting.get(spawn) || [];
                        list.push(k);
                        waiting.set(spawn, list);
                    } else {
                        active.push(k);
                    }
                }
                const out = [];
                function blocked(item) {
                    const merges = item.kind === 'card' ? item.el.getAttribute('data-merges') : null;
                    if (!merges) return false;
                    return merges.split(' ').some(function (id) {
                        const k = index.get(id);
                        return k !== undefined && pos[k] < seqs[k].length;
                    });
                }
                while (true) {
                    let best = -1;
                    let bestTs = Infinity;
                    for (let a = 0; a < active.length; a++) {
                        const k = active[a];
                        if (pos[k] >= seqs[k].length) continue;
                        const item = seqs[k][pos[k]];
                        if (blocked(item)) continue;
                        const ts = isNaN(item.ts) ? -Infinity : item.ts;
                        if (best < 0 || ts < bestTs) { best = k; bestTs = ts; }
                    }
                    if (best < 0) {
                        // Everything left is waiting on something that cannot
                        // come first (clock skew, a lane without its spawn):
                        // take the earliest remaining lane in lane order.
                        for (let k = 0; k < seqs.length && best < 0; k++) {
                            if (pos[k] < seqs[k].length) {
                                best = k;
                                if (active.indexOf(k) < 0) active.push(k);
                            }
                        }
                        if (best < 0) break;
                    }
                    // A tool call and its result are one step: the result
                    // half follows its call at once, unless it closes a lane
                    // whose rows must come first (a synchronous agent).
                    let item = null;
                    do {
                        item = seqs[best][pos[best]++];
                        out.push(item);
                        emitted.add(item);
                        const opened = waiting.get(item);
                        if (opened) {
                            opened.forEach(function (k) { if (active.indexOf(k) < 0) active.push(k); });
                            waiting.delete(item);
                        }
                    } while (opensPair(item) && pos[best] < seqs[best].length
                        && closesPair(seqs[best][pos[best]]) && !blocked(seqs[best][pos[best]]));
                    if (best > 0 && pos[best] >= seqs[best].length) {
                        const at = active.indexOf(best);
                        if (at >= 0) active.splice(at, 1);
                    }
                }
                return out;
            }

            function opensPair(item) {
                const list = item.el.classList;
                return item.kind === 'card' && (list.contains('pair_first') || list.contains('pair_middle'));
            }
            function closesPair(item) {
                const list = item.el.classList;
                return item.kind === 'card' && (list.contains('pair_middle') || list.contains('pair_last'));
            }

            // Rows — ported from DagRail.dc.html renderVals() "Pack rows":
            // time order is kept top to bottom; a lane's first row is below its
            // spawn, a merge row below the merged lane's last row. Without
            // columns this is one item per row; P7 keys `nextFree` by column so
            // cards in different columns can share a row.
            function pack(sequence, model) {
                const nextFree = {};
                const lastRowOf = new Map();
                let prev = 0;
                sequence.forEach(function (item) {
                    const key = 'main'; // P7: the item's column lane when columned
                    let r = Math.max(prev, nextFree[key] || 0);
                    if (item.lane !== 'main') {
                        const spawn = model.spawns.get(item.lane);
                        if (spawn && spawn.row >= 0 && !lastRowOf.has(item.lane)) r = Math.max(r, spawn.row + 1);
                    }
                    const merges = item.kind === 'card' ? item.el.getAttribute('data-merges') : null;
                    if (merges) {
                        merges.split(' ').forEach(function (id) {
                            if (lastRowOf.has(id)) r = Math.max(r, lastRowOf.get(id) + 1);
                        });
                    }
                    item.row = r;
                    lastRowOf.set(item.lane, r);
                    nextFree[key] = r + 1;
                    prev = r;
                });
                return { rows: prev + 1, lastRowOf: lastRowOf };
            }

            // Rail — the "Rail" part of renderVals(): slot 0 is the main line;
            // every lane with something to draw gets the lowest slot free over
            // its span [spawn row, end row] (greedy interval colouring; the
            // mockup hard-coded its slots). End: the merge row; else, when
            // interleaved, the lane's last row; else its spawn row (a stub).
            // Interleaved lanes are placed first — their cards sit in their
            // slot — then folded ones while slots last.
            function railLanes(model, open, packed) {
                const memo = model.memo;
                const plans = [];
                lanes.forEach(function (lane, id) {
                    const opened = open.indexOf(id) >= 0;
                    const spawn = model.spawns.get(id);
                    const parentOpen = lane.parent === 'main' || !lanes.has(lane.parent) || isOpen(lane.parent, memo);
                    if (!parentOpen) return;
                    const seq = opened ? (model.seqs.get(id) || []) : [];
                    if (!spawn && !seq.length) return;
                    const merge = model.merges.get(id);
                    const first = seq.length ? seq[0].row : -1;
                    const last = seq.length ? packed.lastRowOf.get(id) : -1;
                    const start = spawn && spawn.row >= 0 ? spawn.row : first;
                    let end = start;
                    if (merge && merge.row >= 0) end = merge.row;
                    else if (opened && last >= 0) end = last;
                    plans.push({ id: id, lane: lane, open: opened, spawn: spawn, merge: merge, seq: seq, start: start, end: Math.max(start, end) });
                });
                plans.sort(function (a, b) {
                    if (a.open !== b.open) return a.open ? -1 : 1;
                    return a.start - b.start;
                });
                const slots = []; // slot -> [ [start, end], ... ]
                const slotOf = new Map();
                plans.forEach(function (plan) {
                    const parentSlot = slotOf.get(plan.lane.parent) || 0;
                    let s = parentSlot + 1;
                    for (; s <= MAX_SLOTS; s++) {
                        const taken = (slots[s] || []).some(function (span) {
                            return plan.start <= span[1] && span[0] <= plan.end;
                        });
                        if (!taken) break;
                    }
                    if (s > MAX_SLOTS) {
                        if (!plan.open) return; // no room: control only
                        s = MAX_SLOTS;
                    }
                    (slots[s] = slots[s] || []).push([plan.start, plan.end]);
                    slotOf.set(plan.id, s);
                    plan.slot = s;
                    plan.colour = plan.lane.kind === 'fork' ? 'f' : AGENT_COLOURS[(s - 1) % AGENT_COLOURS.length];
                });
                return { plans: plans.filter(function (p) { return p.slot; }), slotOf: slotOf, slots: slots.length ? slots.length - 1 : 0 };
            }

            // ---- writes ----------------------------------------------------
            const ENGINE_CLASSES = /(?:^|\s)(dag-[a-z0-9-]+)(?=\s|$)/g;
            let classed = new Set();
            const rowWritten = new WeakMap();
            const tagWritten = new WeakMap();

            function setEngineClasses(el, wanted) {
                const current = [];
                el.className.replace(ENGINE_CLASSES, function (m, cls) { current.push(cls); return m; });
                const next = wanted ? wanted.split(' ') : [];
                current.forEach(function (cls) { if (next.indexOf(cls) < 0) el.classList.remove(cls); });
                next.forEach(function (cls) { if (cls && current.indexOf(cls) < 0) el.classList.add(cls); });
            }

            function cssString(text) {
                return '"' + String(text).replace(/["\\\n]/g, function (c) { return c === '\n' ? ' ' : '\\' + c; }) + '"';
            }

            const CHEVRON = "<svg width='10' height='10' viewBox='0 0 12 12' aria-hidden='true'><path d='M4 2.5 7.5 6 4 9.5' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'></path></svg>";

            const forkTimes = new Map();
            function localTime(cardId) {
                const card = cardId ? document.getElementById('msg-' + cardId) : null;
                const stamp = card ? card.querySelector('.timestamp[data-timestamp]') : null;
                const date = stamp ? new Date(stamp.getAttribute('data-timestamp')) : null;
                if (!date || isNaN(date.getTime())) return '';
                return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
            }

            // One control per lane, in a container the engine owns at the end
            // of the spawn card (grid row 4 of the card). Labels are generated
            // content (`data-label`), so search and the timeline never index
            // them. A live patch replaces the card and drops the container;
            // the next relayout puts it back.
            function ensureControls(item, colours, memo) {
                const ids = (item.el.getAttribute('data-spawns') || '').split(' ').filter(function (id) { return lanes.has(id); });
                let box = item.el.querySelector(':scope > .mn-bctls');
                if (!ids.length) {
                    if (box) box.remove();
                    return;
                }
                if (!box) {
                    box = document.createElement('div');
                    box.className = 'mn-bctls';
                    item.el.appendChild(box);
                }
                ids.sort(function (a, b) { return lanes.get(a).rank - lanes.get(b).rank; });
                const existing = Array.from(box.children);
                ids.forEach(function (id, i) {
                    const lane = lanes.get(id);
                    let ctl = existing.find(function (el) { return el.getAttribute('data-lane-ref') === id; });
                    if (!ctl) {
                        ctl = document.createElement('div');
                        ctl.setAttribute('data-lane-ref', id);
                        ctl.innerHTML = "<button type='button' class='mn-bfold'>" + CHEVRON + "</button>"
                            // P7: the per-lane Column ⇥ / ⇤ Interleave toggle.
                            + "<button type='button' class='mn-bmini mn-bcol' disabled title='Show this branch in its own column (not available yet)'>Column ⇥</button>";
                    }
                    if (box.children[i] !== ctl) box.insertBefore(ctl, box.children[i] || null);
                    const opened = isOpen(id, memo);
                    const colour = colours.get(id) || (lane.kind === 'fork' ? 'f' : 'a');
                    const cls = 'mn-bctl dag-lc-' + colour + (opened ? ' is-open' : '');
                    if (ctl.className !== cls) ctl.className = cls;
                    const fork = lane.kind === 'fork';
                    const label = (fork ? '⑂ ' + lane.name + ' · ' : '') + (lane.stats || lane.name) + (opened ? ' · interleaved' : '');
                    const btn = ctl.querySelector('.mn-bfold');
                    if (btn.getAttribute('data-label') !== label) btn.setAttribute('data-label', label);
                    const expanded = opened ? 'true' : 'false';
                    if (btn.getAttribute('aria-expanded') !== expanded) btn.setAttribute('aria-expanded', expanded);
                    const aria = (opened ? 'Fold branch: ' : 'Interleave branch: ') + lane.name + (lane.stats ? ' (' + lane.stats + ')' : '');
                    if (btn.getAttribute('aria-label') !== aria) btn.setAttribute('aria-label', aria);
                    let title = lane.name + (lane.meta ? ' · ' + lane.meta : '');
                    if (fork) {
                        if (!forkTimes.has(lane.from)) forkTimes.set(lane.from, localTime(lane.from));
                        const at = forkTimes.get(lane.from);
                        title = lane.name + ' · rewound' + (at ? ' to ' + at : '');
                    }
                    if (btn.title !== title) btn.title = title;
                });
                existing.forEach(function (el) {
                    if (ids.indexOf(el.getAttribute('data-lane-ref')) < 0) el.remove();
                });
            }

            function markToolbar() {
                const seg = document.querySelector('.mn-branches');
                if (!seg) return;
                const has = lanes.size > 0;
                if (seg.hidden === has) seg.hidden = !has;
                const match = matchingGlobal();
                seg.querySelectorAll('[data-mn-branches]').forEach(function (btn) {
                    const on = btn.getAttribute('data-mn-branches') === match;
                    btn.classList.toggle('on', on);
                    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
                });
            }

            // ---- relayout --------------------------------------------------
            let observer = null;
            let observed = null;
            let frame = 0;

            function relayout() {
                if (!started) return;
                const t0 = performance.now();
                lanes = readLanes();
                adoptNewLanes();
                const model = buildModel();
                const memo = model.memo;
                const open = [];
                lanes.forEach(function (lane, id) {
                    if (isOpen(id, memo) && (model.seqs.get(id) || []).length) open.push(id);
                });
                const t1 = performance.now();
                const sequence = order(model);
                const packed = pack(sequence, model);
                const openSet = new Set(open);
                const rail = railLanes(model, open, packed);
                const colours = new Map();
                rail.plans.forEach(function (plan) { colours.set(plan.id, plan.colour); });
                const t2 = performance.now();

                // Per-card engine state: interleaved rows carry their lane's
                // slot, colour and gutter tag; a pair split by interleaved rows
                // shows both halves in full.
                const tags = new Map();
                open.forEach(function (id) {
                    const slot = rail.slotOf.get(id) || 1;
                    const lane = lanes.get(id);
                    const cls = 'dag-in dag-s' + Math.min(slot, MAX_SLOTS) + ' dag-lc-' + (colours.get(id) || 'a');
                    (model.seqs.get(id) || []).forEach(function (item) {
                        const prev = model.cls.get(item.el);
                        model.cls.set(item.el, prev ? prev + ' ' + cls : cls);
                        if (item.kind === 'card') tags.set(item.el, lane.tag || lane.kind);
                    });
                });
                {
                    // Only rows that are shown count: a folded lane between
                    // two halves leaves empty rows, not a split.
                    const lastOf = new Map();
                    const isHalf = function (el, a, b) {
                        return el.classList.contains(a) || el.classList.contains(b);
                    };
                    let shown = 0;
                    sequence.forEach(function (item) {
                        if (item.lane !== 'main' && !openSet.has(item.lane)) return;
                        item.shown = shown++;
                        const previous = lastOf.get(item.lane);
                        lastOf.set(item.lane, item);
                        if (!previous || item.shown === previous.shown + 1) return;
                        if (!isHalf(item.el, 'pair_middle', 'pair_last')) return;
                        if (!isHalf(previous.el, 'pair_first', 'pair_middle')) return;
                        [previous.el, item.el].forEach(function (half) {
                            const prev = model.cls.get(half);
                            model.cls.set(half, prev ? prev + ' dag-split' : 'dag-split');
                        });
                    });
                }

                if (observer) observer.disconnect();
                if (!stage.classList.contains('dag-on')) stage.classList.add('dag-on');
                const slotsValue = String(rail.slots);
                if (stage.style.getPropertyValue('--dag-slots') !== slotsValue) stage.style.setProperty('--dag-slots', slotsValue);
                const nextClassed = new Set();
                model.cls.forEach(function (wanted, el) {
                    setEngineClasses(el, wanted);
                    nextClassed.add(el);
                });
                classed.forEach(function (el) {
                    if (!nextClassed.has(el)) setEngineClasses(el, '');
                });
                classed = nextClassed;
                sequence.forEach(function (item) {
                    const value = String(item.row + 1);
                    if (rowWritten.get(item.el) !== value) {
                        item.el.style.gridRow = value;
                        rowWritten.set(item.el, value);
                    }
                });
                model.items.forEach(function (item) {
                    if (item.kind !== 'card') return;
                    const tag = tags.get(item.el);
                    const value = tag ? cssString(tag) : '';
                    if ((tagWritten.get(item.el) || '') === value) return;
                    if (value) item.el.style.setProperty('--dag-tag', value);
                    else item.el.style.removeProperty('--dag-tag');
                    tagWritten.set(item.el, value);
                });
                model.spawnCards.forEach(function (item) { ensureControls(item, colours, memo); });
                markToolbar();
                if (observer && observed) observe(observed);
                const t3 = performance.now();

                layout = { model: model, rail: rail, packed: packed, sequence: sequence };
                draw();
                const t4 = performance.now();
                lastTiming = {
                    total: t4 - t0, model: t1 - t0, order: t2 - t1, write: t3 - t2, draw: t4 - t3,
                    items: model.items.length, rows: packed.rows, lanes: lanes.size, open: open.length,
                };
                if (DEBUG) console.info('[dag] relayout ' + lastTiming.total.toFixed(1) + 'ms', lastTiming);
            }

            function schedule() {
                if (frame) return;
                frame = window.requestAnimationFrame(function () {
                    frame = 0;
                    if (document.hidden) {
                        hiddenDirty = true;
                        return;
                    }
                    relayout();
                });
            }
            let hiddenDirty = false;
            document.addEventListener('visibilitychange', function () {
                if (!document.hidden && hiddenDirty) {
                    hiddenDirty = false;
                    relayout();
                }
            });

            // ---- rail: SVG from measured rows ------------------------------
            let drawFrame = 0;
            function scheduleDraw() {
                if (drawFrame) return;
                drawFrame = window.requestAnimationFrame(function () {
                    drawFrame = 0;
                    if (!document.hidden) draw();
                });
            }

            function visible(item) {
                if (!item || item.hidden) return false;
                return item.el.getClientRects().length > 0;
            }

            function draw() {
                if (!layout) return;
                const transcript = document.getElementById('transcript');
                if (!transcript) return;
                const stageBox = stage.getBoundingClientRect();
                const transcriptBox = transcript.getBoundingClientRect();
                const style = getComputedStyle(stage);
                const step = parseFloat(style.getPropertyValue('--dag-step')) || 16;
                const x0 = parseFloat(style.getPropertyValue('--dag-x0')) || 9;
                const gut = phone && phone.matches ? 0 : (parseFloat(style.getPropertyValue('--gut')) || 58);
                const base = transcriptBox.left - stageBox.left + gut + x0;
                const slotOf = layout.rail.slotOf;
                function x(slot) { return base + step * slot; }
                const dyCache = new Map();
                function rowY(item) {
                    const el = item.el;
                    const box = el.getBoundingClientRect();
                    // The dot's centre (layout.css): padding-top + .7em.
                    let dy = dyCache.get(el.className);
                    if (dy === undefined) {
                        const cs = getComputedStyle(el);
                        dy = parseFloat(cs.paddingTop) + 0.7 * parseFloat(cs.fontSize);
                        dyCache.set(el.className, dy);
                    }
                    return box.top - stageBox.top + dy;
                }
                function round(v) { return Math.round(v * 10) / 10; }
                const paths = [];
                function path(lane, part, colour, d, dashed) {
                    paths.push("<path class='dag-lc-" + colour + (dashed ? ' dag-dash' : '') + "' data-lane='" + lane.replace(/'/g, '') + "' data-part='" + part + "' d='" + d + "'></path>");
                }
                layout.rail.plans.forEach(function (plan) {
                    const xs = x(slotOf.get(plan.lane.parent) || 0);
                    const xk = x(plan.slot);
                    // First and last shown rows of the lane only.
                    const seen = [];
                    for (let i = 0; i < plan.seq.length; i++) {
                        if (visible(plan.seq[i])) { seen.push(plan.seq[i]); break; }
                    }
                    for (let i = plan.seq.length - 1; i >= 0 && seen.length; i--) {
                        if (visible(plan.seq[i])) { seen.push(plan.seq[i]); break; }
                    }
                    const spawnShown = visible(plan.spawn);
                    if (!spawnShown && !(plan.open && seen.length)) return;
                    const ys = spawnShown ? rowY(plan.spawn) : rowY(seen[0]);
                    const mergeShown = plan.merge && visible(plan.merge) && plan.merge.row > (plan.spawn ? plan.spawn.row : -1);
                    let ye;
                    if (mergeShown) ye = rowY(plan.merge);
                    else if (plan.open && seen.length) ye = rowY(seen[seen.length - 1]);
                    else ye = ys;
                    const dir = xk >= xs ? 1 : -1;
                    if (ye - ys < 2 * RADIUS + 1) {
                        // Nothing to span: a stub hanging off the spawn row.
                        if (!spawnShown) return;
                        const r = RADIUS;
                        path(plan.id, 'stub', plan.colour,
                            'M' + round(xs) + ' ' + round(ys) + 'H' + round(xk - dir * r)
                            + 'A' + r + ' ' + r + ' 0 0 ' + (dir > 0 ? 1 : 0) + ' ' + round(xk) + ' ' + round(ys + r)
                            + 'V' + round(ys + STUB), false);
                        return;
                    }
                    const r = Math.min(RADIUS, (ye - ys) / 2);
                    let top = ys;
                    if (spawnShown) {
                        path(plan.id, 'fork', plan.colour,
                            'M' + round(xs) + ' ' + round(ys) + 'H' + round(xk - dir * r)
                            + 'A' + r + ' ' + r + ' 0 0 ' + (dir > 0 ? 1 : 0) + ' ' + round(xk) + ' ' + round(ys + r), false);
                        top = ys + r;
                    }
                    const bottom = mergeShown ? ye - r : ye;
                    if (bottom > top) {
                        path(plan.id, 'lane', plan.colour, 'M' + round(xk) + ' ' + round(top) + 'V' + round(bottom), !plan.open);
                    }
                    if (mergeShown) {
                        const xm = x(slotOf.get(plan.merge.lane) || 0);
                        const dm = xk >= xm ? 1 : -1;
                        path(plan.id, 'merge', plan.colour,
                            'M' + round(xk) + ' ' + round(ye - r)
                            + 'A' + r + ' ' + r + ' 0 0 ' + (dm > 0 ? 1 : 0) + ' ' + round(xk - dm * r) + ' ' + round(ye)
                            + 'H' + round(xm), false);
                    }
                });
                const width = Math.ceil(stage.clientWidth);
                const height = Math.ceil(stage.scrollHeight);
                railHost.innerHTML = paths.length
                    ? "<svg xmlns='http://www.w3.org/2000/svg' width='" + width + "' height='" + height + "' viewBox='0 0 " + width + ' ' + height + "'>" + paths.join('') + '</svg>'
                    : '';
            }

            // ---- triggers ----------------------------------------------------
            const STRUCTURE = ['message-node', 'message', 'children', 'fork-point'];
            function isStructural(node) {
                if (!node || node.nodeType !== 1) return false;
                return STRUCTURE.some(function (cls) { return node.classList.contains(cls); });
            }
            const HIDING = /(?:^|\s)(?:filtered-hidden|search-hidden)(?=\s|$)/;
            function relevant(records) {
                for (let i = 0; i < records.length; i++) {
                    const record = records[i];
                    if (record.type === 'childList') {
                        for (let j = 0; j < record.addedNodes.length; j++) if (isStructural(record.addedNodes[j])) return true;
                        for (let j = 0; j < record.removedNodes.length; j++) if (isStructural(record.removedNodes[j])) return true;
                    } else if (record.attributeName === 'style') {
                        if (record.target.classList && record.target.classList.contains('children')) return true;
                    } else if (record.attributeName === 'class') {
                        const target = record.target;
                        if (!target.classList || !(target.classList.contains('message') || target.classList.contains('fork-point'))) continue;
                        const was = HIDING.test(record.oldValue || '');
                        const now = HIDING.test(target.className);
                        if (was !== now) return true;
                    }
                }
                return false;
            }
            function observe(transcript) {
                observed = transcript;
                if (!observer) {
                    observer = new MutationObserver(function (records) {
                        if (relevant(records)) schedule();
                    });
                }
                observer.observe(transcript, {
                    subtree: true, childList: true, attributes: true,
                    attributeFilter: ['class', 'style'], attributeOldValue: true,
                });
                if (resizer) {
                    resizer.disconnect();
                    resizer.observe(transcript);
                }
            }
            const resizer = window.ResizeObserver ? new ResizeObserver(scheduleDraw) : null;
            window.addEventListener('resize', scheduleDraw);
            if (phone) {
                const onPhone = function () { scheduleDraw(); };
                if (phone.addEventListener) phone.addEventListener('change', onPhone);
                else if (phone.addListener) phone.addListener(onPhone);
            }

            document.addEventListener('click', function (event) {
                const toggle = event.target.closest('.mn-bfold');
                if (toggle) {
                    const ctl = toggle.closest('[data-lane-ref]');
                    const id = ctl ? ctl.getAttribute('data-lane-ref') : null;
                    if (!id || !lanes.has(id)) return;
                    if (isOpen(id, new Map())) fold(id);
                    else select(id);
                    relayout();
                    return;
                }
                const btn = event.target.closest('[data-mn-branches]');
                if (btn) {
                    const value = btn.getAttribute('data-mn-branches');
                    if (GLOBAL_MODES.indexOf(value) < 0) return;
                    globalMode = value;
                    writeGlobal(value);
                    lanes = readLanes();
                    applyGlobal(value);
                    relayout();
                }
            });
            window.addEventListener('hashchange', function () { revealHash(true); });

            if (window.claudeLogOnRehydrate) {
                window.claudeLogOnRehydrate(function (scope) {
                    if (scope && scope.id === 'transcript') observe(scope);
                    schedule();
                });
            }

            function start() {
                const transcript = document.getElementById('transcript');
                if (!transcript) return;
                lanes = readLanes();
                applyGlobal(globalMode);
                lanes.forEach(function (lane, id) { if (!modes.has(id)) modes.set(id, 'folded'); });
                started = true;
                pendingReveals.splice(0).forEach(function (lane) {
                    if (lanes.has(lane) && !isOpen(lane, new Map())) select(lane);
                });
                const hash = window.location.hash;
                const hashTarget = hash && hash.indexOf('#msg-') === 0 ? document.getElementById(hash.slice(1)) : null;
                if (hashTarget) revealLanes(hashTarget);
                observe(transcript);
                relayout();
                if (hashTarget && hashTarget.scrollIntoView) hashTarget.scrollIntoView({ block: 'start' });
            }

            // For tests and the ?debug-dag timing log.
            window.claudeLogDag = {
                relayout: function () { relayout(); return lastTiming; },
                timing: function () { return lastTiming; },
                mode: function (id) { return isOpen(id, new Map()) ? 'interleaved' : 'folded'; },
            };

            if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
            else start();
        })();
