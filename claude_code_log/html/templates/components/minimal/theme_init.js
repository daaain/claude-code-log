        // Minimal theme: apply the stored colour scheme before first paint.
        // `claude-code-log:theme` holds 'auto' | 'light' | 'dark'; only an
        // explicit choice sets <html data-theme>, so 'auto' (and a missing,
        // blocked or unreadable store) follows prefers-color-scheme.
        (function () {
            try {
                var mode = window.localStorage.getItem('claude-code-log:theme');
                if (mode === 'light' || mode === 'dark') {
                    document.documentElement.setAttribute('data-theme', mode);
                }
            } catch (err) { /* storage unavailable: follow the system */ }
        })();
        // Lay the transcript out once: hide the stage while the page parses
        // (dag.css, `mn-parsing`), so the first layout it gets is the DAG
        // engine's grid at DOMContentLoaded, not a nested page laid out
        // frame by frame during the parse and then thrown away. This is the
        // page's first DOMContentLoaded listener, so every other one —
        // folds, filter, deep links, search, the engine — sees the stage
        // rendered, as before; they run in one task, so nothing paints in
        // between.
        (function () {
            var root = document.documentElement;
            root.classList.add('mn-parsing');
            document.addEventListener('DOMContentLoaded', function () {
                root.classList.remove('mn-parsing');
            });
        })();
