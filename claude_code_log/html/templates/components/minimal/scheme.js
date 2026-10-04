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
