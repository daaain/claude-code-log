// Following from disk, the framed half (head). The parent half is in
// live_update.js; this is the only part that runs on every page load, so
// it does nothing unless this page was loaded *by a copy of itself* to
// report whether it changed: framed, with `#ccl-probe=<lastModified>~<uuid>`.
//
// A `file://` page cannot fetch anything, not even itself, but it can
// frame itself, and the framed copy can `postMessage` what it holds back
// up. Reading the whole page for every poll would cost a full parse, so
// this script sits first in <head>: when the file's mtime is still the
// one the parent last saw, it answers `same` and stops the load before
// the body is parsed (~10ms on a 6MB page). Otherwise the page parses on
// to live_probe_post.js, right after #transcript, which posts the new
// render and stops the load there.
//
// Only session pages carry these scripts (HtmlRenderer.generate,
// `follow_from_disk`), and live_update.js offers following from disk only
// where they are: the combined page holds every session's cards, so a
// page holding one session's uuid could read them all through it.
window.__cclProbeable = true;
(function () {
    const m = /[#&]ccl-probe=([^&]*)/.exec(location.hash);
    if (!m || window.parent === window) return;
    const probe = decodeURIComponent(m[1]).split('~');
    window.__cclProbe = { known: probe[0], token: probe[1] || '' };
    // Nothing the page does at start-up may run in here. `window.stop()`
    // ends the parse, but Firefox still fires DOMContentLoaded and load
    // after it; capturing them on `window` first keeps every page handler
    // (live_update.js among them) from seeing either.
    const halt = function (e) {
        e.stopImmediatePropagation();
    };
    window.addEventListener('DOMContentLoaded', halt, true);
    window.addEventListener('load', halt, true);
    // Only the mtime leaves here, and only to a parent that already knew it.
    if (document.lastModified === probe[0]) {
        window.parent.postMessage({ cclProbe: 'same', lm: document.lastModified }, '*');
        window.stop();
    }
})();
