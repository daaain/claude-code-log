            // ---- minimal theme: results as transcript rows ---------------
            // Included inside the archive search page's script (minimal
            // only), next to the classic renderGroups it replaces, so it
            // shares escapeHtml / highlight / resultsEl. Each project is a
            // header line; each hit is a row in the transcript's row
            // language: a gutter (local date and time, the role), a
            // role-coloured dot on the rail, then the session and the
            // field it matched in (dim mono) above the snippet. The classic
            // hooks stay (`.search-result-item a`, `.search-result-excerpt`).
            function mnPad(n) { return (n < 10 ? '0' : '') + n; }
            const mnFull = new Intl.DateTimeFormat(undefined, {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit',
                hour12: false, timeZoneName: 'short'
            });
            function mnWhen(stamp) {
                const date = stamp ? new Date(stamp) : null;
                if (!date || isNaN(date.getTime())) return { day: '', time: '', full: '' };
                return {
                    day: date.getFullYear() + '-' + mnPad(date.getMonth() + 1) + '-' + mnPad(date.getDate()),
                    time: mnPad(date.getHours()) + ':' + mnPad(date.getMinutes()),
                    full: mnFull.format(date)
                };
            }
            // [role class, gutter label] from the hit's field group and
            // entry type — the transcript's role colours.
            function mnRole(r) {
                switch (r.field) {
                    case 'tool_input': return ['tool', 'Tool'];
                    case 'tool_result': return ['tool', 'Result'];
                    case 'thinking': return ['thinking', 'Thinking'];
                    case 'attachment': return ['note', 'Attach'];
                    case 'meta': return ['system', 'Meta'];
                }
                if (r.type === 'user') return ['user', 'User'];
                if (r.type === 'assistant') return ['assistant', 'Assistant'];
                if (r.type === 'system') return ['system', 'System'];
                return ['other', r.type || r.field || ''];
            }
            function mnRenderGroups(results, query, append) {
                if (!append) resultsEl.innerHTML = '';
                const groups = new Map();
                results.forEach((r) => {
                    if (!groups.has(r.project)) groups.set(r.project, []);
                    groups.get(r.project).push(r);
                });
                let html = '';
                groups.forEach((items, project) => {
                    html += '<div class="search-result-group mn-rgroup">' +
                        '<div class="search-result-group-title mn-rhead">' +
                        '<span class="mn-rproj">' + escapeHtml(project) + '</span>' +
                        '<span class="search-result-count">' + items.length +
                        (items.length === 1 ? ' match' : ' matches') + '</span></div>';
                    items.forEach((r) => {
                        const when = mnWhen(r.timestamp);
                        const role = mnRole(r);
                        const session = r.session_id ? String(r.session_id).slice(0, 8) : 'unknown';
                        html += '<div class="search-result-item mn-hit mn-k-' + role[0] + '">' +
                            '<a href="' + escapeHtml(r.link) + '">' +
                            '<span class="mn-hgut"' + (when.full ? ' title="' + escapeHtml(when.full) + '"' : '') + '>' +
                            '<span class="mn-hd">' + escapeHtml(when.day) + '</span>' +
                            '<span class="mn-ht">' + escapeHtml(when.time) + '</span>' +
                            '<span class="mn-hrole">' + escapeHtml(role[1]) + '</span></span>' +
                            '<span class="mn-hbody">' +
                            '<span class="search-result-session">' + escapeHtml(session) +
                            '<span class="archive-search-field">' + escapeHtml(r.field) + '</span></span>' +
                            '<span class="search-result-excerpt">' + highlight(r.snippet, query) + '</span>' +
                            '</span></a></div>';
                    });
                    html += '</div>';
                });
                resultsEl.insertAdjacentHTML('beforeend', html);
            }
