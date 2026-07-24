-- Migration: 008_history_commands.sql
-- Description: Add history_commands table for storing command history from ~/.claude/history.jsonl

CREATE TABLE IF NOT EXISTS history_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    timestamp_epoch INTEGER NOT NULL DEFAULT 0,
    UNIQUE(display, project, session_id, timestamp_epoch)
);

CREATE INDEX IF NOT EXISTS idx_history_commands_project ON history_commands(project);
CREATE INDEX IF NOT EXISTS idx_history_commands_session ON history_commands(session_id);
CREATE INDEX IF NOT EXISTS idx_history_commands_timestamp ON history_commands(timestamp_epoch);