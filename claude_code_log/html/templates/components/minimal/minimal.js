        // Minimal theme (--theme minimal) runtime: colour-scheme toggle,
        // gutter times, session-summary more/less, toolbar offsets and the
        // overflow menu. Collapse
        // labels and fold depth arrive in P4 (work/minimal-theme-dag.md).
        // Everything bound here is either on the toolbar or delegated on
        // `document`, so a live update's #transcript swap leaves it intact;
        // only the per-card gutter times register with the rehydrate hook.
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
