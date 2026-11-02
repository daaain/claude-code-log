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
// through textContent and setAttribute only, never as markup.
(function () {
    'use strict';

    const DATA_SRC = 'recent-data.js';
    const RELOAD_MS = 15000;
    const TICK_MS = 30000;
    const STALE_MS = 2 * 60000;
    const BEFORE_MS = 6 * 3600000; // the strip shows 6 h back...
    const AFTER_MS = 3600000; // ...and 1 h ahead
    const SPAN_MS = BEFORE_MS + AFTER_MS;
    const NOW_PCT = (BEFORE_MS / SPAN_MS) * 100;
    const MARK_GAP_PX = 16;
    const CACHE_RISK_MIN = 45;
    const STORE_KEY = 'claude-code-log:recent';
    const SVG_NS = 'http://www.w3.org/2000/svg';

    const STATES = {
        asked: 'asked you',
        error: 'error',
        done: 'finished',
        busy: 'mid-turn'
    };
    const WHO = { claude: 'Claude', tool: 'Tool call', error: 'Error' };
    const FILTERS = {
        all: function () {
            return true;
        },
        move: function (s) {
            return s.state === 'asked' || s.state === 'error' || s.state === 'done';
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
        copy: 'M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5'
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
            state: Object.prototype.hasOwnProperty.call(STATES, raw.state) ? raw.state : 'done',
            messages: num(raw.messages),
            turnCount: num(raw.turnCount),
            toolCount: num(raw.toolCount),
            turns: turns
                .filter(function (t) {
                    return Array.isArray(t) && t.length >= 2;
                })
                .map(function (t) {
                    return [num(t[0]), num(t[1]), str(t[2]) || 'asst'];
                }),
            earlierTurns: num(raw.earlierTurns),
            tools: (Array.isArray(raw.tools) ? raw.tools : []).map(num),
            lastPrompt: prompt ? { at: num(prompt.at), text: str(prompt.text) } : null,
            last: last ? { at: num(last.at), who: WHO[last.who] ? last.who : 'claude', text: str(last.text) } : null
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
            ttl: num(raw.cacheTtlMinutes) || 60,
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
            const earlier =
                s.earlierTurns +
                s.turns.filter(function (t) {
                    return t[1] < win.start;
                }).length;
            if (earlier > 0) {
                nodes.push(el('span', 'rc-strip-note rc-earlier', '◂ +' + plural(earlier, 'earlier turn')));
            }
            const lastTurn = s.turns.length ? s.turns[s.turns.length - 1] : null;
            label =
                plural(s.turnCount, 'turn') +
                (lastTurn ? ', the last ending ' + hhmm(lastTurn[1]) : '') +
                (s.lastAt + ttlMs > ctx.now ? '; cache warm until ' + hhmm(s.lastAt + ttlMs) : '');
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
        row.events = inWindow ? markerEvents(s, shown, win) : [];
        row.markWidth = -1;
    }
    function markerEvents(s, shown, win) {
        const events = [];
        const final = s.turns[s.turns.length - 1];
        shown.forEach(function (t) {
            if (t[0] >= win.start) events.push({ at: t[0], kind: 'prompt', keep: false, tip: 'You' });
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
                tip: tip
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
            const node = el('span', (full ? 'rc-mk' : 'rc-dot') + ' rc-k-' + e.kind);
            node.style.left = pct + '%';
            node.title = e.tip + ' · ' + hhmm(e.at);
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

    function makeRow(id) {
        const row = { id: id, sigs: {} };
        row.el = el('div', 'rc-row');
        row.el.setAttribute('role', 'listitem');
        row.el.dataset.id = id;

        row.block = el('button', 'rc-block');
        row.block.type = 'button';
        row.block.setAttribute('aria-controls', 'rc-x-' + id);
        const whenEl = el('span', 'rc-block-when');
        row.blockN = el('span', 'rc-block-n');
        row.blockU = el('span', 'rc-block-u');
        whenEl.append(row.blockN, row.blockU);
        row.blockStatus = el('span', 'rc-block-status');
        row.block.append(whenEl, row.blockStatus);
        row.block.addEventListener('click', function () {
            if (state.expanded.has(id)) state.expanded.delete(id);
            else state.expanded.add(id);
            save();
            applyExpanded(row);
        });

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

        row.el.append(row.block, row.info, row.strip, row.copy, row.panel);
        return row;
    }

    function applyExpanded(row) {
        const open = state.expanded.has(row.id);
        setAttr(row.block, 'aria-expanded', open ? 'true' : 'false');
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
            new Date(ctx.now).toDateString()
        ]);
        if (row.sigs.panel === sig) return;
        row.sigs.panel = sig;
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
            const prompt = el('p', 'rc-prompt');
            prompt.append(
                el('span', 'rc-prompt-at', '› ' + when(s.lastPrompt.at, ctx.now)),
                document.createTextNode('  ' + s.lastPrompt.text)
            );
            main.appendChild(prompt);
        }
        if (s.last) {
            const last = el('div', 'rc-last rc-who-' + s.last.who);
            last.append(
                el('span', 'rc-who', WHO[s.last.who] + ' · ' + when(s.last.at, ctx.now)),
                el('p', 'rc-last-text', s.last.text)
            );
            main.appendChild(last);
        } else {
            main.appendChild(el('p', 'rc-noreply', 'No reply yet.'));
        }
        refocus(row.panel, function () {
            row.panel.replaceChildren(side, main);
        });
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
        row.el.className = 'rc-row rc-state-' + s.state;
        const t = since(s.lastAt, ctx.now);
        setText(row.blockN, t.n);
        setText(row.blockU, t.u);
        row.block.classList.toggle('rc-block-long', t.u !== 'min' && t.u !== 'h');
        setText(row.blockStatus, STATES[s.state]);
        row.label = s.title + ', ' + STATES[s.state] + ', updated ' + ago(s.lastAt, ctx.now);
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
            s.messages
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
    function render() {
        const payload = state.payload;
        if (!payload) return;
        const now = clock();
        const ctx = { now: now, ttl: payload.ttl, win: stripCtx(now) };
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
            render();
        });
    });
    groupEl.addEventListener('change', function () {
        state.group = groupEl.checked;
        save();
        render();
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
            item(band, 'prompt cache warm')
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
