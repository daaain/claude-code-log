// Following from disk, the framed half (after #transcript): see
// live_probe.js. Reached only when the file changed since the parent last
// looked, so this posts the new render, then stops the load before any
// later script runs.
//
// The transcript goes only to a parent that names a card uuid this page
// holds. `postMessage` to a `file://` parent cannot be addressed to an
// origin (every file is an opaque one), so without that check any local
// HTML file could frame this page and read it. The uuids are random and
// appear nowhere but in the transcript, so naming one proves the caller
// has already read this file. Any card will do, not just the first: an
// out-of-order arrival can render ahead of the first card.
(function () {
    const probe = window.__cclProbe;
    if (!probe) return;
    const transcript = document.getElementById('transcript');
    const title = document.getElementById('title');
    const known =
        probe.token && transcript && transcript.querySelector('.message[data-uuid="' + CSS.escape(probe.token) + '"]');
    if (!known || !title) {
        window.parent.postMessage({ cclProbe: 'refused' }, '*');
    } else {
        const cards = transcript.querySelectorAll('.message[data-uuid]');
        window.parent.postMessage(
            {
                cclProbe: 'page',
                lm: document.lastModified,
                html: title.outerHTML + transcript.outerHTML,
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
