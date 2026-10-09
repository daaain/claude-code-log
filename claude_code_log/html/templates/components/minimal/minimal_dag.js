// Minimal theme (--theme minimal): the DAG engine — branch lanes
// (sub-agents at any depth, workflow agents, rewind forks) drawn as a git-graph rail
// beside the main line, each folded ("Main only", the default),
// interleaved with the main line at its real arrival times, or in a
// column of its own (a swimlane, time-aligned with the main line;
// collapsible to a narrow strip).
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
// the `dag-*` classes, inline `grid-row` / `grid-column` and
// `--dag-tag` on cards, the `.mn-bctls` branch controls, the column
// chrome (`.dag-chrome` elements at the head of #transcript),
// `--dag-slots` / `--dag-cols` and the `dag-on` / `dag-cols`
// classes on the stage, `dag-wide` / `--dag-min` / `--dag-view` on
// <body>, and everything in #dag-rail.
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
    // Global modes; per lane: 'folded' | 'interleaved' | 'column' |
    // 'strip' (a column collapsed to a narrow strip).
    const GLOBAL_MODES = ['main', 'interleaved', 'columns'];
    const CAP = 3; // interleaved lanes (and controls shown) per user turn / workflow group
    // Column mode (P7c): the main column takes twice a branch column's
    // share of the spare width, and never less than MAIN_MIN; on a
    // phone every column (main too) is the viewport's width, one at a
    // time, with horizontal scroll snapping (dag.css).
    const MAIN_MIN = 520; // px: the main column's minimum in column mode
    const MAIN_FR = 2; // the main column's share of the spare width
    const COL_MIN = 300; // px: a column's minimum
    const PHONE_PAD = 20; // px: the body's side padding on a phone (2 × 10px)
    const STRIP = 34; // px: a collapsed column
    const ROW0 = 2; // grid row of the first card (row 1: column heads)
    const MAX_SLOTS = 6; // rail slots beside the main line
    const RADIUS = 8; // fork/merge connector corner
    const STUB = 12; // a folded fork's stub below its row
    const END_R = 3.5; // a running lane's open end marker
    // A column lane's pointers: a few px of lane past the end dot
    // (clear of the dot and its ring), the RADIUS curve, a short tail.
    const POINTER_LEAD = 6;
    const POINTER_TAIL = 3;
    const STRIP_X = 6; // px: a strip's lane, from its left edge (clear of its name)
    // An open lane (an agent with no result yet, P7b) reads as
    // *running* on a page served live (`serve`, live_update.js) —
    // however long it has been quiet (P7c: agents wait on background
    // processes, watchers and Monitor tasks for hours); its label
    // says how long instead ("running · quiet 42m"). Only a session
    // silent for a week is taken to have stopped: `serve` runs over
    // whole archives, and an agent that died with a session closed
    // months ago is not running. A static page (file://, an export)
    // never shows one running. dev-docs/minimal-theme.md § 8.
    const RUNNING_MAX_QUIET_MS = 7 * 24 * 60 * 60 * 1000;
    const QUIET_LABEL_MS = 60 * 1000; // "quiet …" from a minute
    const RUNNING_RECHECK_MS = 30 * 1000;
    // Forks are always --lF; agents cycle through the other lane colours.
    const AGENT_COLOURS = ['a', 'b', 's', 'p'];
    const DEBUG = /[?&]debug-dag(?:[=&]|$)/.test(window.location.search);
    const phone = window.matchMedia ? window.matchMedia('(max-width: 640px)') : null;

    // ---- state ---------------------------------------------------
    let globalMode = readGlobal();
    const modes = new Map(); // lane id -> 'folded' | 'interleaved' | 'column' | 'strip'
    // Turns are keyed by their card's data-uuid (lane.turnKey), not
    // its positional d-N: a live swap renumbers the ids, and the
    // per-turn cap and overflow must follow the turn, not the slot.
    // A workflow's agents are capped per group instead (their phase,
    // or a run without phases: data-lane-group, `g:<runId>/<n>`), so
    // a fan-out of many agents never pushes the turn's other
    // branches out, and gets its own "+N more agents".
    const recency = new Map(); // turn / group key -> interleaved lane ids, least recent first
    const expanded = new Set(); // turn / group keys whose "+N more" are shown
    const acksShown = new Set(); // lane ids whose launch acknowledgement is shown (P7c)
    let lanes = new Map(); // lane id -> record (rebuilt each relayout)
    let started = false;
    let layout = null; // last relayout's result, for redraws
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
        } catch (err) {
            /* storage unavailable: the choice lasts this page */
        }
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
            const turn = head.getAttribute('data-lane-turn') || '';
            found.set(id, {
                id: id,
                head: head,
                kind: head.getAttribute('data-lane-kind') || 'agent',
                name: head.getAttribute('data-lane-name') || id,
                tag: head.getAttribute('data-lane-tag') || '',
                meta: head.getAttribute('data-lane-meta') || '',
                parent: head.getAttribute('data-lane-parent') || 'main',
                from: head.getAttribute('data-lane-from') || '',
                to: head.getAttribute('data-lane-to') || '',
                turn: turn,
                turnKey: turnKeyOf(turn),
                // Workflow agents: the phase (run) they belong to.
                group: head.getAttribute('data-lane-group') || '',
                rank: parseInt(head.getAttribute('data-lane-rank') || '1', 10) || 1,
                stats: head.getAttribute('data-lane-stats') || '',
                // Agents without a result: 'open' (may be running)
                // or 'ended' (the page proves it is over); '' when
                // merged, and for forks (lanes.py, P7b).
                state: head.getAttribute('data-lane-state') || '',
                firstTs: ts[0] ? Date.parse(ts[0]) : NaN
            });
        });
        return found;
    }
    // A stable key for a turn: its card's data-uuid (or session id),
    // falling back to the positional id.
    function turnKeyOf(turn) {
        if (!turn) return '';
        const card = document.getElementById('msg-' + turn);
        const stable = card && (card.getAttribute('data-uuid') || card.getAttribute('data-session-id'));
        return stable ? 'u:' + stable : turn;
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
    // The cap's unit: the user turn, or a workflow agent's group.
    function turnOf(id) {
        const lane = lanes.get(id);
        if (lane && lane.group) return 'g:' + lane.group;
        return lane ? lane.turnKey || 'lane:' + id : 'lane:' + id;
    }
    function defaultMode(lane, value) {
        if (value === 'columns') return 'column';
        return value === 'interleaved' && lane.rank <= CAP ? 'interleaved' : 'folded';
    }
    function modeOf(id) {
        return modes.get(id) || 'folded';
    }
    function inColumn(mode) {
        return mode === 'column' || mode === 'strip';
    }
    function forget(id) {
        const turn = turnOf(id);
        recency.set(
            turn,
            (recency.get(turn) || []).filter(function (x) {
                return x !== id;
            })
        );
    }

    // Interleave a lane (and, first, the folded lanes it is nested
    // in), most recent last; past the cap the least recently selected
    // lane of the same turn folds — never one the new lane needs to be
    // visible. A reveal (search, a link, the timeline) passes
    // `evict === false`: it never hides what the reader has open, so a
    // turn can go past the cap until the next manual selection.
    // An enclosing lane in a column stays there (the lane then shows
    // interleaved in that column); a strip expands back to a column.
    function select(id, evict) {
        if (!lanes.has(id)) return;
        const keep = ancestorsOf(id).concat([id]);
        const turn = turnOf(id);
        let list = recency.get(turn) || [];
        keep.forEach(function (lane) {
            const mode = modeOf(lane);
            if (mode === 'strip') modes.set(lane, 'column');
            if (inColumn(modeOf(lane)) && lane !== id) return;
            modes.set(lane, 'interleaved');
            list = list.filter(function (x) {
                return x !== lane;
            });
            list.push(lane);
        });
        recency.set(turn, list);
        if (evict === false) return;
        while (list.length > CAP) {
            const victim = list.find(function (x) {
                return keep.indexOf(x) < 0;
            });
            if (!victim) break;
            fold(victim);
            list = recency.get(turn) || [];
        }
    }
    // Put a lane in a column of its own (and the folded lanes it is
    // nested in: a column needs its parent shown). Columns are not
    // capped.
    function column(id) {
        if (!lanes.has(id)) return;
        ancestorsOf(id).forEach(function (lane) {
            const mode = modeOf(lane);
            if (mode === 'folded' || mode === 'strip') {
                modes.set(lane, 'column');
                forget(lane);
            }
        });
        modes.set(id, 'column');
        forget(id);
    }
    function strip(id) {
        if (!lanes.has(id)) return;
        modes.set(id, 'strip');
        forget(id);
    }
    // Fold a lane and every lane nested in it.
    function fold(id) {
        modes.set(id, 'folded');
        forget(id);
        lanes.forEach(function (lane, other) {
            if (other !== id && ancestorsOf(other).indexOf(id) >= 0 && modeOf(other) !== 'folded') {
                fold(other);
            }
        });
    }
    function applyGlobal(value) {
        recency.clear();
        const byTurn = new Map();
        lanes.forEach(function (lane) {
            modes.set(lane.id, value === 'columns' ? 'column' : 'folded');
            const list = byTurn.get(turnOf(lane.id)) || [];
            list.push(lane);
            byTurn.set(turnOf(lane.id), list);
        });
        if (value !== 'interleaved') return;
        byTurn.forEach(function (list) {
            list.sort(function (a, b) {
                return a.rank - b.rank;
            });
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
            const mode = defaultMode(lane, globalMode);
            if (mode === 'column') {
                modes.set(id, 'column');
                return;
            }
            if (mode !== 'interleaved') return;
            const list = recency.get(turnOf(id)) || [];
            if (list.length < CAP) select(id);
        });
    }
    // Open = its cards are shown (interleaved, or in an expanded
    // column), and so are its parent's.
    function isOpen(id, memo) {
        if (id === 'main') return true;
        if (memo.has(id)) return memo.get(id);
        memo.set(id, false); // cycle guard
        const mode = modeOf(id);
        const open = (mode === 'interleaved' || mode === 'column') && parentOpen(id, memo);
        memo.set(id, open);
        return open;
    }
    function parentOpen(id, memo) {
        const parent = lanes.has(id) ? lanes.get(id).parent : 'main';
        return parent === 'main' || !lanes.has(parent) || isOpen(parent, memo);
    }
    // The column a lane's cards are laid out in: its own when it is in
    // a column (or a strip), else its parent's — so a lane interleaved
    // inside a column lane shows in that column — else 'main'.
    function columnKey(id, memo) {
        if (!id || id === 'main' || !lanes.has(id)) return 'main';
        const hit = memo.get(id);
        if (hit !== undefined) return hit;
        memo.set(id, 'main'); // cycle guard
        const key = inColumn(modeOf(id)) ? id : columnKey(lanes.get(id).parent, memo);
        memo.set(id, key);
        return key;
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

    // ---- running lanes (P7b, rule revised in P7c) ------------------
    // The server marks an agent lane without a result 'open' unless
    // the page proves it is over ('ended': a parent that moved on, a
    // TaskStop, a finished parent lane); it cannot know whether the
    // session is still being written, so the page decides: an open
    // lane is *running* while the page is served live — unless its
    // session (any card, in any lane) has been silent for
    // RUNNING_MAX_QUIET_MS. Elapsed time alone never ends it sooner:
    // the label shows how long the lane has been quiet instead. A
    // static page never shows one running.
    // Newest activity per session (top-level node): on a combined
    // page a live session must not make another session's crashed
    // agent read as running. Lane id -> its session's newest time.
    function newestTimes(model, sequence) {
        const bySession = new Map();
        sequence.forEach(function (item) {
            if (isNaN(item.ts)) return;
            const seen = bySession.get(item.session);
            if (seen === undefined || item.ts > seen) bySession.set(item.session, item.ts);
        });
        const byLane = new Map();
        lanes.forEach(function (lane, id) {
            if (lane.state !== 'open') return;
            const anchor = model.spawns.get(id) || (model.seqs.get(id) || [])[0];
            const newest = anchor ? bySession.get(anchor.session) : undefined;
            if (newest !== undefined) byLane.set(id, newest);
        });
        return byLane;
    }
    // A lane's last activity: its newest card, or a nested lane's
    // (a parent waiting on a busy child is not quiet), else its
    // spawn's time.
    function laneActivity(model, sequence) {
        const out = new Map();
        sequence.forEach(function (item) {
            if (isNaN(item.ts) || item.lane === 'main') return;
            let id = item.lane;
            let guard = 0;
            while (id && guard++ < 64) {
                const seen = out.get(id);
                if (seen === undefined || item.ts > seen) out.set(id, item.ts);
                id = parentOf(id);
            }
        });
        lanes.forEach(function (lane, id) {
            if (out.has(id)) return;
            const spawn = model.spawns.get(id);
            if (spawn && !isNaN(spawn.ts)) out.set(id, spawn.ts);
        });
        return out;
    }
    // Running lanes: lane id -> how long it has been quiet (ms).
    function runningLanes(newest, activity) {
        const out = new Map();
        if (!window.claudeLogLiveUpdate) return out;
        const now = Date.now();
        newest.forEach(function (ts, id) {
            if (now - ts > RUNNING_MAX_QUIET_MS) return;
            if (!lanes.has(id) || lanes.get(id).state !== 'open') return;
            const last = activity.get(id);
            out.set(id, last === undefined || isNaN(last) ? 0 : Math.max(0, now - last));
        });
        return out;
    }
    function quietText(ms) {
        if (!(ms >= QUIET_LABEL_MS)) return '';
        const m = Math.floor(ms / 60000);
        if (m < 60) return m + 'm';
        const h = Math.floor(m / 60);
        if (h < 24) return h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
        const d = Math.floor(h / 24);
        return d + 'd' + (h % 24 ? ' ' + (h % 24) + 'h' : '');
    }
    function runningText(running, id) {
        const quiet = quietText(running.get(id));
        return 'running' + (quiet ? ' · quiet ' + quiet : '');
    }
    function sameSet(a, b) {
        if (a.size !== b.size) return false;
        let same = true;
        a.forEach(function (_value, key) {
            if (!b.has(key)) same = false;
        });
        return same;
    }

    // ---- reveal: links, deep links, search, timeline open lanes ----
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
        openForReveal(lane);
        return true;
    }
    // A reveal opens the lane the way the page is being read: as a
    // column under the global Columns choice (or when it, or a lane
    // it is nested in, is a strip), interleaved otherwise — without
    // evicting anything (select's `evict === false`): a search that
    // hits more than three lanes of one turn shows every hit.
    function openForReveal(id) {
        const chain = ancestorsOf(id).concat([id]);
        const stripped = chain.some(function (lane) {
            return modeOf(lane) === 'strip';
        });
        if (globalMode === 'columns' || stripped || inColumn(modeOf(id))) column(id);
        else select(id, false);
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
                get: function () {
                    return wrapped;
                },
                set: function (fn) {
                    inner = fn;
                    wrapped = fn ? wrap(fn) : fn;
                }
            });
        } catch (err) {
            /* leave the hook unwrapped */
        }
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
    // block: teammate threads (spec § 1.6.5), old-style sidechains
    // without an agent transcript. (Workflow phase and agent cards
    // are rows: each agent's transcript is a lane of its own.)
    function isBlockGroup(owner, kid) {
        return !!owner && kid.classList.contains('sidechain') && agentDepth(kid) > agentDepth(owner);
    }
    // A sub-agent's or a workflow agent's transcript (not a fork).
    const AGENT_LANE = /^(?:agent|wfagent)-/;

    function buildModel() {
        const transcript = document.getElementById('transcript');
        const memo = new Map();
        const model = {
            items: [],
            seqs: new Map(),
            spawns: new Map(), // lane -> spawn item
            merges: new Map(), // lane -> merge item
            cls: new Map(), // element -> engine classes wanted
            spawnCards: []
        };
        if (!transcript) return model;
        function want(el, cls) {
            const prev = model.cls.get(el);
            model.cls.set(el, prev ? prev + ' ' + cls : cls);
        }
        function addItem(el, lane, kind, hidden) {
            const item = {
                el: el,
                lane: lane,
                kind: kind,
                hidden: hidden || el.classList.contains('filtered-hidden') || el.classList.contains('search-hidden'),
                row: -1,
                ts: NaN,
                session: session
            };
            model.items.push(item);
            let seq = model.seqs.get(lane);
            if (!seq) model.seqs.set(lane, (seq = []));
            seq.push(item);
            if (kind === 'block') return item;
            const spawns = el.getAttribute('data-spawns');
            if (spawns) {
                model.spawnCards.push(item);
                spawns.split(' ').forEach(function (id) {
                    if (id) model.spawns.set(id, item);
                });
            }
            const merges = el.getAttribute('data-merges');
            if (merges)
                merges.split(' ').forEach(function (id) {
                    if (id) model.merges.set(id, item);
                });
            return item;
        }
        function walkContainer(container, lane, hidden) {
            const kids = container.children;
            for (let i = 0; i < kids.length; i++) {
                const child = kids[i];
                const list = child.classList;
                if (list.contains('message-node')) walkNode(child, lane, hidden);
                else if (list.contains('fork-point'))
                    addItem(child, child.getAttribute('data-lane') || lane, 'box', hidden);
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
                    const kidLane = first ? first.getAttribute('data-lane') || cardLane : cardLane;
                    if (first && kidLane !== cardLane && AGENT_LANE.test(kidLane)) {
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
        // Top-level nodes are sessions (a combined page holds many):
        // each item records which, for the running-lane rule.
        let session = 0;
        const tops = transcript.children;
        for (let i = 0; i < tops.length; i++) {
            session = i;
            const child = tops[i];
            if (child.classList.contains('message-node')) walkNode(child, 'main', false);
            else if (child.classList.contains('fork-point'))
                addItem(child, child.getAttribute('data-lane') || 'main', 'box', false);
        }
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
        model.seqs.forEach(function (seq, id) {
            if (id !== 'main') others.push(id);
        });
        if (!others.length) return main.slice();
        const seqs = [main].concat(
            others.map(function (id) {
                return model.seqs.get(id);
            })
        );
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
        const pos = seqs.map(function () {
            return 0;
        });
        const index = new Map();
        ids.forEach(function (id, k) {
            index.set(id, k);
        });
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
                if (best < 0 || ts < bestTs) {
                    best = k;
                    bestTs = ts;
                }
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
                    opened.forEach(function (k) {
                        if (active.indexOf(k) < 0) active.push(k);
                    });
                    waiting.delete(item);
                }
            } while (
                opensPair(item) &&
                pos[best] < seqs[best].length &&
                closesPair(seqs[best][pos[best]]) &&
                !blocked(seqs[best][pos[best]])
            );
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

    // Rows — ported from DagRail.dc.html / DagLanes.dc.html
    // renderVals() "Pack rows": time order is kept top to bottom; a
    // lane's first row is below its spawn, a merge row below the
    // merged lane's last row. `nextFree` is kept per column (`keyOf`:
    // a column lane's id, else 'main'), so cards in different columns
    // may share a row; without columns it is one item per row.
    function pack(sequence, model, keyOf) {
        const nextFree = new Map();
        const lastRowOf = new Map();
        let prev = 0;
        sequence.forEach(function (item) {
            const key = keyOf(item.lane);
            item.key = key;
            let r = Math.max(prev, nextFree.get(key) || 0);
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
            nextFree.set(key, r + 1);
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
    function railLanes(model, open, packed, skip, running) {
        const memo = model.memo;
        const plans = [];
        lanes.forEach(function (lane, id) {
            // Columns have no rail (the mockup's `railed` = folded +
            // interleaved), nor do lanes inside one, nor the lanes
            // behind a turn's "+N more branches".
            if (skip(id)) return;
            const opened = open.indexOf(id) >= 0;
            const spawn = model.spawns.get(id);
            const parentOpen = lane.parent === 'main' || !lanes.has(lane.parent) || isOpen(lane.parent, memo);
            if (!parentOpen) return;
            const seq = opened ? model.seqs.get(id) || [] : [];
            if (!spawn && !seq.length) return;
            const merge = model.merges.get(id);
            const first = seq.length ? seq[0].row : -1;
            const last = seq.length ? packed.lastRowOf.get(id) : -1;
            const start = spawn && spawn.row >= 0 ? spawn.row : first;
            let end = start;
            const live = running.has(id);
            if (merge && merge.row >= 0) end = merge.row;
            else if (live) end = packed.rows - 1; // runs on to the newest row
            else if (opened && last >= 0) end = last;
            plans.push({
                id: id,
                lane: lane,
                open: opened,
                running: live,
                spawn: spawn,
                merge: merge,
                seq: seq,
                start: start,
                end: Math.max(start, end)
            });
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
        return {
            plans: plans.filter(function (p) {
                return p.slot;
            }),
            slotOf: slotOf,
            slots: slots.length ? slots.length - 1 : 0
        };
    }

    // ---- writes ----------------------------------------------------
    const ENGINE_CLASSES = /(?:^|\s)(dag-[a-z0-9-]+)(?=\s|$)/g;
    let classed = new Set();
    const rowWritten = new WeakMap();
    const colWritten = new WeakMap();
    const tagWritten = new WeakMap();

    function setEngineClasses(el, wanted) {
        const current = [];
        el.className.replace(ENGINE_CLASSES, function (m, cls) {
            current.push(cls);
            return m;
        });
        const next = wanted ? wanted.split(' ') : [];
        current.forEach(function (cls) {
            if (next.indexOf(cls) < 0) el.classList.remove(cls);
        });
        next.forEach(function (cls) {
            if (cls && current.indexOf(cls) < 0) el.classList.add(cls);
        });
    }

    function cssString(text) {
        return (
            '"' +
            String(text).replace(/["\\\n]/g, function (c) {
                return c === '\n' ? ' ' : '\\' + c;
            }) +
            '"'
        );
    }

    const CHEVRON =
        "<svg width='10' height='10' viewBox='0 0 12 12' aria-hidden='true'><path d='M4 2.5 7.5 6 4 9.5' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'></path></svg>";

    const forkTimes = new Map();
    function localTime(cardId) {
        const card = cardId ? document.getElementById('msg-' + cardId) : null;
        const stamp = card ? card.querySelector('.timestamp[data-timestamp]') : null;
        const date = stamp ? new Date(stamp.getAttribute('data-timestamp')) : null;
        if (!date || isNaN(date.getTime())) return '';
        return date.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        });
    }

    // One control per lane, in a container the engine owns at the end
    // of the spawn card (grid row 4 of the card). Labels are generated
    // content (`data-label`), so search and the timeline never index
    // them. A live patch replaces the card and drops the container;
    // the next relayout puts it back. `overflow` hides the controls of
    // a turn's lanes past the third (until "+N more branches") and
    // says which control carries that toggle.
    function setAttr(el, name, value) {
        if (el.getAttribute(name) !== value) el.setAttribute(name, value);
    }
    // An async spawn's result card that holds nothing but the launch
    // acknowledgement (`.mn-ack`, html/renderer.py) and its
    // `Result ↓` line (P7c compact spawn rows): the engine hides it
    // (`dag-ack`) and the spawn's control carries both — a
    // `Result ↓` link and a `launched` toggle that shows the card
    // again. A result card with anything else on it stays.
    function ackCardOf(card) {
        const node = card.parentElement;
        const next = node && node.classList.contains('message-node') ? node.nextElementSibling : null;
        const result =
            next && next.classList.contains('message-node')
                ? next.querySelector(':scope > .message.tool_result.pair_last')
                : null;
        const content = result ? result.querySelector(':scope > .content') : null;
        if (!content || !content.querySelector(':scope > .mn-ack')) return null;
        const kids = content.children;
        for (let i = 0; i < kids.length; i++) {
            if (!kids[i].classList.contains('mn-ack') && !kids[i].classList.contains('mn-async-jump')) return null;
        }
        return result;
    }
    function ensureExtra(ctl, cls, tag, before) {
        let el = ctl.querySelector(':scope > .' + cls);
        if (!el) {
            el = document.createElement(tag);
            el.className = cls + (tag === 'button' ? ' mn-bmini' : '');
            if (tag === 'button') el.type = 'button';
            ctl.insertBefore(el, before);
        }
        return el;
    }
    function ensureControls(item, colours, memo, overflow, running, acks) {
        const ids = (item.el.getAttribute('data-spawns') || '').split(' ').filter(function (id) {
            return lanes.has(id);
        });
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
        ids.sort(function (a, b) {
            return lanes.get(a).rank - lanes.get(b).rank;
        });
        const existing = Array.from(box.children);
        ids.forEach(function (id, i) {
            const lane = lanes.get(id);
            let ctl = existing.find(function (el) {
                return el.getAttribute('data-lane-ref') === id;
            });
            if (!ctl) {
                ctl = document.createElement('div');
                ctl.setAttribute('data-lane-ref', id);
                ctl.innerHTML =
                    "<button type='button' class='mn-bfold'>" +
                    CHEVRON +
                    '</button>' +
                    "<span class='mn-brun' data-label='running'></span>" +
                    "<button type='button' class='mn-bmini mn-bcol'></button>";
            }
            if (box.children[i] !== ctl) box.insertBefore(ctl, box.children[i] || null);
            const mode = modeOf(id);
            const opened = isOpen(id, memo);
            const columned = inColumn(mode) && parentOpen(id, memo);
            const interleaved = opened && !columned;
            const colour = colours.get(id) || (lane.kind === 'fork' ? 'f' : 'a');
            const live = running.has(id);
            const cls =
                'mn-bctl dag-lc-' +
                colour +
                (interleaved ? ' is-open' : '') +
                (columned ? ' is-col' : '') +
                (live ? ' is-running' : '');
            if (ctl.className !== cls) ctl.className = cls;
            const hide = overflow.hidden.has(id);
            if (ctl.hidden !== hide) ctl.hidden = hide;
            const fork = lane.kind === 'fork';
            // A phase spawns several workflow agents: each control
            // names its agent (a Task's spawn row is its own name).
            const named = fork || lane.kind === 'workflow-agent';
            // An agent without a result that is not running ended
            // without one (or the page is a static copy).
            const unfinished = !live && (lane.state === 'open' || lane.state === 'ended');
            const label =
                (fork ? '⑂ ' : '') +
                (named ? lane.name + (lane.stats ? ' · ' : '') : '') +
                (named ? lane.stats : lane.stats || lane.name) +
                (unfinished ? ' · no result' : '');
            // The mode is its own part (P8): on a narrow line the
            // label gives way (ellipsis), the mode stays readable.
            const modeText = interleaved ? ' · interleaved' : columned ? ' · in column →' : '';
            const btn = ctl.querySelector('.mn-bfold');
            setAttr(btn, 'data-label', label);
            if (modeText) setAttr(btn, 'data-mode', modeText);
            else if (btn.hasAttribute('data-mode')) btn.removeAttribute('data-mode');
            setAttr(btn, 'aria-expanded', opened ? 'true' : 'false');
            const runLabel = live ? runningText(running, id) : 'running';
            const pill = ctl.querySelector('.mn-brun');
            if (pill) setAttr(pill, 'data-label', runLabel);
            setAttr(
                btn,
                'aria-label',
                (mode === 'folded' ? 'Interleave branch: ' : 'Fold branch: ') +
                    lane.name +
                    (lane.stats
                        ? ' (' + lane.stats + (live ? ', ' + runLabel : unfinished ? ', no result' : '') + ')'
                        : '')
            );
            // The stats too: a narrow control truncates its label (P8).
            let title =
                lane.name +
                (lane.meta ? ' · ' + lane.meta : '') +
                (lane.stats ? ' · ' + lane.stats : '') +
                (live ? ' · still ' + runLabel : unfinished ? ' · ended without a result' : '');
            if (fork) {
                // Keyed by lane and card: a live swap can renumber d-N.
                const key = id + '|' + lane.from;
                if (!forkTimes.has(key)) forkTimes.set(key, localTime(lane.from));
                const at = forkTimes.get(key);
                title = lane.name + ' · rewound' + (at ? ' to ' + at : '');
            }
            if (btn.title !== title) btn.title = title;
            const col = ctl.querySelector('.mn-bcol');
            if (col) {
                setAttr(col, 'data-label', inColumn(mode) ? '⇤ Interleave' : 'Column ⇥');
                setAttr(
                    col,
                    'aria-label',
                    (inColumn(mode) ? 'Interleave branch: ' : 'Show in its own column: ') + lane.name
                );
                const colTitle = inColumn(mode)
                    ? 'Merge this branch back into the main line'
                    : 'Show this branch in its own column';
                if (col.title !== colTitle) col.title = colTitle;
            }
            // P7c: an async agent's `Result ↓` (its answer is on the
            // notification, the merge row further down) and the
            // folded launch acknowledgement, between the stats and
            // the Column button.
            const resultHref = lane.kind === 'async-agent' && lane.to ? '#msg-' + lane.to : '';
            let res = ctl.querySelector(':scope > .mn-bres');
            if (resultHref) {
                res = ensureExtra(ctl, 'mn-bres', 'a', col);
                setAttr(res, 'href', resultHref);
                setAttr(res, 'data-label', 'Result ↓');
                setAttr(res, 'aria-label', 'Result of ' + lane.name + ' (with the async notification)');
                if (res.title !== 'Jump to the result, with the async notification')
                    res.title = 'Jump to the result, with the async notification';
            } else if (res) {
                res.remove();
            }
            let ack = ctl.querySelector(':scope > .mn-back');
            if (acks.has(id)) {
                ack = ensureExtra(ctl, 'mn-back', 'button', col);
                const shown = acksShown.has(id);
                setAttr(ack, 'data-label', shown ? 'launched ▾' : 'launched ▸');
                setAttr(ack, 'aria-expanded', shown ? 'true' : 'false');
                setAttr(ack, 'aria-label', (shown ? 'Hide' : 'Show') + ' the launch acknowledgement of ' + lane.name);
                if (ack.title !== 'The launch acknowledgement (the tool result)')
                    ack.title = 'The launch acknowledgement (the tool result)';
            } else if (ack) {
                ack.remove();
            }
            // "+N more branches" / "− fewer branches" on the turn's
            // third control.
            const more = overflow.anchors.get(id);
            let toggle = ctl.querySelector('.mn-bmore');
            if (more) {
                if (!toggle) {
                    toggle = document.createElement('button');
                    toggle.type = 'button';
                    toggle.className = 'mn-bmini mn-bmore';
                    ctl.appendChild(toggle);
                }
                setAttr(toggle, 'data-turn', more.turn);
                const agents = more.noun === 'agent';
                const plural = agents ? 'agents' : 'branches';
                const counted = more.count + ' more ' + (more.count === 1 ? more.noun : plural);
                const scope = agents ? 'this workflow phase' : 'this turn';
                setAttr(toggle, 'data-label', more.expanded ? '− fewer ' + plural : '+' + counted);
                setAttr(
                    toggle,
                    'aria-label',
                    more.expanded ? 'Hide the extra ' + plural + ' of ' + scope : 'Show ' + counted + ' of ' + scope
                );
                setAttr(toggle, 'aria-expanded', more.expanded ? 'true' : 'false');
            } else if (toggle) {
                toggle.remove();
            }
        });
        existing.forEach(function (el) {
            if (ids.indexOf(el.getAttribute('data-lane-ref')) < 0) el.remove();
        });
    }

    // Branch overflow (spec § 1.6.3, per user turn): of a turn's
    // top-level lanes (spawned from the main line), the first three by
    // rank keep a control (and a rail lane); the rest stay folded
    // behind a "+N more branches" toggle on the third control, unless
    // the turn is expanded or the lane is not folded (a lane the
    // reader opened, or a reveal did, always shows its control).
    // Nested lanes are left out: they only show once the reader opens
    // their parent, and counting them would make opening one lane hide
    // another's control. A workflow group ("+N more agents") counts
    // at any depth: its lanes share one spawn row, so one parent.
    function computeOverflow(model) {
        const hidden = new Set();
        const anchors = new Map();
        const byTurn = new Map();
        lanes.forEach(function (lane, id) {
            if ((parentOf(id) && !lane.group) || !model.spawns.has(id)) return;
            const turn = turnOf(id);
            const list = byTurn.get(turn) || [];
            list.push(lane);
            byTurn.set(turn, list);
        });
        byTurn.forEach(function (list, turn) {
            if (list.length <= CAP) return;
            list.sort(function (a, b) {
                return a.rank - b.rank;
            });
            const isExpanded = expanded.has(turn);
            let count = 0;
            list.slice(CAP).forEach(function (lane) {
                if (modeOf(lane.id) !== 'folded') return;
                count++;
                if (!isExpanded) hidden.add(lane.id);
            });
            const noun = list[CAP - 1].group ? 'agent' : 'branch';
            if (isExpanded || count)
                anchors.set(list[CAP - 1].id, { turn: turn, count: count, expanded: isExpanded, noun: noun });
        });
        return { hidden: hidden, anchors: anchors };
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

    // Column tracks: lanes in a column (or strip) whose parent is
    // shown, ordered by spawn row, each nested lane right after its
    // parent column's subtree (DagLanes.dc.html, generalised).
    function columnTracks(model, memo) {
        const tracks = [];
        lanes.forEach(function (lane, id) {
            if (inColumn(modeOf(id)) && parentOpen(id, memo)) tracks.push(id);
        });
        if (!tracks.length) return tracks;
        const set = new Set(tracks);
        function spawnRow(id) {
            const spawn = model.spawns.get(id);
            if (spawn && spawn.row >= 0) return spawn.row;
            const seq = model.seqs.get(id);
            return seq && seq.length ? seq[0].row : Infinity;
        }
        const kids = new Map();
        const roots = [];
        tracks.forEach(function (id) {
            let up = parentOf(id);
            while (up && !set.has(up)) up = parentOf(up);
            if (up) {
                if (!kids.has(up)) kids.set(up, []);
                kids.get(up).push(id);
            } else {
                roots.push(id);
            }
        });
        const byRow = function (a, b) {
            return spawnRow(a) - spawnRow(b) || lanes.get(a).rank - lanes.get(b).rank;
        };
        const out = [];
        (function visit(list) {
            list.sort(byRow).forEach(function (id) {
                out.push(id);
                if (kids.has(id)) visit(kids.get(id));
            });
        })(roots);
        return out;
    }

    // Column chrome: per column a background spanning every row (the
    // mockup's `colbg`: lane-colour edge + tint) holding its head — a
    // name / meta line with "⇤ Interleave" and "Collapse", sticky under
    // the toolbar — or, collapsed, a narrow strip with the name set
    // vertically (click to expand); plus the main column's "Main
    // session" head. They are engine-owned grid items at the head of
    // #transcript: first in DOM order, so they paint under the cards.
    // Nothing else reads them (live updates, search, the filter and
    // the timeline all work on `.message-node` / `.message`), and a
    // wholesale swap drops them until the next relayout.
    const chrome = new Map(); // column key -> element
    function syncChrome(transcript, tracks, colours, rows, running) {
        const wanted = tracks.length ? ['main'].concat(tracks) : [];
        chrome.forEach(function (el, key) {
            if (wanted.indexOf(key) < 0 || el.parentNode !== transcript) {
                el.remove();
                if (wanted.indexOf(key) < 0) chrome.delete(key);
            }
        });
        transcript.querySelectorAll(':scope > .dag-chrome').forEach(function (el) {
            if (wanted.indexOf(el.getAttribute('data-col')) < 0) el.remove();
        });
        wanted.forEach(function (key, i) {
            const main = key === 'main';
            const lane = main ? null : lanes.get(key);
            const isStrip = !main && modeOf(key) === 'strip';
            let el = chrome.get(key);
            const shape = main ? 'main' : isStrip ? 'strip' : 'col';
            if (!el || el.getAttribute('data-shape') !== shape) {
                if (el) el.remove();
                el = document.createElement('div');
                el.setAttribute('data-col', key);
                el.setAttribute('data-shape', shape);
                if (main) {
                    el.innerHTML = "<div class='dag-colhead dag-mainhead' data-label='Main session'></div>";
                } else if (isStrip) {
                    el.innerHTML =
                        "<button type='button' class='dag-strip' data-col-act='expand'><span></span></button>";
                } else {
                    el.innerHTML =
                        "<div class='dag-colhead'><span class='dag-colname'></span><span class='dag-colmeta'></span>" +
                        "<span class='dag-colacts'><button type='button' class='mn-bmini' data-col-act='interleave' data-label='⇤' data-long='⇤ Interleave'" +
                        " title='Merge this branch back into the main line'></button>" +
                        "<button type='button' class='mn-bmini' data-col-act='collapse' data-label='−' data-long='Collapse'" +
                        " title='Collapse this column to a narrow strip'></button></span></div>";
                }
                chrome.set(key, el);
            }
            const cls =
                'dag-chrome dag-colbg' +
                (main ? ' dag-mainbg' : ' dag-lc-' + (colours.get(key) || 'a')) +
                (isStrip ? ' dag-stripbg' : '');
            if (el.className !== cls) el.className = cls;
            el.style.gridColumn = String(i + 1);
            el.style.gridRow = '1 / span ' + Math.max(1, rows + ROW0 - 1);
            if (lane) {
                // Stats first (P8): they are what a narrow head keeps.
                const meta = [lane.stats, running.has(key) ? runningText(running, key) : '', lane.meta]
                    .filter(Boolean)
                    .join(' · ');
                if (isStrip) {
                    const btn = el.querySelector('.dag-strip');
                    setAttr(btn, 'aria-label', 'Expand column: ' + lane.name);
                    btn.title = lane.name + (meta ? ' · ' + meta : '');
                    setAttr(btn.firstChild, 'data-label', lane.name);
                } else {
                    setAttr(
                        el.querySelector('.dag-colname'),
                        'data-label',
                        (lane.kind === 'fork' ? '⑂ ' : '') + lane.name
                    );
                    setAttr(el.querySelector('.dag-colmeta'), 'data-label', meta);
                    const head = el.querySelector('.dag-colhead');
                    const title = lane.name + (meta ? ' · ' + meta : '');
                    if (head.title !== title) head.title = title;
                    setAttr(
                        el.querySelector("[data-col-act='interleave']"),
                        'aria-label',
                        'Interleave branch: ' + lane.name
                    );
                    setAttr(
                        el.querySelector("[data-col-act='collapse']"),
                        'aria-label',
                        'Collapse column: ' + lane.name
                    );
                }
            }
            if (el.parentNode !== transcript || el.previousSibling !== (i ? chrome.get(wanted[i - 1]) : null)) {
                transcript.insertBefore(el, i ? chrome.get(wanted[i - 1]).nextSibling : transcript.firstChild);
            }
        });
    }

    function relayout() {
        if (!started) return;
        const t0 = performance.now();
        lanes = readLanes();
        adoptNewLanes();
        const model = buildModel();
        const memo = model.memo;
        const keyMemo = new Map();
        const keyOf = function (id) {
            return columnKey(id, keyMemo);
        };
        // Interleaved into the main column: rail slots, tint, tags.
        const open = [];
        lanes.forEach(function (lane, id) {
            if (isOpen(id, memo) && keyOf(id) === 'main' && (model.seqs.get(id) || []).length) open.push(id);
        });
        const t1 = performance.now();
        const sequence = order(model);
        const packed = pack(sequence, model, keyOf);
        const newest = newestTimes(model, sequence);
        const activity = laneActivity(model, sequence);
        const running = runningLanes(newest, activity);
        const tracks = columnTracks(model, memo);
        const colIndex = new Map();
        const colours = new Map();
        tracks.forEach(function (id, i) {
            colIndex.set(id, i + 2);
            colours.set(id, lanes.get(id).kind === 'fork' ? 'f' : AGENT_COLOURS[i % AGENT_COLOURS.length]);
        });
        const overflow = computeOverflow(model);
        const openSet = new Set(open);
        const rail = railLanes(
            model,
            open,
            packed,
            function (id) {
                return keyOf(id) !== 'main' || overflow.hidden.has(id);
            },
            running
        );
        rail.plans.forEach(function (plan) {
            colours.set(plan.id, plan.colour);
        });
        // A lane inside a column takes its column's colour.
        lanes.forEach(function (lane, id) {
            const key = keyOf(id);
            if (key !== 'main' && key !== id && colours.has(key)) colours.set(id, colours.get(key));
        });
        const t2 = performance.now();

        // Per-card engine state: interleaved rows carry their lane's
        // slot, colour and gutter tag; column rows their column's
        // colour (and, for a lane interleaved inside a column, its
        // tag); a pair split by other rows shows both halves in full.
        const tags = new Map();
        const want = function (el, cls) {
            const prev = model.cls.get(el);
            model.cls.set(el, prev ? prev + ' ' + cls : cls);
        };
        open.forEach(function (id) {
            const slot = rail.slotOf.get(id) || 1;
            const lane = lanes.get(id);
            const cls = 'dag-in dag-s' + Math.min(slot, MAX_SLOTS) + ' dag-lc-' + (colours.get(id) || 'a');
            (model.seqs.get(id) || []).forEach(function (item) {
                want(item.el, cls);
                if (item.kind === 'card') tags.set(item.el, lane.tag || lane.kind);
            });
        });
        const acks = new Map();
        model.spawnCards.forEach(function (item) {
            if (item.el.classList.contains('fork-point')) return;
            const card = ackCardOf(item.el);
            if (!card) return;
            (item.el.getAttribute('data-spawns') || '').split(' ').forEach(function (id) {
                if (!id || !lanes.has(id)) return;
                acks.set(id, card);
                if (!acksShown.has(id)) want(card, 'dag-ack');
            });
        });
        const shownLanes = new Set(openSet);
        tracks.forEach(function (key) {
            if (modeOf(key) !== 'column') return;
            lanes.forEach(function (lane, id) {
                if (keyOf(id) !== key || !isOpen(id, memo)) return;
                shownLanes.add(id);
                const cls = 'dag-col dag-lc-' + (colours.get(key) || 'a') + (id !== key ? ' dag-colin' : '');
                (model.seqs.get(id) || []).forEach(function (item) {
                    want(item.el, cls);
                    if (id !== key && item.kind === 'card') tags.set(item.el, lane.tag || lane.kind);
                });
            });
        });
        if (tracks.length) {
            // The first shown card under each column head (main's
            // included) draws no hairline: the head's edge is there.
            const firstSeen = new Set();
            sequence.forEach(function (item) {
                if (item.kind !== 'card' || item.hidden || firstSeen.has(item.key)) return;
                if (item.key !== 'main' && !colIndex.has(item.key)) return;
                firstSeen.add(item.key);
                want(item.el, 'dag-cfirst');
            });
        }
        {
            // Only rows that are shown count: a folded lane between
            // two halves leaves empty rows, not a split.
            const lastOf = new Map();
            const isHalf = function (el, a, b) {
                return el.classList.contains(a) || el.classList.contains(b);
            };
            let shown = 0;
            let lastRow = -1;
            sequence.forEach(function (item) {
                if (item.lane !== 'main' && !shownLanes.has(item.lane)) return;
                // Rows shared across columns count once.
                if (item.row !== lastRow) shown++;
                lastRow = item.row;
                item.shown = shown;
                const previous = lastOf.get(item.lane);
                lastOf.set(item.lane, item);
                if (!previous || item.shown === previous.shown + 1) return;
                if (!isHalf(item.el, 'pair_middle', 'pair_last')) return;
                if (!isHalf(previous.el, 'pair_first', 'pair_middle')) return;
                [previous.el, item.el].forEach(function (half) {
                    want(half, 'dag-split');
                });
            });
        }

        // The one layout read before the writes (setView's width).
        const view = tracks.length ? document.documentElement.clientWidth : 0;
        if (observer) observer.disconnect();
        if (!stage.classList.contains('dag-on')) stage.classList.add('dag-on');
        const slotsValue = String(rail.slots);
        if (stage.style.getPropertyValue('--dag-slots') !== slotsValue)
            stage.style.setProperty('--dag-slots', slotsValue);
        // Columns: the stage grid gains one track per column and the
        // page widens past the 960px column (body `dag-wide`), to at
        // least the columns' minimum widths — the page then scrolls
        // horizontally, and the toolbar stays put (dag.css).
        const cols = tracks.length > 0;
        stage.classList.toggle('dag-cols', cols);
        document.body.classList.toggle('dag-wide', cols);
        if (cols) {
            const narrow = !!(phone && phone.matches);
            const pane = Math.max(240, view - PHONE_PAD);
            let min = narrow ? pane : MAIN_MIN;
            const gtc = [narrow ? pane + 'px' : 'minmax(' + MAIN_MIN + 'px, ' + MAIN_FR + 'fr)'];
            tracks.forEach(function (id) {
                const isStrip = modeOf(id) === 'strip';
                gtc.push(isStrip ? STRIP + 'px' : narrow ? pane + 'px' : 'minmax(' + COL_MIN + 'px, 1fr)');
                min += isStrip ? STRIP : narrow ? pane : COL_MIN;
            });
            stage.classList.toggle('dag-panes', narrow);
            const value = gtc.join(' ');
            if (stage.style.getPropertyValue('--dag-cols') !== value) stage.style.setProperty('--dag-cols', value);
            if (document.body.style.getPropertyValue('--dag-min') !== min + 'px')
                document.body.style.setProperty('--dag-min', min + 'px');
            setView(view);
        } else {
            stage.style.removeProperty('--dag-cols');
            stage.classList.remove('dag-panes');
            document.body.style.removeProperty('--dag-min');
        }
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
            const value = String(item.row + ROW0);
            if (rowWritten.get(item.el) !== value) {
                item.el.style.gridRow = value;
                rowWritten.set(item.el, value);
            }
            // With columns every item needs its track: auto-placement
            // would put a definite-row item after the last placed one.
            const col = colIndex.get(item.key);
            const column = col ? String(col) : cols ? '1' : '';
            if ((colWritten.get(item.el) || '') !== column) {
                item.el.style.gridColumn = column;
                colWritten.set(item.el, column);
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
        const transcript = document.getElementById('transcript');
        if (transcript) syncChrome(transcript, tracks, colours, packed.rows, running);
        model.spawnCards.forEach(function (item) {
            ensureControls(item, colours, memo, overflow, running, acks);
        });
        markToolbar();
        if (observer && observed) observe(observed);
        const t3 = performance.now();

        layout = {
            model: model,
            rail: rail,
            packed: packed,
            sequence: sequence,
            newest: newest,
            activity: activity,
            running: running,
            columns: columnLanes(sequence, tracks, colours, model, running)
        };
        draw();
        const t4 = performance.now();
        lastTiming = {
            total: t4 - t0,
            model: t1 - t0,
            order: t2 - t1,
            write: t3 - t2,
            draw: t4 - t3,
            items: model.items.length,
            rows: packed.rows,
            lanes: lanes.size,
            open: open.length,
            columns: tracks.length
        };
        if (DEBUG) console.info('[dag] relayout ' + lastTiming.total.toFixed(1) + 'ms', lastTiming);
    }
    // Column lanes (drawn by draw()): per column or strip, its
    // colour, its shown items in row order (a strip: the row range of
    // its hidden ones), its merge row and whether it runs.
    function columnLanes(sequence, tracks, colours, model, running) {
        if (!tracks.length) return [];
        const byKey = new Map();
        const out = tracks.map(function (key) {
            const col = {
                key: key,
                colour: colours.get(key) || 'a',
                strip: modeOf(key) === 'strip',
                running: running.has(key),
                merge: model.merges.get(key) || null,
                items: [],
                first: -1,
                last: -1
            };
            byKey.set(key, col);
            return col;
        });
        sequence.forEach(function (item) {
            const col = byKey.get(item.key);
            if (!col) return;
            if (col.first < 0) col.first = item.row;
            col.last = item.row;
            if (!item.hidden) col.items.push(item);
        });
        return out;
    }
    // The viewport's width without its scrollbar: the sticky toolbar
    // keeps to it while a wide (columns) page scrolls sideways.
    function setView(width) {
        const view = (width || document.documentElement.clientWidth) + 'px';
        if (document.body.style.getPropertyValue('--dag-view') !== view)
            document.body.style.setProperty('--dag-view', view);
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
        if (stage.classList.contains('dag-cols')) setView();
        const transcript = document.getElementById('transcript');
        if (!transcript) return;
        const stageBox = stage.getBoundingClientRect();
        const transcriptBox = transcript.getBoundingClientRect();
        const style = getComputedStyle(stage);
        const step = parseFloat(style.getPropertyValue('--dag-step')) || 16;
        const x0 = parseFloat(style.getPropertyValue('--dag-x0')) || 9;
        const gut = phone && phone.matches ? 0 : parseFloat(style.getPropertyValue('--gut')) || 72;
        const base = transcriptBox.left - stageBox.left + gut + x0;
        const slotOf = layout.rail.slotOf;
        function x(slot) {
            return base + step * slot;
        }
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
        function round(v) {
            return Math.round(v * 10) / 10;
        }
        const paths = [];
        function path(lane, part, colour, d, dashed, extra) {
            paths.push(
                "<path class='dag-lc-" +
                    colour +
                    (dashed ? ' dag-dash' : '') +
                    (extra ? ' ' + extra : '') +
                    "' data-lane='" +
                    lane.replace(/'/g, '') +
                    "' data-part='" +
                    part +
                    "' d='" +
                    d +
                    "'></path>"
            );
        }
        // The bottom of the newest shown row: where a running lane
        // ends (it carries on beside everything that happens).
        let newestBottom = null;
        function lastShownBottom() {
            if (newestBottom !== null) return newestBottom;
            newestBottom = NaN;
            for (let i = layout.sequence.length - 1; i >= 0; i--) {
                const item = layout.sequence[i];
                if (!visible(item)) continue;
                newestBottom = item.el.getBoundingClientRect().bottom - stageBox.top;
                break;
            }
            return newestBottom;
        }
        layout.rail.plans.forEach(function (plan) {
            const xs = x(slotOf.get(plan.lane.parent) || 0);
            const xk = x(plan.slot);
            // First and last shown rows of the lane only.
            const seen = [];
            for (let i = 0; i < plan.seq.length; i++) {
                if (visible(plan.seq[i])) {
                    seen.push(plan.seq[i]);
                    break;
                }
            }
            for (let i = plan.seq.length - 1; i >= 0 && seen.length; i--) {
                if (visible(plan.seq[i])) {
                    seen.push(plan.seq[i]);
                    break;
                }
            }
            const spawnShown = visible(plan.spawn);
            if (!spawnShown && !(plan.open && seen.length)) return;
            const ys = spawnShown ? rowY(plan.spawn) : rowY(seen[0]);
            if (plan.running) {
                // Running (P7b): no merge yet and not a fork's stub —
                // the lane carries on to the bottom of the newest row
                // and ends in an open marker; when its result arrives
                // the next relayout draws the ordinary merge.
                const r = RADIUS;
                let bottom = lastShownBottom() - 2 * END_R;
                if (plan.open && seen.length) bottom = Math.max(bottom, rowY(seen[seen.length - 1]));
                const tail = (spawnShown ? ys + r : ys) + 2 * r;
                if (!(bottom >= tail)) bottom = tail;
                let top = ys;
                if (spawnShown) {
                    const dir = xk >= xs ? 1 : -1;
                    path(
                        plan.id,
                        'fork',
                        plan.colour,
                        'M' +
                            round(xs) +
                            ' ' +
                            round(ys) +
                            'H' +
                            round(xk - dir * r) +
                            'A' +
                            r +
                            ' ' +
                            r +
                            ' 0 0 ' +
                            (dir > 0 ? 1 : 0) +
                            ' ' +
                            round(xk) +
                            ' ' +
                            round(ys + r),
                        false
                    );
                    top = ys + r;
                }
                path(
                    plan.id,
                    'lane',
                    plan.colour,
                    'M' + round(xk) + ' ' + round(top) + 'V' + round(bottom - END_R),
                    !plan.open,
                    'dag-running'
                );
                path(
                    plan.id,
                    'end',
                    plan.colour,
                    'M' +
                        round(xk - END_R) +
                        ' ' +
                        round(bottom) +
                        'a' +
                        END_R +
                        ' ' +
                        END_R +
                        ' 0 1 0 ' +
                        2 * END_R +
                        ' 0' +
                        'a' +
                        END_R +
                        ' ' +
                        END_R +
                        ' 0 1 0 ' +
                        -2 * END_R +
                        ' 0',
                    false,
                    'dag-end'
                );
                return;
            }
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
                path(
                    plan.id,
                    'stub',
                    plan.colour,
                    'M' +
                        round(xs) +
                        ' ' +
                        round(ys) +
                        'H' +
                        round(xk - dir * r) +
                        'A' +
                        r +
                        ' ' +
                        r +
                        ' 0 0 ' +
                        (dir > 0 ? 1 : 0) +
                        ' ' +
                        round(xk) +
                        ' ' +
                        round(ys + r) +
                        'V' +
                        round(ys + STUB),
                    false
                );
                return;
            }
            const r = Math.min(RADIUS, (ye - ys) / 2);
            let top = ys;
            if (spawnShown) {
                path(
                    plan.id,
                    'fork',
                    plan.colour,
                    'M' +
                        round(xs) +
                        ' ' +
                        round(ys) +
                        'H' +
                        round(xk - dir * r) +
                        'A' +
                        r +
                        ' ' +
                        r +
                        ' 0 0 ' +
                        (dir > 0 ? 1 : 0) +
                        ' ' +
                        round(xk) +
                        ' ' +
                        round(ys + r),
                    false
                );
                top = ys + r;
            }
            const bottom = mergeShown ? ye - r : ye;
            if (bottom > top) {
                path(
                    plan.id,
                    'lane',
                    plan.colour,
                    'M' + round(xk) + ' ' + round(top) + 'V' + round(bottom),
                    !plan.open
                );
            }
            if (mergeShown) {
                const xm = x(slotOf.get(plan.merge.lane) || 0);
                const dm = xk >= xm ? 1 : -1;
                path(
                    plan.id,
                    'merge',
                    plan.colour,
                    'M' +
                        round(xk) +
                        ' ' +
                        round(ye - r) +
                        'A' +
                        r +
                        ' ' +
                        r +
                        ' 0 0 ' +
                        (dm > 0 ? 1 : 0) +
                        ' ' +
                        round(xk - dm * r) +
                        ' ' +
                        round(ye) +
                        'H' +
                        round(xm),
                    false
                );
            }
        });
        const fades = drawColumns(transcript, stageBox, style, rowY, round, path, lastShownBottom);
        // Measure without the previous drawing: an absolute SVG taller
        // than the stage counts in its scrollHeight, so a rail drawn
        // for a taller layout (a lane in a column) would never shrink
        // and would leave empty scroll space under the transcript.
        railHost.textContent = '';
        const width = Math.ceil(stage.clientWidth);
        const height = Math.ceil(stage.scrollHeight);
        // One fading stroke per lane colour a pointer uses (the
        // colour is the gradient's own --lc, so it follows the scheme).
        let defs = '';
        fades.forEach(function (colour) {
            defs +=
                "<linearGradient id='dag-fade-" +
                colour +
                "' class='dag-lc-" +
                colour +
                "' x1='1' y1='0' x2='0' y2='0'>" +
                "<stop offset='0'></stop><stop offset='0.4'></stop><stop offset='1'></stop></linearGradient>";
        });
        railHost.innerHTML = paths.length
            ? "<svg xmlns='http://www.w3.org/2000/svg' width='" +
              width +
              "' height='" +
              height +
              "' viewBox='0 0 " +
              width +
              ' ' +
              height +
              "'>" +
              (defs ? '<defs>' + defs + '</defs>' : '') +
              paths.join('') +
              '</svg>'
            : '';
    }

    // Column lanes: in Columns mode each column (or strip) draws its
    // lane where the main line has its rail — between the column
    // cards' gutter and content (`--dag-col-*`, dag.css), whose dots
    // sit on it — over the lane's active span only: from its first
    // shown row to its last, or down to its merge row (a sync result,
    // an async notification, a workflow agent's row). No join is
    // drawn across to the parent column: a short curve at the top
    // bends left towards where the branch came from and fades out
    // (`col-in`), and a merging lane ends in a matching one
    // (`col-out`); a fork never merges, and a running lane carries on
    // to the newest row and ends in the open marker, as on the rail.
    // A strip (its cards hidden) draws the line alone over the rows
    // its lane spans, at its left edge. Returns the colours whose
    // fade the pointers use.
    function drawColumns(transcript, stageBox, style, rowY, round, path, lastShownBottom) {
        const fades = new Set();
        const columns = layout.columns || [];
        if (!columns.length) return fades;
        const pad = parseFloat(style.getPropertyValue('--dag-col-pad')) || 0;
        const gut = parseFloat(style.getPropertyValue('--dag-col-gut')) || 0;
        const rail = parseFloat(style.getPropertyValue('--dag-col-rail')) || 16;
        const offset = pad + gut + rail / 2;
        const r = RADIUS;
        const sequence = layout.sequence;
        const lastRow = sequence.length ? sequence[sequence.length - 1].row : -1;
        // The grid's row heights, read once per draw and only for a
        // strip: the resolved grid-template-rows lists every track,
        // implicit ones included, and the layout is already clean.
        let heights = null;
        function rowHeights() {
            if (!heights) heights = getComputedStyle(transcript).gridTemplateRows.split(' ').map(parseFloat);
            return heights;
        }
        // The dot y of the first shown item in a row (NaN: none).
        function shownIn(row) {
            let lo = 0;
            let hi = sequence.length;
            while (lo < hi) {
                const mid = (lo + hi) >> 1;
                if (sequence[mid].row < row) lo = mid + 1;
                else hi = mid;
            }
            for (let i = lo; i < sequence.length && sequence[i].row === row; i++) {
                if (visible(sequence[i])) return rowY(sequence[i]);
            }
            return NaN;
        }
        // A strip's rows are empty: the dot y of the nearest shown
        // item at or after (dir 1) / at or before (dir -1) a row. An
        // empty row has no height, so the walk passes over those on
        // the measured heights alone, however many there are (no
        // per-item visibility read): only a row with a height is
        // looked into (rows are packed in sequence order).
        function nearRowY(row, dir) {
            const sizes = rowHeights();
            for (let k = row; k >= 0 && k <= lastRow; k += dir) {
                if (sizes[k + ROW0 - 1] === 0) continue;
                const y = shownIn(k);
                if (!isNaN(y)) return y;
            }
            return NaN;
        }
        function pointer(id, part, colour, d) {
            fades.add(colour);
            path(id, part, colour, d, false, 'dag-fade');
        }
        columns.forEach(function (col) {
            const el = chrome.get(col.key);
            if (!el || !el.isConnected) return;
            const xk = el.getBoundingClientRect().left - stageBox.left + (col.strip ? STRIP_X : offset);
            let ys = NaN;
            let ye = NaN;
            let lastRow = col.last;
            if (col.strip) {
                if (col.first < 0) return;
                ys = nearRowY(col.first, 1);
                // Its rows are the last shown (a session that ended
                // mid-branch, or a running one): it starts level with
                // the shown row they follow.
                if (isNaN(ys)) ys = nearRowY(col.first, -1);
                ye = nearRowY(col.last, -1);
            } else {
                let first = null;
                let last = null;
                for (let i = 0; i < col.items.length && !first; i++) if (visible(col.items[i])) first = col.items[i];
                for (let i = col.items.length - 1; i >= 0 && first && !last; i--)
                    if (visible(col.items[i])) last = col.items[i];
                if (!first) return;
                ys = rowY(first);
                ye = rowY(last);
                lastRow = last.row;
            }
            if (isNaN(ys)) return;
            if (!(ye >= ys)) ye = ys;
            const id = col.key;
            const colour = col.colour;
            const lead = POINTER_LEAD;
            pointer(
                id,
                'col-in',
                colour,
                'M' +
                    round(xk) +
                    ' ' +
                    round(ys) +
                    'V' +
                    round(ys - lead) +
                    'A' +
                    r +
                    ' ' +
                    r +
                    ' 0 0 0 ' +
                    round(xk - r) +
                    ' ' +
                    round(ys - lead - r) +
                    'H' +
                    round(xk - r - POINTER_TAIL)
            );
            if (col.running) {
                // The open marker clears the last dot even when that
                // row is the newest (a short card).
                let bottom = lastShownBottom() - 2 * END_R;
                if (!(bottom >= ye + 2 * END_R + 4)) bottom = ye + 2 * END_R + 4;
                path(
                    id,
                    'col',
                    colour,
                    'M' + round(xk) + ' ' + round(ys) + 'V' + round(bottom - END_R),
                    false,
                    'dag-running'
                );
                path(
                    id,
                    'end',
                    colour,
                    'M' +
                        round(xk - END_R) +
                        ' ' +
                        round(bottom) +
                        'a' +
                        END_R +
                        ' ' +
                        END_R +
                        ' 0 1 0 ' +
                        2 * END_R +
                        ' 0' +
                        'a' +
                        END_R +
                        ' ' +
                        END_R +
                        ' 0 1 0 ' +
                        -2 * END_R +
                        ' 0',
                    false,
                    'dag-end'
                );
                return;
            }
            const merge = col.merge;
            const ym = merge && visible(merge) && merge.row > lastRow ? rowY(merge) : NaN;
            if (ym > ye) {
                // The merge row: the lane runs down to it and bends
                // back towards the parent column there.
                const rm = Math.min(r, Math.max(2, ym - ys));
                const split = Math.max(ys, ym - rm - lead);
                path(id, 'col', colour, 'M' + round(xk) + ' ' + round(ys) + 'V' + round(split), false);
                pointer(
                    id,
                    'col-out',
                    colour,
                    'M' +
                        round(xk) +
                        ' ' +
                        round(split) +
                        'V' +
                        round(ym - rm) +
                        'A' +
                        rm +
                        ' ' +
                        rm +
                        ' 0 0 1 ' +
                        round(xk - rm) +
                        ' ' +
                        round(ym) +
                        'H' +
                        round(xk - rm - POINTER_TAIL)
                );
                return;
            }
            path(id, 'col', colour, 'M' + round(xk) + ' ' + round(ys) + 'V' + round(ye), false);
        });
        return fades;
    }

    // ---- triggers ----------------------------------------------------
    const STRUCTURE = ['message-node', 'message', 'children', 'fork-point'];
    function isStructural(node) {
        if (!node || node.nodeType !== 1) return false;
        return STRUCTURE.some(function (cls) {
            return node.classList.contains(cls);
        });
    }
    const HIDING = /(?:^|\s)(?:filtered-hidden|search-hidden)(?=\s|$)/;
    function relevant(records) {
        for (let i = 0; i < records.length; i++) {
            const record = records[i];
            if (record.type === 'childList') {
                for (let j = 0; j < record.addedNodes.length; j++) if (isStructural(record.addedNodes[j])) return true;
                for (let j = 0; j < record.removedNodes.length; j++)
                    if (isStructural(record.removedNodes[j])) return true;
            } else if (record.attributeName === 'style') {
                if (record.target.classList && record.target.classList.contains('children')) return true;
            } else if (record.attributeName === 'class') {
                const target = record.target;
                if (
                    !target.classList ||
                    !(target.classList.contains('message') || target.classList.contains('fork-point'))
                )
                    continue;
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
            subtree: true,
            childList: true,
            attributes: true,
            attributeFilter: ['class', 'style'],
            attributeOldValue: true
        });
        if (resizer) {
            resizer.disconnect();
            resizer.observe(transcript);
        }
    }
    const resizer = window.ResizeObserver ? new ResizeObserver(scheduleDraw) : null;
    // Phone columns are sized to the viewport: a resize re-lays them.
    window.addEventListener('resize', function () {
        if (stage.classList.contains('dag-panes')) schedule();
        else scheduleDraw();
    });
    // A card a live update adds fades in from a few px below
    // (`live-new-in`, a transform): the rail measured it mid-way, and
    // a transform resizes nothing, so redraw once it has landed.
    document.addEventListener('animationend', function (event) {
        if (event.animationName === 'live-new-in') scheduleDraw();
    });
    if (phone) {
        const onPhone = function () {
            if (stage.classList.contains('dag-cols')) schedule();
            else scheduleDraw();
        };
        if (phone.addEventListener) phone.addEventListener('change', onPhone);
        else if (phone.addListener) phone.addListener(onPhone);
    }

    document.addEventListener('click', function (event) {
        const target = event.target;
        if (!target || !target.closest) return;
        const toggle = target.closest('.mn-bfold, .mn-bcol, .mn-bmore, .mn-back');
        if (toggle) {
            if (toggle.classList.contains('mn-bmore')) {
                const turn = toggle.getAttribute('data-turn');
                if (expanded.has(turn)) expanded.delete(turn);
                else expanded.add(turn);
                relayout();
                return;
            }
            const ctl = toggle.closest('[data-lane-ref]');
            const id = ctl ? ctl.getAttribute('data-lane-ref') : null;
            if (!id || !lanes.has(id)) return;
            if (toggle.classList.contains('mn-back')) {
                if (acksShown.has(id)) acksShown.delete(id);
                else acksShown.add(id);
            } else if (toggle.classList.contains('mn-bcol')) {
                if (inColumn(modeOf(id))) select(id);
                else column(id);
            } else if (modeOf(id) !== 'folded' && parentOpen(id, new Map())) {
                fold(id);
            } else {
                select(id);
            }
            relayout();
            return;
        }
        const act = target.closest('.dag-chrome [data-col-act]');
        if (act) {
            const id = act.closest('.dag-chrome').getAttribute('data-col');
            if (!id || !lanes.has(id)) return;
            const what = act.getAttribute('data-col-act');
            if (what === 'interleave') select(id);
            else if (what === 'collapse') strip(id);
            else column(id);
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
    window.addEventListener('hashchange', function () {
        revealHash(true);
    });

    // A live update (patch or swap) relayouts before the browser
    // gets to lay out or paint the new markup — a swapped-in
    // #transcript has none of the engine's rows or classes, and an
    // intermediate layout with every lane unfolded would move the
    // reader's scroll position (P7b). Rehydrate is called once per
    // changed element, so the relayout is coalesced to one
    // microtask after the update's synchronous work.
    let rehydrateQueued = false;
    if (window.claudeLogOnRehydrate) {
        window.claudeLogOnRehydrate(function (scope) {
            if (scope && scope.id === 'transcript') observe(scope);
            if (rehydrateQueued) return;
            rehydrateQueued = true;
            const run = function () {
                rehydrateQueued = false;
                if (document.hidden) {
                    hiddenDirty = true;
                    return;
                }
                relayout();
            };
            if (window.queueMicrotask) window.queueMicrotask(run);
            else Promise.resolve().then(run);
        });
    }
    // While a lane runs, its "quiet …" label is brought up to date
    // (and a session silent for RUNNING_MAX_QUIET_MS stops showing
    // one), with or without an update.
    setInterval(function () {
        if (!started || !layout || document.hidden || !window.claudeLogLiveUpdate) return;
        const next = runningLanes(layout.newest, layout.activity);
        if (next.size || !sameSet(next, layout.running)) relayout();
    }, RUNNING_RECHECK_MS);

    function start() {
        const transcript = document.getElementById('transcript');
        if (!transcript) return;
        lanes = readLanes();
        applyGlobal(globalMode);
        lanes.forEach(function (lane, id) {
            if (!modes.has(id)) modes.set(id, 'folded');
        });
        started = true;
        pendingReveals.splice(0).forEach(function (lane) {
            if (lanes.has(lane) && !isOpen(lane, new Map())) openForReveal(lane);
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
        relayout: function () {
            relayout();
            return lastTiming;
        },
        timing: function () {
            return lastTiming;
        },
        // 'folded' | 'interleaved' | 'column' | 'strip', as shown (a
        // lane inside a folded one reads 'folded').
        mode: function (id) {
            const memo = new Map();
            const mode = modeOf(id);
            if (!parentOpen(id, memo)) return 'folded';
            return mode;
        },
        // True while the lane is drawn as running (P7b).
        running: function (id) {
            return !!(layout && layout.running.has(id));
        },
        // How long a running lane has been quiet (ms), else null (P7c).
        quietMs: function (id) {
            return layout && layout.running.has(id) ? layout.running.get(id) : null;
        },
        runningMaxQuietMs: RUNNING_MAX_QUIET_MS
    };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
