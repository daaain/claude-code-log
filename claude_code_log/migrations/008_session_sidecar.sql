-- Session-scoped incremental rendering: cross-session sidecar
-- Migration: 008
-- Description: Persist the compact cross-session facts a full load
-- derives from the whole transcript, so a later run can regenerate a
-- stale session file from that session's own JSONL alone
-- (work/render-format-once.md, streaming stage 2). Three projections
-- of the built SessionTree, all tiny relative to entries:
--
--   session_parents  — trunk DAG-lines whose parent lives in another
--                      session (resume/fork attachment points).
--   junction_uuids   — junction points: the parent-side message uuid
--                      and its ordered target sessions.
--   dedup_winners    — uuids carried by more than one session (resume
--                      replay prefixes) and the session whose copy the
--                      whole-project dedup keeps.
--
-- sidecar_state marks that a full load has populated the sidecar for
-- the project; its absence declines the session-scoped path. Rows are
-- rewritten wholesale (delete + insert, one transaction) on every
-- full directory load, so they are exactly as fresh as cached_files.

CREATE TABLE IF NOT EXISTS session_parents (
    project_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    parent_session_id TEXT,
    attachment_uuid TEXT,
    PRIMARY KEY (project_id, session_id),
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS junction_uuids (
    project_id INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (project_id, uuid, seq),
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dedup_winners (
    project_id INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    winner_session_id TEXT NOT NULL,
    PRIMARY KEY (project_id, uuid),
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sidecar_state (
    project_id INTEGER PRIMARY KEY,
    populated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
);
