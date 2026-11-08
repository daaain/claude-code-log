// The recent sessions page (recent.html): draws the rows from a payload
// (see claude_code_log/recent.py for its shape) and keeps them current.
//
// The page is opened from disk, where nothing can be fetched; a <script>
// tag can still be added, though, so the data lives in a sibling
// recent-data.js that calls window.__cclRecent(payload). Every 15 s (while
// the tab is visible) the script is added again with a fresh ?t= so no
// cache serves an old copy, and removed once it has run. The rows are
// updated in place — expanded rows, the filter, grouping, scroll position,
// focus and a text selection all survive a refresh — and every 30 s the
// times are redrawn against the clock.
//
// Every string in the payload comes from a transcript: it reaches the page
// through textContent and setAttribute only, never as markup. The one
// exception is the payload's `html` fields — the last prompt and message
// rendered as Markdown by recent.py with the session pages' pipeline, which
// escapes raw HTML in the transcript — and those go in through
// renderedMarkdown() alone.
(function () {
    'use strict';

    const DATA_SRC = 'recent-data.js';
    const RELOAD_MS = 15000;
    const TICK_MS = 30000;
    const STALE_MS = 2 * 60000;
    const BEFORE_MS = 4 * 3600000; // the strip shows 4 h back...
    const AFTER_MS = 3600000; // ...and 1 h ahead
    const SPAN_MS = BEFORE_MS + AFTER_MS;
    const NOW_PCT = (BEFORE_MS / SPAN_MS) * 100;
    const MARK_GAP_PX = 16;
    const CACHE_RISK_MIN = 45;
    const STORE_KEY = 'claude-code-log:recent';
    const SVG_NS = 'http://www.w3.org/2000/svg';

    // The block's words for each state. The payload sends the first four
    // (from the transcript); 'waiting' is the page's own: a finished turn
    // with background work still running or a wake-up still to come.
    const STATES = {
        asked: 'asked you',
        error: 'error',
        done: 'finished',
        busy: 'mid-turn',
        waiting: 'background'
    };
    const TRANSCRIPT_STATES = ['asked', 'error', 'done', 'busy'];
    const BG_KINDS = { bash: 'Bash', agent: 'Agent', monitor: 'Monitor' };
    const BG_ENDS = ['completed', 'failed', 'stopped', 'exited'];
    // How an end reads, where the status's own word won't do.
    const BG_END_TEXT = { exited: 'ended with the session' };
    const MAX_LANES = 3; // background lanes on the strip; the rest fold into the last
    const LANE_GAP_PCT = 0.6; // room between two items sharing a lane
    const MAX_BG_LISTED = 12; // background items in an open row's list
    const WHO = {
        claude: 'Claude',
        question: 'Claude asked',
        plan: 'Plan to approve',
        tool: 'Tool call',
        error: 'Error'
    };
    const FILTERS = {
        all: function () {
            return true;
        },
        move: function (s, ctx) {
            const st = effectiveState(s, ctx);
            return st === 'asked' || st === 'error' || st === 'done';
        },
        urgent: function (s) {
            return s.state === 'asked' || s.state === 'error';
        },
        cache: function (s, ctx) {
            return cacheLeft(s, ctx) !== null;
        }
    };
    const ICON = {
        prompt: 'M3 4.5 7 8l-4 3.5M8.5 12H13',
        asst: 'M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z',
        ask: 'M6.3 6.2a1.8 1.8 0 1 1 2.5 1.6c-.5.2-.8.6-.8 1.1v.4M8 11.6v.2',
        err: 'M8 4v4.5M8 11.3v.2',
        branch: 'M4 5v6M12 7.5c0 2.5-3 2.5-6.5 4',
        clock: 'M8 4.5V8l2.5 1.5',
        copy: 'M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5',
        chevron: 'M6 3.5 10.5 8 6 12.5',
        // a bell: background work reported back
        task: 'M4.5 11V7.5a3.5 3.5 0 0 1 7 0V11l1 1.5h-9zM6.8 14.2h2.4',
        // an alarm clock: a wake-up
        wake: 'M8 4.5a4.5 4.5 0 1 0 0 9a4.5 4.5 0 1 0 0-9M8 6.8V9l1.4 1M2.8 4.3 4.6 2.8M13.2 4.3l-1.8-1.5',
        x: 'M4.5 4.5l7 7M11.5 4.5l-7 7'
    };

    // ---- clock -----------------------------------------------------
    // ?now=<ms> freezes the clock (the tests use it); otherwise it's live.
    const frozenNow = (function () {
        const match = /[?&]now=(\d+)/.exec(window.location.search);
        return match ? Number(match[1]) : null;
    })();
    function clock() {
        return frozenNow !== null ? frozenNow : Date.now();
    }

    function pad(n) {
        return (n < 10 ? '0' : '') + n;
    }
    function hhmm(ms) {
        const d = new Date(ms);
        return pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    function weekday(ms) {
        return new Date(ms).toLocaleDateString('en-GB', { weekday: 'short' });
    }
    function sameDay(a, b) {
        return new Date(a).toDateString() === new Date(b).toDateString();
    }
    // A time of day, with the weekday when it isn't today.
    function when(ms, now) {
        return sameDay(ms, now) ? hhmm(ms) : weekday(ms) + ' ' + hhmm(ms);
    }
    function minutesSince(ms, now) {
        return Math.max(0, Math.floor((now - ms) / 60000));
    }
    // The status block's big number and its unit.
    function since(ms, now) {
        const min = minutesSince(ms, now);
        if (min < 60) return { n: String(min), u: 'min' };
        if (min < 24 * 60) return { n: Math.floor(min / 60) + ':' + pad(min % 60), u: 'h' };
        return { n: hhmm(ms), u: weekday(ms) };
    }
    function ago(ms, now) {
        const min = minutesSince(ms, now);
        if (min < 60) return min + ' min ago';
        if (min < 24 * 60) return Math.floor(min / 60) + ' h ' + (min % 60) + ' min ago';
        return weekday(ms) + ' ' + hhmm(ms);
    }
    function plural(n, word) {
        return n + ' ' + word + (n === 1 ? '' : 's');
    }

    // Minutes of prompt cache left while it's at risk (45 min or more
    // since the last update, not yet expired), else null.
    function cacheLeft(s, ctx) {
        const elapsed = minutesSince(s.lastAt, ctx.now);
        if (elapsed < CACHE_RISK_MIN || elapsed >= ctx.ttl) return null;
        return Math.max(1, Math.ceil((s.lastAt + ctx.ttl * 60000 - ctx.now) / 60000));
    }

    // ---- background work -------------------------------------------
    // The transcript can't say whether work is still running, only that
    // nothing reported its end. Such an item counts as running until its
    // own expiry (a monitor's timeout) or bgCap after the session's last
    // entry, whichever is first, judged against this page's clock so an
    // old data file still ages; past that it has 'no end recorded'.
    function bgLimit(item, s, ctx) {
        const cap = s.lastAt + ctx.bgCap * 60000;
        return item.expires !== null ? Math.min(item.expires, cap) : cap;
    }
    function bgStatus(item, s, ctx) {
        if (item.end !== null) return item.status;
        return ctx.now < bgLimit(item, s, ctx) ? 'running' : 'lost';
    }
    // Where an item's line stops: its end, else now or its limit.
    function bgEnd(item, s, ctx) {
        return item.end !== null ? item.end : Math.min(bgLimit(item, s, ctx), ctx.now);
    }
    function bgRunning(s, ctx) {
        return s.bg.filter(function (item) {
            return bgStatus(item, s, ctx) === 'running';
        }).length;
    }
    // Wake-ups still to come (a missed one, past its grace, is dropped).
    function pendingWakes(s, ctx) {
        return s.wakes.filter(function (w) {
            return ctx.now < w.at + ctx.wakeGrace * 60000;
        });
    }
    // The transcript state, or 'waiting' for a finished turn that is
    // waiting on its own background work. A question, an error or a turn
    // under way stays what it is.
    function effectiveState(s, ctx) {
        if (s.state !== 'done') return s.state;
        return bgRunning(s, ctx) > 0 || pendingWakes(s, ctx).length > 0 ? 'waiting' : 'done';
    }
    function bgName(item) {
        return BG_KINDS[item.kind] + (item.label ? ' “' + item.label + '”' : '');
    }
    // "14:02 – running", "14:02 – no end recorded", "14:02–14:20 · failed",
    // "14:02–14:20 · ended with the session" (its process exited)
    function bgSpan(item, status) {
        if (status === 'running') return hhmm(item.start) + ' – running';
        if (status === 'lost') return hhmm(item.start) + ' – no end recorded';
        return hhmm(item.start) + '–' + hhmm(item.end) + ' · ' + (BG_END_TEXT[status] || status);
    }

    // ---- payload ---------------------------------------------------
    function num(value) {
        const n = Number(value);
        return isFinite(n) ? n : 0;
    }
    function str(value) {
        return value === null || value === undefined ? '' : String(value);
    }
    // A link the payload gives: relative paths only (no scheme, no //host).
    function safeHref(value) {
        const href = str(value);
        if (!href || /^\s*([a-z][a-z0-9+.-]*:|\/\/)/i.test(href)) return null;
        return href;
    }
    function cleanSession(raw) {
        const turns = Array.isArray(raw.turns) ? raw.turns : [];
        const last = raw.last && typeof raw.last === 'object' ? raw.last : null;
        const prompt = raw.lastPrompt && typeof raw.lastPrompt === 'object' ? raw.lastPrompt : null;
        return {
            id: str(raw.id),
            short: str(raw.short) || str(raw.id).slice(0, 8),
            project: str(raw.project),
            projectDir: str(raw.projectDir) || str(raw.project),
            worktree: raw.worktree ? str(raw.worktree) : null,
            branch: raw.branch ? str(raw.branch) : null,
            title: str(raw.title) || str(raw.short),
            href: safeHref(raw.href),
            lastAt: num(raw.lastAt),
            state: TRANSCRIPT_STATES.indexOf(raw.state) >= 0 ? raw.state : 'done',
            messages: num(raw.messages),
            turnCount: num(raw.turnCount),
            toolCount: num(raw.toolCount),
            turns: turns
                .filter(function (t) {
                    return Array.isArray(t) && t.length >= 2;
                })
                .map(function (t) {
                    // [start, end, how it ended, what started it, how it
                    // ended (snippets), who started it: you | task | wake]
                    const origin = t[5] === 'task' || t[5] === 'wake' ? t[5] : 'you';
                    return [num(t[0]), num(t[1]), str(t[2]) || 'asst', str(t[3]), str(t[4]), origin];
                }),
            // [start, end|null, kind, status|null, label, expiresAt|null]
            bg: (Array.isArray(raw.bg) ? raw.bg : [])
                .filter(function (b) {
                    return Array.isArray(b) && b.length >= 3;
                })
                .map(function (b) {
                    const end = b[1] === null || b[1] === undefined ? null : num(b[1]);
                    return {
                        start: num(b[0]),
                        end: end,
                        kind: BG_KINDS[b[2]] ? b[2] : 'bash',
                        status: end !== null && BG_ENDS.indexOf(b[3]) >= 0 ? b[3] : end !== null ? 'completed' : null,
                        label: str(b[4]),
                        expires: b[5] === null || b[5] === undefined ? null : num(b[5])
                    };
                })
                .sort(function (a, b) {
                    return a.start - b.start;
                }),
            // [at, label]: wake-ups not yet fired
            wakes: (Array.isArray(raw.wakes) ? raw.wakes : [])
                .filter(function (w) {
                    return Array.isArray(w) && w.length >= 1;
                })
                .map(function (w) {
                    return { at: num(w[0]), label: str(w[1]) };
                }),
            earlierTurns: num(raw.earlierTurns),
            tools: (Array.isArray(raw.tools) ? raw.tools : []).map(num),
            lastPrompt: prompt ? { at: num(prompt.at), html: str(prompt.html) } : null,
            last: last
                ? {
                      at: num(last.at),
                      who: WHO[last.who] ? last.who : 'claude',
                      text: str(last.text),
                      html: str(last.html)
                  }
                : null
        };
    }
    function cleanPayload(raw) {
        if (!raw || typeof raw !== 'object' || !Array.isArray(raw.sessions)) return null;
        const sessions = raw.sessions
            .filter(function (s) {
                return s && typeof s === 'object' && s.id;
            })
            .map(cleanSession);
        sessions.sort(function (a, b) {
            return b.lastAt - a.lastAt;
        });
        return {
            generatedAt: num(raw.generatedAt),
            windowDays: num(raw.windowDays) || 3,
            window: str(raw.window),
            ttl: num(raw.cacheTtlMinutes) || 60,
            bgCap: num(raw.bgCapMinutes) || 60,
            wakeGrace: num(raw.wakeGraceMinutes) || 5,
            sessions: sessions
        };
    }

    // ---- state -----------------------------------------------------
    const state = { payload: null, filter: 'all', group: false, expanded: new Set() };
    (function restore() {
        try {
            const saved = JSON.parse(window.sessionStorage.getItem(STORE_KEY) || 'null');
            if (!saved) return;
            if (Array.isArray(saved.expanded)) state.expanded = new Set(saved.expanded.map(String));
            if (Object.prototype.hasOwnProperty.call(FILTERS, saved.filter)) state.filter = saved.filter;
            state.group = saved.group === true;
        } catch (err) {
            /* storage unavailable or unreadable: start fresh */
        }
    })();
    function save() {
        try {
            window.sessionStorage.setItem(
                STORE_KEY,
                JSON.stringify({ expanded: Array.from(state.expanded), filter: state.filter, group: state.group })
            );
        } catch (err) {
            /* storage unavailable: the choice lasts this page */
        }
    }

    // ---- DOM helpers -----------------------------------------------
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }
    // Assigning the same text still replaces the text node (and drops a
    // selection inside it), so only write what changed.
    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }
    function setAttr(node, name, value) {
        if (node.getAttribute(name) !== value) node.setAttribute(name, value);
    }
    function icon(paths, size, extra) {
        const svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('width', String(size));
        svg.setAttribute('height', String(size));
        svg.setAttribute('viewBox', '0 0 16 16');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.6');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        (extra || []).forEach(function (shape) {
            const node = document.createElementNS(SVG_NS, shape[0]);
            Object.keys(shape[1]).forEach(function (key) {
                node.setAttribute(key, shape[1][key]);
            });
            svg.appendChild(node);
        });
        paths.forEach(function (d) {
            const path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }
    // The one place markup enters the page: a payload `html` field, which
    // recent.py rendered with the session pages' escaping Markdown pipeline
    // (never a transcript string itself; test_recent_page.py holds this).
    function renderedMarkdown(cls, html) {
        const node = el('div', 'rc-md ' + cls);
        node.innerHTML = html;
        return node;
    }
    function markerIcon(kind) {
        const svg = icon([ICON[kind]], 12);
        svg.setAttribute('stroke-width', '1.9');
        return svg;
    }
    // Put `nodes` under `parent` in order, moving only what is out of place
    // (a moved node loses a selection inside it) and dropping the rest.
    function syncChildren(parent, nodes) {
        nodes.forEach(function (node, i) {
            const at = parent.children[i];
            if (at !== node) parent.insertBefore(node, at || null);
        });
        while (parent.children.length > nodes.length) parent.removeChild(parent.lastElementChild);
    }

    // ---- page elements ---------------------------------------------
    const $ = function (id) {
        return document.getElementById(id);
    };
    const rowsEl = $('rc-rows');
    const emptyEl = $('rc-empty');
    const axisEl = $('rc-axis');
    const staleEl = $('rc-stale');
    const updatedEl = $('rc-updated');
    const groupEl = $('rc-group');
    const filterEls = Array.prototype.slice.call(document.querySelectorAll('[data-filter]'));

    const rows = new Map(); // session id -> row record
    const groups = new Map(); // project dir -> group record
    let lastCtx = null; // the last render's clock and strip window

    // ---- strip geometry --------------------------------------------
    function stripCtx(now) {
        const start = now - BEFORE_MS;
        return {
            start: start,
            end: now + AFTER_MS,
            pct: function (ms) {
                return ((ms - start) / SPAN_MS) * 100;
            }
        };
    }
    // Local full hours inside the strip's window.
    function hours(win) {
        const out = [];
        const d = new Date(win.start);
        d.setMinutes(0, 0, 0);
        d.setHours(d.getHours() + 1);
        while (d.getTime() < win.end) {
            out.push(d.getTime());
            d.setHours(d.getHours() + 1);
        }
        return out;
    }
    function span(cls, leftPct, widthPct) {
        const node = el('span', cls);
        node.style.left = leftPct + '%';
        if (widthPct !== undefined) node.style.width = widthPct + '%';
        return node;
    }
    function drawAxis(ctx) {
        const nodes = [];
        hours(ctx.win).forEach(function (ms) {
            const pct = ctx.win.pct(ms);
            if (pct < 3 || pct > 97 || Math.abs(pct - NOW_PCT) < 5) return;
            const label = span('', pct);
            label.textContent = pad(new Date(ms).getHours());
            nodes.push(label);
        });
        const now = span('rc-axis-now', NOW_PCT);
        now.textContent = 'NOW';
        nodes.push(now);
        axisEl.replaceChildren(...nodes);
    }

    // The strip's background, bars, ticks and labels; the markers are laid
    // out by layoutMarks once the strip has a width.
    function drawStrip(row, s, ctx) {
        const win = ctx.win;
        const strip = row.strip;
        const nodes = [span('rc-future', NOW_PCT)];
        hours(win).forEach(function (ms) {
            nodes.push(span('rc-grid', win.pct(ms)));
        });
        const shown = s.turns.filter(function (t) {
            return t[1] >= win.start && t[0] <= win.end;
        });
        const inWindow = s.lastAt >= win.start;
        const ttlMs = ctx.ttl * 60000;
        let label;
        if (inWindow) {
            if (s.lastAt + ttlMs > ctx.now) {
                const left = win.pct(s.lastAt);
                const band = span('rc-band', left, Math.min(100, win.pct(s.lastAt + ttlMs)) - left);
                if (cacheLeft(s, ctx) !== null) band.classList.add('rc-band-risk');
                nodes.push(band);
            }
            shown.forEach(function (t) {
                const left = win.pct(Math.max(t[0], win.start));
                nodes.push(span('rc-bar', left, win.pct(Math.min(t[1], win.end)) - left));
            });
            s.tools.forEach(function (ms) {
                if (ms >= win.start && ms <= win.end) nodes.push(span('rc-tick', win.pct(ms)));
            });
            drawLanes(s, ctx).forEach(function (node) {
                nodes.push(node);
            });
            const earlier =
                s.earlierTurns +
                s.turns.filter(function (t) {
                    return t[1] < win.start;
                }).length;
            if (earlier > 0) {
                nodes.push(el('span', 'rc-strip-note rc-earlier', '◂ +' + plural(earlier, 'earlier turn')));
            }
            const lastTurn = s.turns.length ? s.turns[s.turns.length - 1] : null;
            const running = bgRunning(s, ctx);
            const wakes = pendingWakes(s, ctx);
            label =
                plural(s.turnCount, 'turn') +
                (lastTurn ? ', the last ending ' + hhmm(lastTurn[1]) : '') +
                (s.lastAt + ttlMs > ctx.now ? '; cache warm until ' + hhmm(s.lastAt + ttlMs) : '') +
                (running ? '; ' + plural(running, 'background task') + ' running' : '') +
                (wakes.length ? '; wakes up at ' + hhmm(wakes[0].at) : '');
        } else {
            nodes.push(
                el(
                    'span',
                    'rc-strip-note rc-outside',
                    '← ' +
                        ago(s.lastAt, ctx.now) +
                        ' · ' +
                        plural(s.turnCount, 'turn') +
                        ' · ' +
                        plural(s.messages, 'message')
                )
            );
            label = 'Last update ' + ago(s.lastAt, ctx.now);
        }
        row.marks = el('span', 'rc-marks');
        nodes.push(row.marks);
        nodes.push(span('rc-now', NOW_PCT));
        strip.replaceChildren(...nodes);
        strip.setAttribute('aria-label', label);
        row.events = inWindow ? markerEvents(s, shown, ctx) : [];
        row.markWidth = -1;
    }
    // The background lanes: each item a thin line on the first lane free
    // at its start (so work done one after another shares a lane), at
    // most MAX_LANES of them; when more are needed, the last lane carries
    // the rest as the union of their spans, with a "+N more" note.
    function drawLanes(s, ctx) {
        const win = ctx.win;
        const laneEnds = [];
        const placed = [];
        s.bg.forEach(function (item) {
            const status = bgStatus(item, s, ctx);
            const end = bgEnd(item, s, ctx);
            if (end < win.start || item.start > win.end) return;
            const left = win.pct(Math.max(item.start, win.start));
            const right = win.pct(Math.min(end, win.end));
            let lane = laneEnds.findIndex(function (laneEnd) {
                return laneEnd + LANE_GAP_PCT <= left;
            });
            if (lane < 0) {
                lane = laneEnds.length;
                laneEnds.push(0);
            }
            laneEnds[lane] = right;
            placed.push({ item: item, status: status, left: left, right: right, lane: lane, ends: end <= win.end });
        });
        const folded = laneEnds.length > MAX_LANES;
        const nodes = [];
        const overflow = [];
        placed.forEach(function (p) {
            if (folded && p.lane >= MAX_LANES - 1) {
                overflow.push(p);
                return;
            }
            const line = span('rc-bg rc-bg-' + p.status, p.left, p.right - p.left);
            line.dataset.lane = String(p.lane);
            line.title = bgName(p.item) + ' · ' + bgSpan(p.item, p.status);
            nodes.push(line);
            if (p.ends && BG_ENDS.indexOf(p.status) >= 0) {
                const end = span('rc-bg-end rc-bg-end-' + p.status, p.right);
                end.dataset.lane = String(p.lane);
                if (p.status === 'failed') end.appendChild(icon([ICON.x], 11));
                nodes.push(end);
            }
        });
        if (overflow.length) {
            const lane = String(MAX_LANES - 1);
            const tip =
                '+' +
                overflow.length +
                ' more: ' +
                overflow
                    .slice(0, 8)
                    .map(function (p) {
                        return bgName(p.item);
                    })
                    .join(', ') +
                (overflow.length > 8 ? ', …' : '');
            overflow.sort(function (a, b) {
                return a.left - b.left;
            });
            let run = null;
            overflow.concat([null]).forEach(function (p) {
                if (p && run && p.left <= run.right + LANE_GAP_PCT) {
                    run.right = Math.max(run.right, p.right);
                    return;
                }
                if (run) {
                    const line = span('rc-bg rc-bg-more', run.left, run.right - run.left);
                    line.dataset.lane = lane;
                    line.title = tip;
                    nodes.push(line);
                }
                run = p ? { left: p.left, right: p.right } : null;
            });
            const note = el('span', 'rc-strip-note rc-bg-more-n', '+' + overflow.length + ' more');
            note.title = tip;
            nodes.push(note);
        }
        return nodes;
    }

    // What started a turn: a prompt you typed, background work reporting
    // back, or a wake-up.
    const ORIGIN = {
        you: { kind: 'prompt', tip: 'You' },
        task: { kind: 'task', tip: 'Background work reported' },
        wake: { kind: 'wake', tip: 'Woke up' }
    };
    function markerEvents(s, shown, ctx) {
        const win = ctx.win;
        const events = [];
        const final = s.turns[s.turns.length - 1];
        pendingWakes(s, ctx).forEach(function (w) {
            if (w.at < win.start) return;
            // Clamped to the strip's end; the tooltip has the time.
            events.push({
                at: Math.min(w.at, win.end),
                time: w.at,
                kind: 'wake',
                pending: true,
                keep: true,
                tip: 'Wakes up',
                snippet: w.label
            });
        });
        shown.forEach(function (t) {
            if (t[0] >= win.start) {
                const origin = ORIGIN[t[5]];
                events.push({ at: t[0], kind: origin.kind, keep: false, tip: origin.tip, snippet: t[3] });
            }
            const kind = t[2];
            if (kind === 'open' || t[1] < win.start || t[1] > win.end) return;
            const isLast = t === final;
            const tip =
                kind === 'ask'
                    ? 'Claude asked you'
                    : kind === 'err'
                      ? 'Error'
                      : isLast && s.state === 'done'
                        ? 'Claude finished'
                        : 'Claude replied';
            events.push({
                at: t[1],
                kind: kind === 'ask' || kind === 'err' ? kind : 'asst',
                keep: isLast || kind === 'ask' || kind === 'err',
                tip: tip,
                snippet: t[4]
            });
        });
        events.sort(function (a, b) {
            return a.at - b.at;
        });
        return events;
    }
    // Full markers where there is room; one closer than MARK_GAP_PX to the
    // previous full marker shrinks to a dot, unless it must stay full (the
    // last turn's end, and any ask or error).
    function layoutMarks(row, win) {
        const width = row.strip.clientWidth;
        if (!row.marks || width === 0 || width === row.markWidth) return;
        row.markWidth = width;
        let lastFull = -Infinity;
        const nodes = row.events.map(function (e) {
            const pct = win.pct(e.at);
            const x = (pct / 100) * width;
            const full = e.keep || x - lastFull >= MARK_GAP_PX;
            if (full) lastFull = x;
            const node = el('span', (full ? 'rc-mk' : 'rc-dot') + ' rc-k-' + e.kind + (e.pending ? ' rc-pending' : ''));
            node.style.left = pct + '%';
            node.title = e.tip + ' · ' + hhmm(e.time || e.at) + (e.snippet ? '\n' + e.snippet : '');
            if (full) node.appendChild(markerIcon(e.kind));
            return node;
        });
        row.marks.replaceChildren(...nodes);
    }

    // ---- rows ------------------------------------------------------
    function copyText(text) {
        function fallback() {
            const area = document.createElement('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            let ok = false;
            try {
                ok = document.execCommand('copy');
            } catch (err) {
                ok = false;
            }
            area.remove();
            return ok;
        }
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text).then(
                    function () {
                        return true;
                    },
                    function () {
                        return fallback();
                    }
                );
            }
        } catch (err) {
            /* no async clipboard here: use the fallback */
        }
        return Promise.resolve(fallback());
    }

    // ---- motion ----------------------------------------------------
    // Changes to the list animate by FLIP: measure where the rows are
    // drawn, make the change (the layout jumps straight to its end), then
    // play each part back from where it was. The rows animate only
    // transform, opacity and clip-path, so no frame needs their layout and
    // the browser can run them off the main thread while a refresh is
    // parsed and drawn. The one exception is the list's own height, which
    // carries the card's bottom edge and the legend below along with the
    // rows instead of jumping (nothing in the list depends on it).
    // Every part's motion comes from the one before/after measurement, so
    // a row sliding up the list and the rows an opening row pushes down
    // can't disagree, and a change mid-motion measures where things are
    // drawn then and carries on from there. Only what is on screen moves;
    // with reduced motion asked for, nothing does.
    const MOTION_MIN_MS = 180;
    const MOTION_MAX_MS = 400;
    const MOTION_MS_PER_PX = 0.25; // a longer way takes a little longer
    const MOTION_EASING = 'cubic-bezier(0.2, 0, 0, 1)';
    const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    const motions = new Set();
    let fresh = []; // parts drawn anew during a change: they fade in

    function motionOn() {
        return typeof Element.prototype.animate === 'function' && !(reducedMotion && reducedMotion.matches);
    }
    function motionMs(px) {
        return Math.round(Math.min(MOTION_MAX_MS, MOTION_MIN_MS + Math.abs(px) * MOTION_MS_PER_PX));
    }
    function play(node, keyframes, ms, done) {
        const anim = node.animate(keyframes, { duration: ms, easing: MOTION_EASING });
        motions.add(anim);
        function end() {
            motions.delete(anim);
            if (done) done();
        }
        anim.onfinish = end;
        anim.oncancel = end;
    }
    function fadeIn(node, ms) {
        play(node, [{ opacity: 0 }, { opacity: 0, offset: 0.25 }, { opacity: 1 }], ms);
    }
    // How much of a row a running reveal still clips off its bottom.
    function clippedOff(node) {
        const m = /inset\(([^)]*)\)/.exec(getComputedStyle(node).clipPath || '');
        if (!m) return 0;
        const v = m[1].trim().split(/\s+/).map(parseFloat);
        return (v.length > 2 ? v[2] : v[0]) || 0;
    }
    // Where each row and group heading is drawn: transforms included, and
    // the height it shows (less a reveal's clip) while it is moving.
    function measure() {
        const out = new Map();
        const moving = motions.size > 0;
        rowsEl.querySelectorAll('.rc-row, .rc-group-h').forEach(function (node) {
            const r = node.getBoundingClientRect();
            out.set(node, { top: r.top, height: r.height - (moving ? clippedOff(node) : 0) });
        });
        return out;
    }
    function onScreen(box) {
        return box.top < window.innerHeight && box.top + box.height > 0;
    }
    // The boxes a part is drawn as: itself or, under display: contents
    // (an open row at phone width), its children's.
    function boxes(node) {
        if (getComputedStyle(node).display !== 'contents') return [node];
        return Array.prototype.concat.apply([], Array.from(node.children).map(boxes));
    }
    // A closing row's panel, left drawn where it was (inert, unseen by
    // assistive tech) while it fades and the rows below slide up over it.
    // At phone width the panel has no box of its own, and simply goes.
    function ghostOf(row) {
        const panel = row.panel;
        if (panel.hidden || getComputedStyle(panel).display === 'contents') return null;
        const ghost = panel.cloneNode(true);
        ghost.removeAttribute('id');
        ghost.className = 'rc-ghost';
        ghost.setAttribute('aria-hidden', 'true');
        ghost.inert = true;
        ghost.style.top = panel.offsetTop + 'px';
        ghost.style.left = panel.offsetLeft + 'px';
        ghost.style.width = panel.offsetWidth + 'px';
        return ghost;
    }

    // Make `change` and animate what it moved. `opts.opened` / `opts.closed`
    // name a row whose panel it opens or closes.
    function flip(change, opts) {
        opts = opts || {};
        fresh = [];
        if (!motionOn()) {
            change();
            fresh = [];
            return;
        }
        const before = measure();
        const listWas = rowsEl.getBoundingClientRect();
        const ghost = opts.closed ? ghostOf(opts.closed) : null;
        Array.from(motions).forEach(function (anim) {
            anim.cancel();
        });
        rowsEl.querySelectorAll('.rc-ghost').forEach(function (node) {
            node.remove();
        });
        change();
        const after = measure();
        let longest = 0;
        function move(node, keyframes, ms, done) {
            longest = Math.max(longest, ms);
            play(node, keyframes, ms, done);
        }
        after.forEach(function (now, node) {
            const was = before.get(node);
            if (!onScreen(now) && !(was && onScreen(was))) return;
            if (!was) {
                fadeIn(node, MOTION_MIN_MS);
                return;
            }
            const dy = was.top - now.top;
            const grew = now.height - was.height;
            const from = {};
            const to = {};
            if (Math.abs(dy) >= 0.5) {
                from.transform = 'translateY(' + dy + 'px)';
                to.transform = 'none';
                // A row moving up is drawn above the rows it passes (only
                // an update moves a row up the list; the rest shift down)
                // and above a closing panel's copy.
                if (dy > 0) from.zIndex = to.zIndex = '1';
            }
            if (grew >= 0.5) {
                from.clipPath = 'inset(0px 0px ' + grew + 'px 0px)';
                to.clipPath = 'inset(0px 0px 0px 0px)';
            }
            if (Object.keys(from).length) move(node, [from, to], motionMs(Math.max(Math.abs(dy), grew)));
        });
        if (opts.opened) {
            const box = after.get(opts.opened.el);
            if (box && onScreen(box)) {
                const ms = motionMs(box.height - before.get(opts.opened.el).height);
                boxes(opts.opened.panel).forEach(function (node) {
                    fadeIn(node, ms);
                });
            }
        }
        if (ghost) {
            opts.closed.el.appendChild(ghost);
            move(ghost, [{ opacity: 1 }, { opacity: 0 }], motionMs(ghost.offsetHeight), function () {
                ghost.remove();
            });
        }
        const listNow = rowsEl.getBoundingClientRect();
        const longer = listNow.height - listWas.height;
        const edge = { top: listWas.top + Math.min(listWas.height, listNow.height), height: Math.abs(longer) };
        if (Math.abs(longer) >= 0.5 && onScreen(edge)) {
            play(
                rowsEl,
                [{ height: listWas.height + 'px' }, { height: listNow.height + 'px' }],
                longest || motionMs(longer)
            );
        }
        fresh.forEach(function (node) {
            if (node.isConnected && onScreen(node.getBoundingClientRect())) fadeIn(node, MOTION_MIN_MS);
        });
        fresh = [];
    }

    function makeRow(id) {
        const row = { id: id, sigs: {} };
        row.el = el('div', 'rc-row');
        row.el.setAttribute('role', 'listitem');
        row.el.dataset.id = id;

        // The chevron says the row opens; it is a pointer target only (the
        // block is the control keyboards and screen readers use), so a row
        // doesn't gain a second tab stop.
        row.chev = el('span', 'rc-chev');
        row.chev.setAttribute('aria-hidden', 'true');
        row.chev.appendChild(icon([ICON.chevron], 14));
        row.chev.addEventListener('click', toggle);

        row.block = el('button', 'rc-block');
        row.block.type = 'button';
        row.block.setAttribute('aria-controls', 'rc-x-' + id);
        const whenEl = el('span', 'rc-block-when');
        row.blockN = el('span', 'rc-block-n');
        row.blockU = el('span', 'rc-block-u');
        whenEl.append(row.blockN, row.blockU);
        row.blockStatus = el('span', 'rc-block-status');
        row.block.append(whenEl, row.blockStatus);
        function toggle() {
            const opening = !state.expanded.has(id);
            flip(
                function () {
                    if (opening) state.expanded.add(id);
                    else state.expanded.delete(id);
                    save();
                    applyExpanded(row);
                },
                opening ? { opened: row } : { closed: row }
            );
        }
        row.block.addEventListener('click', toggle);

        row.info = el('div', 'rc-info');
        row.strip = el('div', 'rc-strip');
        row.strip.setAttribute('role', 'img');

        row.copy = el('button', 'rc-copy');
        row.copy.type = 'button';
        row.copyLabel = el('span', '', '');
        row.copy.append(
            icon([ICON.copy], 13, [['rect', { x: '5', y: '5', width: '9', height: '9', rx: '1.5' }]]),
            row.copyLabel
        );
        row.copy.addEventListener('click', function () {
            copyText(id).then(function (ok) {
                row.copied = ok ? 'copied' : 'copy failed';
                setText(row.copyLabel, row.copied);
                row.copy.classList.toggle('rc-copied', ok);
                window.clearTimeout(row.copyTimer);
                row.copyTimer = window.setTimeout(function () {
                    row.copied = null;
                    row.copy.classList.remove('rc-copied');
                    setText(row.copyLabel, row.short);
                }, 1500);
            });
        });

        row.panel = el('div', 'rc-x');
        row.panel.id = 'rc-x-' + id;
        row.panel.hidden = true;

        row.el.append(row.chev, row.block, row.info, row.strip, row.copy, row.panel);
        return row;
    }

    function applyExpanded(row) {
        const open = state.expanded.has(row.id);
        setAttr(row.block, 'aria-expanded', open ? 'true' : 'false');
        setAttr(row.el, 'data-open', open ? 'true' : 'false');
        if (row.panel.hidden === open) row.panel.hidden = !open;
        if (row.label) {
            setAttr(row.block, 'aria-label', (open ? 'Hide' : 'Show') + ' the last message of ' + row.label);
        }
    }

    function drawInfo(row, s, ctx) {
        const left = cacheLeft(s, ctx);
        const sig = JSON.stringify([s.title, s.href, s.project, s.worktree, s.branch, left]);
        if (row.sigs.info === sig) return;
        row.sigs.info = sig;
        const title = el(s.href ? 'a' : 'span', 'rc-title', s.title);
        if (s.href) {
            title.href = s.href;
            title.title = 'Open the session page';
        }
        title.dataset.focus = 'title';
        const meta = el('span', 'rc-meta');
        const where = el('span', '');
        where.appendChild(el('span', 'rc-meta-project', s.project));
        if (s.worktree) where.appendChild(document.createTextNode(' / ' + s.worktree));
        meta.appendChild(where);
        if (s.branch) {
            const branch = el('span', 'rc-meta-branch');
            branch.title = 'Branch';
            branch.append(
                icon([ICON.branch], 12, [
                    ['circle', { cx: '4', cy: '3.5', r: '1.5' }],
                    ['circle', { cx: '4', cy: '12.5', r: '1.5' }],
                    ['circle', { cx: '12', cy: '6', r: '1.5' }]
                ]),
                document.createTextNode(s.branch)
            );
            meta.appendChild(branch);
        }
        if (left !== null) {
            const chip = el('span', 'rc-chip');
            chip.title = 'The prompt cache expires in ' + plural(left, 'minute');
            chip.append(
                icon([ICON.clock], 11, [['circle', { cx: '8', cy: '8', r: '6.25' }]]),
                document.createTextNode('cache ' + left + ' min')
            );
            meta.appendChild(chip);
        }
        refocus(row.info, function () {
            row.info.replaceChildren(title, meta);
        });
    }

    function drawPanel(row, s, ctx) {
        const sig = JSON.stringify([
            s.turnCount,
            s.toolCount,
            s.messages,
            s.href,
            s.lastPrompt,
            s.last,
            new Date(ctx.now).toDateString(),
            s.bg,
            s.bg.map(function (item) {
                return bgStatus(item, s, ctx);
            }),
            pendingWakes(s, ctx)
        ]);
        if (row.sigs.panel === sig) return;
        row.sigs.panel = sig;
        // The panel is redrawn whole; in an open row, the prompt or last
        // message that is new since it was last drawn fades in.
        const promptSig = JSON.stringify(s.lastPrompt);
        const lastSig = JSON.stringify(s.last);
        const newPrompt = row.sigs.prompt !== undefined && row.sigs.prompt !== promptSig;
        const newLast = row.sigs.last !== undefined && row.sigs.last !== lastSig;
        const shown = state.expanded.has(row.id);
        row.sigs.prompt = promptSig;
        row.sigs.last = lastSig;
        const side = el('div', 'rc-x-side');
        const dl = el('dl');
        [
            ['turns', s.turnCount],
            ['tool calls', s.toolCount],
            ['messages', s.messages]
        ].forEach(function (pair) {
            const item = el('div');
            item.append(el('dt', '', pair[0]), el('dd', '', String(pair[1])));
            dl.appendChild(item);
        });
        side.appendChild(dl);
        if (s.href) {
            const link = el('a', 'rc-x-link', 'Transcript →');
            link.href = s.href;
            link.dataset.focus = 'transcript';
            side.appendChild(link);
        }
        const main = el('div', 'rc-x-main');
        if (s.lastPrompt) {
            const prompt = el('div', 'rc-prompt');
            prompt.append(
                el('span', 'rc-prompt-at', '› ' + when(s.lastPrompt.at, ctx.now)),
                renderedMarkdown('rc-prompt-text', s.lastPrompt.html)
            );
            main.appendChild(prompt);
            if (shown && newPrompt) fresh.push(prompt);
        }
        if (s.last) {
            const last = el('div', 'rc-last rc-who-' + s.last.who);
            last.append(
                el('span', 'rc-who', WHO[s.last.who] + ' · ' + when(s.last.at, ctx.now)),
                s.last.html ? renderedMarkdown('rc-last-text', s.last.html) : el('p', 'rc-last-text', s.last.text)
            );
            main.appendChild(last);
            if (shown && newLast) fresh.push(last);
        } else {
            main.appendChild(el('p', 'rc-noreply', 'No reply yet.'));
        }
        const background = backgroundList(s, ctx);
        if (background) main.appendChild(background);
        refocus(row.panel, function () {
            row.panel.replaceChildren(side, main);
        });
    }

    // An open row's list of its background work: what is running first,
    // then wake-ups still to come, then the rest by start. Text only:
    // the labels are transcript text.
    function backgroundList(s, ctx) {
        const wakes = pendingWakes(s, ctx);
        if (!s.bg.length && !wakes.length) return null;
        const items = s.bg.map(function (item) {
            return { item: item, status: bgStatus(item, s, ctx) };
        });
        const running = items.filter(function (x) {
            return x.status === 'running';
        });
        const rest = items.filter(function (x) {
            return x.status !== 'running';
        });
        const lines = running
            .map(function (x) {
                return bgLine(x);
            })
            .concat(
                wakes.map(function (w) {
                    return { text: 'Wakes up ' + hhmm(w.at) + (w.label ? ' · ' + w.label : ''), cls: 'rc-bgl-wake' };
                }),
                rest.map(function (x) {
                    return bgLine(x);
                })
            );
        const box = el('div', 'rc-bgl');
        box.appendChild(el('span', 'rc-who', 'Background'));
        const list = el('ul');
        lines.slice(0, MAX_BG_LISTED).forEach(function (line) {
            list.appendChild(el('li', line.cls, line.text));
        });
        if (lines.length > MAX_BG_LISTED) {
            list.appendChild(el('li', 'rc-bgl-more', '+' + (lines.length - MAX_BG_LISTED) + ' more'));
        }
        box.appendChild(list);
        return box;
    }
    function bgLine(x) {
        const parts = [BG_KINDS[x.item.kind]];
        if (x.item.label) parts.push(x.item.label);
        parts.push(bgSpan(x.item, x.status));
        return { text: parts.join(' · '), cls: 'rc-bgl-' + x.status };
    }

    // Replace a part's content, putting focus back on its counterpart if
    // it was inside (the same data-focus key).
    function refocus(container, replace) {
        const active = document.activeElement;
        const key = active && container.contains(active) ? active.dataset.focus : null;
        replace();
        if (key) {
            const again = container.querySelector('[data-focus="' + key + '"]');
            if (again) again.focus();
        }
    }

    function updateRow(row, s, ctx) {
        row.short = s.short;
        const shownState = effectiveState(s, ctx);
        row.el.className = 'rc-row rc-state-' + shownState;
        const t = since(s.lastAt, ctx.now);
        setText(row.blockN, t.n);
        setText(row.blockU, t.u);
        row.block.classList.toggle('rc-block-long', t.u !== 'min' && t.u !== 'h');
        setText(row.blockStatus, STATES[shownState]);
        row.label = s.title + ', ' + STATES[shownState] + ', updated ' + ago(s.lastAt, ctx.now);
        applyExpanded(row);
        drawInfo(row, s, ctx);
        const stripSig = JSON.stringify([
            Math.floor(ctx.now / 60000),
            ctx.ttl,
            s.lastAt,
            s.state,
            s.turns,
            s.tools,
            s.earlierTurns,
            s.turnCount,
            s.messages,
            s.bg,
            s.wakes,
            ctx.bgCap,
            ctx.wakeGrace
        ]);
        if (row.sigs.strip !== stripSig) {
            row.sigs.strip = stripSig;
            drawStrip(row, s, ctx);
        }
        setAttr(row.copy, 'aria-label', 'Copy session id ' + s.id);
        setAttr(row.copy, 'title', 'Copy the session id (claude --resume takes it)');
        if (!row.copied) setText(row.copyLabel, s.short);
        drawPanel(row, s, ctx);
    }

    function groupFor(dir, name) {
        let group = groups.get(dir);
        if (!group) {
            group = { dir: dir, el: el('section', 'rc-group') };
            group.heading = el('h2', 'rc-group-h');
            group.name = el('span', '');
            group.count = el('span', 'rc-group-n');
            group.heading.append(group.name, group.count);
            group.list = el('div', 'rc-list');
            group.list.setAttribute('role', 'list');
            group.el.append(group.heading, group.list);
            groups.set(dir, group);
        }
        setText(group.name, name);
        setAttr(group.el, 'aria-label', name);
        return group;
    }

    // ---- render ----------------------------------------------------
    // A refresh or the clock animates what it moves; the first draw, and
    // choosing a filter or grouping (`instant`), redraw at once.
    function render(instant) {
        if (!state.payload) return;
        if (!lastCtx || instant) {
            draw();
            fresh = [];
        } else {
            flip(draw);
        }
    }
    function draw() {
        const payload = state.payload;
        const now = clock();
        const ctx = {
            now: now,
            ttl: payload.ttl,
            bgCap: payload.bgCap,
            wakeGrace: payload.wakeGrace,
            win: stripCtx(now)
        };
        const scrollY = window.scrollY;

        filterEls.forEach(function (button) {
            const name = button.getAttribute('data-filter');
            const count = payload.sessions.filter(function (s) {
                return FILTERS[name](s, ctx);
            }).length;
            setText(button.querySelector('.rc-n'), String(count));
            setAttr(button, 'aria-pressed', name === state.filter ? 'true' : 'false');
        });
        setText(updatedEl, payload.generatedAt ? hhmm(payload.generatedAt) : '–');
        updatedEl.title = payload.generatedAt ? new Date(payload.generatedAt).toString() : '';
        // Old data: say so (a role=status region, so only rewrite it when
        // its text changes, or a screen reader hears it on every tick).
        const stale = payload.generatedAt && now - payload.generatedAt > STALE_MS;
        if (staleEl.hidden === Boolean(stale)) staleEl.hidden = !stale;
        const staleText = stale ? 'Data from ' + ago(payload.generatedAt, now) + ' — is ' : '';
        if (stale && staleEl.dataset.text !== staleText) {
            staleEl.dataset.text = staleText;
            staleEl.replaceChildren(
                document.createTextNode(staleText),
                el('code', '', 'claude-code-log watch'),
                document.createTextNode(' running?')
            );
        }
        groupEl.checked = state.group;
        drawAxis(ctx);

        const ids = new Set();
        const visible = [];
        payload.sessions.forEach(function (s) {
            ids.add(s.id);
            let row = rows.get(s.id);
            if (!row) {
                row = makeRow(s.id);
                rows.set(s.id, row);
            }
            if (!FILTERS[state.filter](s, ctx)) return;
            updateRow(row, s, ctx);
            visible.push({ s: s, row: row });
        });
        rows.forEach(function (row, id) {
            if (!ids.has(id)) rows.delete(id);
        });

        if (state.group) {
            rowsEl.removeAttribute('role');
            const order = [];
            const members = new Map();
            visible.forEach(function (v) {
                const dir = v.s.projectDir;
                if (!members.has(dir)) {
                    members.set(dir, []);
                    order.push(groupFor(dir, v.s.project));
                }
                members.get(dir).push(v.row.el);
            });
            order.forEach(function (group) {
                const list = members.get(group.dir);
                setText(group.count, String(list.length));
                syncChildren(group.list, list);
            });
            syncChildren(
                rowsEl,
                order.map(function (g) {
                    return g.el;
                })
            );
        } else {
            rowsEl.setAttribute('role', 'list');
            syncChildren(
                rowsEl,
                visible.map(function (v) {
                    return v.row.el;
                })
            );
        }

        emptyEl.hidden = visible.length > 0;
        if (!visible.length) {
            setText(
                emptyEl,
                payload.sessions.length
                    ? 'No sessions match this filter.'
                    : payload.window
                      ? 'No sessions in the date window ' + payload.window + '.'
                      : 'No sessions updated in the last ' + plural(payload.windowDays, 'day') + '.'
            );
        }
        visible.forEach(function (v) {
            layoutMarks(v.row, ctx.win);
        });
        if (window.scrollY !== scrollY) window.scrollTo(window.scrollX, scrollY);
        lastCtx = ctx;
    }

    // ---- controls --------------------------------------------------
    filterEls.forEach(function (button) {
        button.addEventListener('click', function () {
            state.filter = button.getAttribute('data-filter');
            save();
            render(true);
        });
    });
    groupEl.addEventListener('change', function () {
        state.group = groupEl.checked;
        save();
        render(true);
    });
    let resizeFrame = 0;
    window.addEventListener('resize', function () {
        if (resizeFrame) return;
        resizeFrame = window.requestAnimationFrame(function () {
            resizeFrame = 0;
            if (!lastCtx) return;
            rows.forEach(function (row) {
                if (row.el.isConnected) layoutMarks(row, lastCtx.win);
            });
        });
    });

    // ---- legend ----------------------------------------------------
    (function legend() {
        const box = $('rc-legend');
        if (!box) return;
        function item(sample, label) {
            const node = el('span', 'rc-lg');
            node.append(sample, document.createTextNode(label));
            return node;
        }
        function mark(kind) {
            const node = el('span', 'rc-mk rc-k-' + kind);
            node.appendChild(markerIcon(kind));
            return node;
        }
        const band = el('span', 'rc-band');
        box.replaceChildren(
            item(mark('prompt'), 'prompt'),
            item(mark('asst'), 'reply'),
            item(mark('ask'), 'asked you'),
            item(mark('err'), 'error'),
            item(el('span', 'rc-tick'), 'tool call'),
            item(band, 'prompt cache warm'),
            item(el('span', 'rc-bg rc-bg-running'), 'background work'),
            item(mark('task'), 'work reported'),
            item(mark('wake'), 'wake-up')
        );
    })();

    // ---- data ------------------------------------------------------
    window.__cclRecent = function (raw) {
        const payload = cleanPayload(raw);
        if (!payload) return;
        state.payload = payload;
        render();
    };

    let loading = null;
    function reload() {
        if (loading) return;
        const script = document.createElement('script');
        script.src = DATA_SRC + '?t=' + Date.now();
        script.async = true;
        function done() {
            script.remove();
            if (loading === script) loading = null;
        }
        script.onload = done;
        script.onerror = done;
        loading = script;
        document.head.appendChild(script);
    }

    let reloadTimer = 0;
    function schedule() {
        window.clearInterval(reloadTimer);
        reloadTimer = 0;
        if (document.hidden) return;
        reloadTimer = window.setInterval(reload, RELOAD_MS);
    }
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) reload();
        schedule();
    });
    // The page's own <script src='recent-data.js'> (after this one) gives
    // the first payload, and has run or failed by DOMContentLoaded. Only
    // then is "no data" worth drawing, and the refresh worth starting.
    document.addEventListener('DOMContentLoaded', function () {
        if (!state.payload) {
            state.payload = cleanPayload({ sessions: [] });
            render();
        }
        schedule();
    });
    window.setInterval(function () {
        if (!document.hidden) render();
    }, TICK_MS);
})();
