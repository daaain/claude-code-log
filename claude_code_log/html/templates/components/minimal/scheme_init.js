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
    } catch (err) {
        /* storage unavailable: follow the system */
    }
})();
