# Restoring Archived Sessions

When you run `claude-code-log`, its output may end with a note like:

```sh
Note: 3 archived session(s) found (cleanupPeriodDays: 30 default).
```

This indicates that 3 sessions exist in the cache whose source JSONL files have been deleted. A project converted on that run also counts its own on its progress line (`project-name: 2 sessions, 3 archived (1.2s)`); the TUI shows the count for the project it's opened on.

## What Are Archived Sessions?

Archived sessions are sessions preserved in the SQLite cache (`~/.claude/projects/claude-code-log-cache.db`) even after their source JSONL files have been deleted. This happens when:

1. Claude Code automatically deletes old JSONL files based on the `cleanupPeriodDays` setting
2. You manually delete JSONL files from `~/.claude/projects/*/`
3. You archive sessions using the TUI (`a` key)

The cache stores the complete message data, so full restoration is possible.

## Preventing Automatic Deletion

Claude Code automatically deletes session logs after 30 days by default. To change this, add `cleanupPeriodDays` to your `~/.claude/settings.json`:

```json
{
  "cleanupPeriodDays": 99999
}
```

This effectively disables automatic cleanup (274 years). You can also set it to a specific number of days.

See Claude Code's [settings documentation](https://docs.anthropic.com/en/docs/claude-code/settings) for more details.

## Using the TUI to Manage Archived Sessions

The easiest way to browse and restore archived sessions is through the interactive TUI.

### Launch the TUI

```bash
claude-code-log --tui
```

### Viewing Archived Sessions

Archived sessions appear inline with current sessions, marked with an `[ARCHIVED]` prefix:

```text
┌─ Claude Code Log ─────────────────────────────────────────────────┐
│ Project: my-project (3 archived)                                  │
│ Sessions: 5 │ Messages: 456 │ Tokens: 45,230                      │
├──────────┬───────────────────────────────────────┬─────────┬──────┤
│ Session  │ Title                                 │ Start   │ Msgs │
├──────────┼───────────────────────────────────────┼─────────┼──────┤
│ xyz789   │ Current session                       │ 01-20   │ 32   │
│ abc123   │ [ARCHIVED] Fix authentication bug     │ 12-01   │ 45   │
│ def456   │ [ARCHIVED] Add user settings page     │ 11-28   │ 123  │
└──────────┴───────────────────────────────────────┴─────────┴──────┘
 [a] Archive  [r] Restore  [h] HTML  [v] View  [c] Resume  [q] Quit
```

### Archive and Restore Keys

- `a` - **Archive Session**: Deletes the JSONL file but keeps the session in cache
- `r` - **Restore JSONL**: Recreates the JSONL file from cached data

### Restore a Session

1. Navigate to the archived session (marked with `[ARCHIVED]`)
2. Press `r` to restore the session to a JSONL file
3. The session will be restored to `~/.claude/projects/{project}/{session-id}.jsonl`
4. The `[ARCHIVED]` prefix will be removed after the session list refreshes

### View Archived Sessions

You can view archived sessions as HTML or Markdown without restoring them:

- `h` - Open HTML in browser
- `m` - Open Markdown in browser
- `v` - View Markdown in embedded viewer

## Limitations

- **Message order**: Messages are ordered by timestamp, which may differ slightly from original file order for same-timestamp entries
- **Whitespace**: Original JSON formatting is not preserved (semantically identical)

## Manual SQL Approach

For advanced users, you can also query the cache database directly:

```bash
sqlite3 ~/.claude/projects/claude-code-log-cache.db
```

```sql
-- List all sessions
SELECT p.project_path, s.session_id, s.first_timestamp, s.message_count
FROM sessions s
JOIN projects p ON s.project_id = p.id
ORDER BY s.first_timestamp;

-- List a session's messages
SELECT type, timestamp, _uuid FROM messages WHERE session_id = 'your-session-id' ORDER BY timestamp;
```

`messages.content` is zlib-compressed JSON, so `sqlite3` shows it as a blob. To
export a session as JSONL, decompress it in Python:

```python
import json, sqlite3, sys, zlib

db, session_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for (blob,) in con.execute(
    "SELECT content FROM messages WHERE session_id = ? ORDER BY timestamp",
    (session_id,),
):
    print(json.dumps(json.loads(zlib.decompress(blob))))
```

To search message bodies, see
[Searching from a terminal or an agent](archive-search.md#searching-from-a-terminal-or-an-agent).
