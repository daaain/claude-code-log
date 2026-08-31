// Convert timestamps to user's timezone
// This function can be called directly or will auto-run on DOMContentLoaded if included standalone
(function() {
    // `root` scopes the work to a subtree. A live update replaces only the
    // transcript container, and its new cards carry raw ISO timestamps;
    // re-converting the whole document would redo every card that is
    // already localised (and `innerHTML` has been rewritten on those, so
    // they no longer match `[data-timestamp]`'s original text anyway).
    function convertTimestampsToLocalTimezone(root) {
        const scope = root || document;
        const timestampElements = Array.from(scope.querySelectorAll('.timestamp[data-timestamp]'));

        if (timestampElements.length === 0) return;

        const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

        const localFormatter = new Intl.DateTimeFormat(undefined, {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false,
            timeZone: userTimezone
        });

        const utcFormatter = new Intl.DateTimeFormat(undefined, {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false,
            timeZone: 'UTC'
        });

        const tzNameFormatter = new Intl.DateTimeFormat('en', {
            timeZoneName: 'short',
            timeZone: userTimezone
        });

        function localizeOne(element) {
            const rawTimestamp = element.getAttribute('data-timestamp');
            const rawTimestampEnd = element.getAttribute('data-timestamp-end');
            const duration = element.getAttribute('data-duration');

            if (!rawTimestamp) return;

            try {
                // Parse the ISO timestamp
                const date = new Date(rawTimestamp);
                if (isNaN(date.getTime())) return; // Invalid date

                const localTime = localFormatter.format(date).replace(/, /g, ' ');
                const utcTime = utcFormatter.format(date).replace(/, /g, ' ');

                // Get timezone abbreviation (reuse formatter)
                const timezoneName = tzNameFormatter.formatToParts(date).find(part => part.type === 'timeZoneName')?.value || userTimezone;

                // Handle time ranges (earliest to latest)
                if (rawTimestampEnd) {
                    const dateEnd = new Date(rawTimestampEnd);
                    if (!isNaN(dateEnd.getTime())) {
                        const localTimeEnd = localFormatter.format(dateEnd).replace(/, /g, ' ');
                        const utcTimeEnd = utcFormatter.format(dateEnd).replace(/, /g, ' ');

                        // Update the element with range
                        if (localTime !== utcTime || localTimeEnd !== utcTimeEnd) {
                            element.innerHTML = localTime + ' to ' + localTimeEnd + ' <span style="color: #888; font-size: 0.9em;">(' + timezoneName + ')</span>';
                            element.title = 'UTC: ' + utcTime + ' to ' + utcTimeEnd;
                        } else {
                            // If they're the same (user is in UTC), just show UTC
                            element.innerHTML = utcTime + ' to ' + utcTimeEnd + ' <span style="color: #888; font-size: 0.9em;">(UTC)</span>';
                            element.title = 'UTC: ' + utcTime + ' to ' + utcTimeEnd;
                        }
                    }
                } else {
                    // Single timestamp
                    if (localTime !== utcTime) {
                        element.innerHTML = localTime + ' <span style="color: #888; font-size: 0.9em;">(' + timezoneName + ')</span>';
                        element.title = duration ? duration : 'UTC: ' + utcTime;
                    } else {
                        // If they're the same (user is in UTC), just show UTC
                        element.innerHTML = utcTime + ' <span style="color: #888; font-size: 0.9em;">(UTC)</span>';
                        element.title = duration ? duration : 'UTC: ' + utcTime;
                    }
                }

            } catch (error) {
                // If conversion fails, leave the original timestamp
                console.warn('Failed to convert timestamp:', rawTimestamp, error);
            }
        }

        // Drain the queue against the idle deadline rather than a fixed batch
        // size. The work itself is cheap — a whole 4MB page's 1,180 timestamps
        // cost ~8ms of CPU — so a fixed 25-per-callback made the *callback
        // count* the cost: 48 idle turns for that page, measured at 766ms of
        // wall clock, and 3.3s for a 27MB one. Worse, the queue is in document
        // order, so cards appended by a live update localise last and the
        // fade-in plays over a raw ISO string.
        //
        // Draining on the deadline instead takes those to 13ms and 35ms —
        // within a few ms of a straight synchronous pass, while still handing
        // the main thread back whenever the browser wants it.
        const scheduleWork = window.requestIdleCallback
            ? function(cb) { window.requestIdleCallback(cb, { timeout: 200 }); }
            // No requestIdleCallback (Safari < 16): a macrotask still yields
            // between slices, and the synthetic deadline keeps them bounded.
            : function(cb) { setTimeout(function() { cb({ timeRemaining: function() { return 8; }, didTimeout: false }); }, 0); };

        let cursor = 0;
        function drain(deadline) {
            // timeRemaining() is not free, so check it per chunk rather than
            // per element; 32 conversions cost well under a millisecond.
            const chunk = 32;
            while (cursor < timestampElements.length) {
                if (!deadline.didTimeout && deadline.timeRemaining() <= 1) break;
                const end = Math.min(cursor + chunk, timestampElements.length);
                for (; cursor < end; cursor++) localizeOne(timestampElements[cursor]);
            }
            if (cursor < timestampElements.length) scheduleWork(drain);
        }

        // The first slice runs on the current task, so a live update's new
        // cards are localised before the browser paints them rather than an
        // idle turn later. It gets a real budget rather than an unbounded
        // one, so the largest pages yield instead of blocking on load.
        const firstSliceEnds = performance.now() + 24;
        drain({
            timeRemaining: function() { return Math.max(0, firstSliceEnds - performance.now()); },
            didTimeout: false
        });
    }

    // Execute immediately - assumes this is included within a DOMContentLoaded handler
    convertTimestampsToLocalTimezone();

    // Exposed so a live update can re-run it over freshly swapped-in
    // markup. Registered with the rehydrate contract when one is present
    // (transcript pages); harmless on pages without it (the index).
    window.claudeLogLocalizeTimestamps = convertTimestampsToLocalTimezone;
    if (window.claudeLogOnRehydrate) {
        window.claudeLogOnRehydrate(convertTimestampsToLocalTimezone);
    }
})();
