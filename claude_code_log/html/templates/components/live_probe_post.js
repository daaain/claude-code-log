// Following from disk, the framed half (after #transcript): see
// live_probe.js. Reached only when the file changed since the parent last
// looked, so this posts the new render, then stops the load before any
// later script runs.
//
// The transcript goes only to a parent that names this page's token
// (live_probe.js). `postMessage` to a `file://` parent cannot be addressed
// to an origin (every file is an opaque one), so without that check any
// local HTML file could frame this page and read it. The token is found
// in this page and nowhere else: not in a link, not in the JSONL (a card's
// uuid, the first design's token, is in both — archive-search links carry
// it), so naming it shows the caller has read this file.
(function () {
    const probe = window.__cclProbe;
    if (!probe) return;
    const transcript = document.getElementById('transcript');
    const title = document.getElementById('title');
    const known = probe.token && probe.token === window.__cclProbeToken;
    if (!known || !transcript || !title) {
        window.parent.postMessage({ cclProbe: 'refused' }, '*');
    } else {
        const cards = transcript.querySelectorAll('.message[data-uuid]');
        window.parent.postMessage(
            {
                cclProbe: 'page',
                lm: document.lastModified,
                html: title.outerHTML + transcript.outerHTML,
                // An update replaces only the transcript and the title, so
                // a render in the other theme can't be applied to this page.
                theme: transcript.closest('.mn-stage') ? 'minimal' : 'classic',
                // What decoration never rewrites, to tell whether this
                // render is the one already on screen (live_update.js).
                fingerprint: [
                    cards.length,
                    cards.length ? cards[cards.length - 1].getAttribute('data-uuid') : '',
                    title.textContent
                ].join('\n')
            },
            '*'
        );
    }
    window.stop();
})();
