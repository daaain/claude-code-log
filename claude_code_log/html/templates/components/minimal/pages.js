// Minimal theme runtime for the project index and the archive search
// page: the Auto / Light / Dark toggle — the same code, storage key
// (`claude-code-log:theme`) and markup as the transcript pages, so a
// choice made on one page carries to every other — and local dates.
(function () {
    'use strict';
{% include 'components/minimal/scheme.js' %}

    // ---- local dates ---------------------------------------------
    // The server writes UTC dates (correct without JavaScript); this
    // rewrites them in the viewer's time zone, the job
    // timezone_converter.js does for the classic page, in the compact
    // forms the rows use. The full local date and time, with the
    // zone, becomes the tooltip.
    function pad(n) {
        return (n < 10 ? '0' : '') + n;
    }
    function parse(stamp) {
        const date = stamp ? new Date(stamp) : null;
        return date && !isNaN(date.getTime()) ? date : null;
    }
    function day(date) {
        return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
    }
    function clock(date) {
        return pad(date.getHours()) + ':' + pad(date.getMinutes());
    }
    const full = new Intl.DateTimeFormat(undefined, {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZoneName: 'short'
    });
    function span(from, to) {
        return to ? full.format(from) + ' – ' + full.format(to) : full.format(from);
    }
    // A date range: `2025-01-01 – 2025-01-15`, one date if same day.
    document.querySelectorAll('[data-mn-from]').forEach(function (el) {
        const from = parse(el.getAttribute('data-mn-from'));
        if (!from) return;
        const to = parse(el.getAttribute('data-mn-to'));
        el.textContent = to && day(to) !== day(from) ? day(from) + ' – ' + day(to) : day(from);
        el.title = span(from, to);
    });
    // A session row's gutter: its first message's local date and time.
    document.querySelectorAll('[data-mn-ts]').forEach(function (el) {
        const at = parse(el.getAttribute('data-mn-ts'));
        if (!at) return;
        const dayEl = el.querySelector('.mn-sd');
        const timeEl = el.querySelector('.mn-st');
        if (dayEl) dayEl.textContent = day(at);
        if (timeEl) timeEl.textContent = clock(at);
        el.title = span(at, parse(el.getAttribute('data-mn-to')));
    });
})();
