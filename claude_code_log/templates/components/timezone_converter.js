// Convert timestamps to user's timezone
// This function can be called directly or will auto-run on DOMContentLoaded if included standalone
(function() {
    function convertTimestampsToLocalTimezone() {
        const timestampElements = Array.from(document.querySelectorAll('.timestamp[data-timestamp]'));

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

        // Process timestamps in batches to keep page responsive
        const batchSize = 25;
        const scheduleWork = window.requestIdleCallback || function(cb) { setTimeout(cb, 16); };

        function processBatch(startIndex) {
            const endIndex = Math.min(startIndex + batchSize, timestampElements.length);

            for (let i = startIndex; i < endIndex; i++) {
                const element = timestampElements[i];
                const rawTimestamp = element.getAttribute('data-timestamp');
                const rawTimestampEnd = element.getAttribute('data-timestamp-end');
                const duration = element.getAttribute('data-duration');

                if (!rawTimestamp) continue;

                try {
                    // Parse the ISO timestamp
                    const date = new Date(rawTimestamp);
                    if (isNaN(date.getTime())) continue; // Invalid date

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

            // Schedule next batch if there are more timestamps
            if (endIndex < timestampElements.length) {
                scheduleWork(function() {
                    processBatch(endIndex);
                });
            }
        }

        // Start processing the first batch
        scheduleWork(function() {
            processBatch(0);
        });
    }

    // Execute immediately - assumes this is included within a DOMContentLoaded handler
    convertTimestampsToLocalTimezone();
})();
