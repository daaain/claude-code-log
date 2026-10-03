        // Minimal theme (--theme minimal) runtime: colour-scheme toggle,
        // gutter times, session-summary more/less, toolbar offsets, the
        // overflow menu, collapse previews ("+N lines" / "− less") and the
        // fold-depth control (Prompts / Steps / All). See
        // work/minimal-theme-dag.md § 1.4–1.5 and § 3.4.
        // Everything bound here is either on the toolbar or delegated on
        // `document`, so a live update's #transcript swap leaves it intact;
        // only per-card decoration (gutter times, session summaries,
        // collapse labels, fold depth for new cards) registers with the
        // rehydrate hook.
        (function () {
            'use strict';
            const THEME_KEY = 'claude-code-log:theme';
            const MODES = ['auto', 'light', 'dark'];
            const root = document.documentElement;

            // ---- colour scheme: Auto / Light / Dark ---------------------
            function readMode() {
                try {
                    const stored = window.localStorage.getItem(THEME_KEY);
                    return MODES.indexOf(stored) >= 0 ? stored : 'auto';
                } catch (err) {
                    return 'auto';
                }
            }
            function writeMode(mode) {
                try {
                    if (mode === 'auto') window.localStorage.removeItem(THEME_KEY);
                    else window.localStorage.setItem(THEME_KEY, mode);
                } catch (err) { /* storage unavailable: choice lasts this page */ }
            }
            function applyMode(mode) {
                if (mode === 'light' || mode === 'dark') root.setAttribute('data-theme', mode);
                else root.removeAttribute('data-theme');
                document.querySelectorAll('[data-mn-theme]').forEach(function (btn) {
                    const on = btn.getAttribute('data-mn-theme') === mode;
                    btn.classList.toggle('on', on);
                    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
                });
            }
            document.addEventListener('click', function (event) {
                const btn = event.target.closest('[data-mn-theme]');
                if (!btn) return;
                const mode = btn.getAttribute('data-mn-theme');
                writeMode(mode);
                applyMode(mode);
            });
            // Another tab changed the choice: follow it.
            window.addEventListener('storage', function (event) {
                if (event.key === THEME_KEY || event.key === null) applyMode(readMode());
            });
            applyMode(readMode());

            // ---- gutter time: short local HH:MM:SS -------------------------
            // The server renders the UTC time-of-day (correct without JS);
            // this localises it, like timezone_converter.js does for the full
            // timestamp, which stays in the DOM and becomes the tooltip.
            const timeFormatter = new Intl.DateTimeFormat(undefined, {
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
            });
            const fullFormatter = new Intl.DateTimeFormat(undefined, {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit',
                hour12: false, timeZoneName: 'short'
            });
            function localiseGutterTimes(scope) {
                (scope || document).querySelectorAll('.mn-time').forEach(function (el) {
                    const stamp = el.parentElement &&
                        el.parentElement.querySelector('.timestamp[data-timestamp]');
                    if (!stamp) return;
                    const date = new Date(stamp.getAttribute('data-timestamp'));
                    if (isNaN(date.getTime())) return;
                    el.textContent = timeFormatter.format(date);
                    const duration = stamp.getAttribute('data-duration');
                    el.title = fullFormatter.format(date) + (duration ? ' · ' + duration : '');
                });
            }
            localiseGutterTimes(document);
            if (window.claudeLogOnRehydrate) window.claudeLogOnRehydrate(localiseGutterTimes);

            // ---- session summary: two lines, more / less -------------------
            // CSS clamps a session header's summary to two lines; when that
            // hides text, a small toggle (placed on the meta line after it)
            // expands it. The full text is also the summary's `title`. A live
            // update can replace a header, so this runs on rehydrate too.
            function decorateSessionSummaries(scope) {
                (scope || document).querySelectorAll('.mn-sh-sum').forEach(function (sum) {
                    const next = sum.nextElementSibling;
                    const toggle = next && next.classList.contains('mn-sh-more') ? next : null;
                    const open = sum.classList.contains('mn-open');
                    const clamped = open || sum.scrollHeight > sum.clientHeight + 1;
                    if (!clamped) {
                        if (toggle) toggle.remove();
                        return;
                    }
                    if (toggle) return;
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'mn-sh-more';
                    btn.textContent = open ? '− less' : '+ more';
                    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
                    sum.after(btn);
                });
            }
            document.addEventListener('click', function (event) {
                const btn = event.target.closest('.mn-sh-more');
                if (!btn) return;
                const sum = btn.previousElementSibling;
                if (!sum || !sum.classList.contains('mn-sh-sum')) return;
                const open = sum.classList.toggle('mn-open');
                btn.textContent = open ? '− less' : '+ more';
                btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
            decorateSessionSummaries(document);
            if (window.claudeLogOnRehydrate) window.claudeLogOnRehydrate(decorateSessionSummaries);
            window.addEventListener('resize', function () { decorateSessionSummaries(document); });

            // ---- sticky offsets ------------------------------------------
            // The search & filter panel and the timeline are sticky too; they
            // sit under the toolbar, whose height changes when it wraps.
            const toolbar = document.querySelector('.mn-toolbar');
            const filterPanel = document.querySelector('.filter-toolbar');
            function updateOffsets() {
                if (toolbar) root.style.setProperty('--mn-bar-h', toolbar.offsetHeight + 'px');
                const filterHeight = filterPanel && getComputedStyle(filterPanel).display !== 'none'
                    ? filterPanel.offsetHeight : 0;
                root.style.setProperty('--mn-filter-h', filterHeight + 'px');
            }
            updateOffsets();
            if (window.ResizeObserver) {
                const observer = new ResizeObserver(updateOffsets);
                if (toolbar) observer.observe(toolbar);
                if (filterPanel) observer.observe(filterPanel);
            } else {
                window.addEventListener('resize', updateOffsets);
            }

            // The resume button's copy toast is fixed-position; drop it just
            // below the toolbar wherever the toolbar is when it's clicked
            // (it moves from under the header to the top as the page scrolls).
            document.addEventListener('click', function (event) {
                if (!toolbar || !event.target.closest('#resumeSession')) return;
                root.style.setProperty('--mn-toast-top',
                    Math.max(0, toolbar.getBoundingClientRect().bottom) + 8 + 'px');
            });

            // ---- collapse previews: "+N lines" / "− less" -----------------
            // The formatters' <details> already carry a preview in their
            // <summary>; CSS clips it (with a fade) while closed and shows
            // the summary as a "− less" line while open. The label comes
            // from the `.line-count` the formatter wrote, else from the
            // body's text (\n count) or a params table's row count, and is
            // written to `data-more` on the <summary> — CSS renders it with
            // `attr()`, which only reads the pseudo-element's own element.
            // Generated content stays out of textContent, so search and the
            // timeline never index the labels. Long bodies also get a
            // trailing "− less" button (label in CSS too).
            const COLLAPSIBLES = 'details.collapsible-code, details.collapsible-details, details.tool-param-collapsible';
            const LONG_BODY = 12;

            function countLines(text) {
                const trimmed = text.trim();
                return trimmed ? trimmed.split('\n').length : 0;
            }
            function measureBody(details, summary) {
                const lineCount = summary.querySelector(':scope > .line-count');
                const counted = lineCount ? parseInt(lineCount.textContent, 10) : NaN;
                if (counted > 0) return { n: counted, unit: 'line' };
                const table = details.querySelector(':scope > table');
                if (table) {
                    const rows = table.querySelectorAll(':scope > tbody > tr, :scope > tr').length;
                    return { n: rows, unit: 'item' };
                }
                let text = '';
                Array.from(details.children).forEach(function (child) {
                    if (child === summary || child.classList.contains('mn-less')
                        || child.classList.contains('tool-param-fold-controls')) return;
                    text += child.textContent;
                });
                return { n: countLines(text), unit: 'line' };
            }
            function moreLabel(size) {
                if (size.unit === 'item' && size.n > 0) {
                    return '+ ' + size.n + (size.n === 1 ? ' item' : ' items');
                }
                // One or two lines are (mostly) in the preview already: what
                // is hidden is the rest of a long line, not more lines.
                if (size.n >= 3) return '+ ' + size.n + ' lines';
                return '+ more';
            }
            function decorateCollapsibles(scope) {
                const found = Array.from((scope || document).querySelectorAll(COLLAPSIBLES));
                if (scope && scope.matches && scope.matches(COLLAPSIBLES)) found.unshift(scope);
                found.forEach(function (details) {
                    const summary = details.querySelector(':scope > summary');
                    if (!summary || summary.hasAttribute('data-more')) return;
                    const size = measureBody(details, summary);
                    summary.setAttribute('data-more', moreLabel(size));
                    if (size.n >= LONG_BODY && !details.querySelector(':scope > .mn-less')) {
                        const less = document.createElement('button');
                        less.type = 'button';
                        less.className = 'mn-less';
                        less.setAttribute('aria-label', 'Show less');
                        details.appendChild(less);
                    }
                });
            }
            // A trailing "− less" closes its block and, when the block's top
            // has scrolled away under the sticky toolbar, brings it back.
            document.addEventListener('click', function (event) {
                const btn = event.target.closest('.mn-less');
                if (!btn) return;
                const details = btn.parentElement;
                if (!details || details.tagName !== 'DETAILS') return;
                details.open = false;
                const bar = document.querySelector('.mn-toolbar');
                const barBottom = bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
                const top = details.getBoundingClientRect().top;
                if (top < barBottom) window.scrollBy(0, top - barBottom - 8);
            });
            decorateCollapsibles(document);
            if (window.claudeLogOnRehydrate) window.claudeLogOnRehydrate(decorateCollapsibles);

            // ---- fold depth: Prompts / Steps / All -------------------------
            // Maps onto the classic fold state machine (transcript.html,
            // dev-docs/message-hierarchy.md) through the hooks the minimal
            // template branch exports: every card decides only its OWN
            // children container, then each fold bar is re-synced from what
            // the containers show.
            //   prompts — session and branch headers open, everything else
            //             folded: each turn is its prompt + fold-bar line;
            //   steps   — everything open except sub-agent subtrees (cards
            //             that open a deeper agent's transcript); long blocks
            //             as previews (every collapsible closed);
            //   all     — everything open, every collapsible open.
            // A fold-bar click or the 📋 open/close-all overrides the depth
            // locally: the segment then shows no choice until the next one.
            // Opening one preview doesn't (reading a block isn't a new depth).
            const DEPTH_KEY = 'claude-code-log:fold-depth';
            const DEPTHS = ['prompts', 'steps', 'all'];
            function readDepth() {
                try {
                    const stored = window.localStorage.getItem(DEPTH_KEY);
                    return DEPTHS.indexOf(stored) >= 0 ? stored : 'steps';
                } catch (err) {
                    return 'steps';
                }
            }
            function writeDepth(value) {
                try {
                    window.localStorage.setItem(DEPTH_KEY, value);
                } catch (err) { /* storage unavailable: depth lasts this page */ }
            }
            let depth = readDepth();

            function markDepth(active) {
                document.querySelectorAll('[data-mn-depth]').forEach(function (btn) {
                    const on = active && btn.getAttribute('data-mn-depth') === depth;
                    btn.classList.toggle('on', on);
                    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
                });
            }
            function childrenOf(card) {
                const node = card.parentElement;
                return node && node.classList.contains('message-node')
                    ? node.querySelector(':scope > .children') : null;
            }
            function agentDepth(card) {
                const match = /(?:^|\s)agent-depth-(\d+)(?:\s|$)/.exec(card.className);
                if (match) return parseInt(match[1], 10);
                return card.classList.contains('sidechain') ? 1 : 0;
            }
            // A card whose children start a deeper agent's transcript: the
            // spawning tool_result (or a workflow agent card). P6's Branches
            // control takes these over; until then Steps keeps them folded,
            // which is what its default ("Main only") will show.
            function opensBranch(card, children) {
                const own = agentDepth(card);
                return Array.from(children.querySelectorAll(':scope > .message-node > .message.sidechain'))
                    .some(function (kid) { return agentDepth(kid) > own; });
            }
            function wantsOpen(card, children, value) {
                if (value === 'all') return true;
                if (card.classList.contains('session-header')) return true;
                if (value === 'prompts') return false;
                return !opensBranch(card, children);
            }
            const seenContainers = new WeakSet();
            const depthApplied = new WeakSet();
            function applyDepthTo(cards, value) {
                const fold = window.claudeLogApplyFoldState;
                const sync = window.claudeLogSyncFoldBar;
                if (!fold || !sync) return;
                const touched = [];
                cards.forEach(function (card) {
                    depthApplied.add(card);
                    const children = childrenOf(card);
                    if (!children) return;
                    seenContainers.add(children);
                    if (wantsOpen(card, children, value)) children.style.display = '';
                    else fold(card, 'folded');
                    touched.push(card);
                });
                touched.forEach(sync);
            }
            function setCollapsibles(scope, open) {
                scope.querySelectorAll(COLLAPSIBLES).forEach(function (details) {
                    if (details.open !== open) details.open = open;
                });
            }
            function transcriptCards() {
                const transcript = document.getElementById('transcript');
                return transcript ? Array.from(transcript.querySelectorAll('.message')) : [];
            }
            function chooseDepth(value) {
                depth = value;
                writeDepth(value);
                applyDepthTo(transcriptCards(), value);
                const transcript = document.getElementById('transcript');
                if (transcript) setCollapsibles(transcript, value === 'all');
                markDepth(true);
                if (window.claudeLogUpdateDetailsToggle) window.claudeLogUpdateDetailsToggle();
            }
            document.addEventListener('click', function (event) {
                const btn = event.target.closest('[data-mn-depth]');
                if (btn) {
                    chooseDepth(btn.getAttribute('data-mn-depth'));
                    return;
                }
                if (event.target.closest('#transcript .fold-bar-section, #toggleDetails')) {
                    markDepth(false);
                }
            });
            // Called by transcript.html right after the classic initial fold
            // state, before deep links and the search component reveal
            // anything. Previews start closed in the markup, so only All has
            // collapsibles to open.
            window.claudeLogMinimalInitFolds = function () {
                applyDepthTo(transcriptCards(), depth);
                if (depth === 'all') {
                    const transcript = document.getElementById('transcript');
                    if (transcript) setCollapsibles(transcript, true);
                }
                markDepth(true);
            };
            markDepth(true);

            // Live updates (serve --watch): the swap restores the fold state
            // of every card that was already on screen and tags the new ones
            // `live-new`; the patch keeps existing cards' containers and also
            // tags its additions. New cards — and, on a patch, a card that
            // just gained its first children (its container arrives
            // wholesale) — get the current depth; nothing the reader folded
            // or unfolded by hand is touched.
            if (window.claudeLogOnRehydrate) {
                window.claudeLogOnRehydrate(function (scope) {
                    if (!scope || !scope.querySelectorAll) return;
                    const swap = scope.id === 'transcript';
                    const cards = Array.from(scope.querySelectorAll('.message'));
                    if (scope.matches && scope.matches('.message')) cards.unshift(scope);
                    const fresh = cards.filter(function (card) {
                        if (card.classList.contains('live-new') && !depthApplied.has(card)) return true;
                        if (swap) return false;
                        const children = childrenOf(card);
                        return !!children && !seenContainers.has(children);
                    });
                    cards.forEach(function (card) {
                        const children = childrenOf(card);
                        if (children) seenContainers.add(children);
                        if (!card.classList.contains('live-new')) depthApplied.add(card);
                    });
                    applyDepthTo(fresh, depth);
                    if (depth === 'all') {
                        fresh.forEach(function (card) { setCollapsibles(card, true); });
                    }
                });
            }

            // ---- overflow menu ---------------------------------------------
            // A <details>: closes on a click outside it or on Escape. Clicks
            // on its items keep it open, so a state change stays visible.
            const more = document.querySelector('.mn-more');
            if (more) {
                document.addEventListener('click', function (event) {
                    if (more.open && !more.contains(event.target)) more.open = false;
                });
                document.addEventListener('keydown', function (event) {
                    if (event.key === 'Escape' && more.open) {
                        more.open = false;
                        const summary = more.querySelector('summary');
                        if (summary) summary.focus();
                    }
                });
            }
        })();
