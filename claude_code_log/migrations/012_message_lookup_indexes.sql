-- Let the incremental refresh's lookups use an index
-- Migration: 012
-- Description: Composite indexes on `messages` for the lookups the
-- incremental cache refresh makes on every watch tick.
--
-- All of them were scanning the whole project. `EXPLAIN QUERY PLAN` on a
-- real 38,706-row archive showed each as
--     SEARCH m USING INDEX idx_messages_project_timestamp (project_id=?)
-- i.e. walking every row the project has, to read a handful. Measured
-- per call on that archive, before -> after:
--
--   get_uuid_owners                17.2 ms -> 0.9 ms   (project_id, _uuid)
--   get_parent_uuid_dependents     21.5 ms -> 0.6 ms   (project_id, _parent_uuid)
--   get_request_id_entries         17.0 ms -> 0.3 ms   (project_id, _request_id)
--   get_metadata_target_files      14.5 ms -> 0.2 ms   partial, on type
--   get_session_file_map           16.8 ms -> 2.5 ms   (project_id, session_id, file_id)
--
-- `get_uuid_owners` is the instructive one: an `idx_messages_uuid(_uuid)`
-- index has existed since 001, but the query filters `project_id = ? AND
-- _uuid IN (...)` and SQLite uses one index per table reference, so it
-- took the project one and scanned. The composite covers both terms.
--
-- The session index below is double-edged and its callers know it: as
-- well as serving `get_session_file_map` (which must touch every session
-- anyway, and now does so as an index-only scan), it gives the planner a
-- way to satisfy a bare `session_id IS NOT NULL` as a range scan over
-- every session-bearing row — which it will happily prefer to seeking
-- the handful of uuids a query actually asked for. Three queries were
-- measurably *slower* with this index until that predicate moved out of
-- SQL and into Python; see the comment on `get_uuid_owners`.
--
-- The metadata index is partial (288 of 38,706 rows here), which is why
-- it costs 0.01% rather than the ~1% a full index on `type` would.
--
-- Cost, measured on a 49 MB archive: the cache grows 39.5 MB -> 42.8 MB
-- (**+8.4%**), and the write path — where rewriting a file's rows is a
-- watch tick's largest item — does not regress: a cold conversion goes
-- 5.85 s -> 5.79 s, because five more indexes on an INSERT are small
-- next to compressing the row's content blob. Ticks over the same
-- archive: 0.379 s -> 0.317 s (full rewrite), 0.267 s -> 0.228 s
-- (resumed).
--
-- Pure performance: no column changes, nothing to backfill, and an older
-- library reading this database simply ignores them.

CREATE INDEX IF NOT EXISTS idx_messages_project_uuid
    ON messages(project_id, _uuid);

CREATE INDEX IF NOT EXISTS idx_messages_project_parent_uuid
    ON messages(project_id, _parent_uuid);

CREATE INDEX IF NOT EXISTS idx_messages_project_request_id
    ON messages(project_id, _request_id);

CREATE INDEX IF NOT EXISTS idx_messages_project_metadata_type
    ON messages(project_id, type) WHERE type IN ('summary', 'ai-title');

CREATE INDEX IF NOT EXISTS idx_messages_project_session_file
    ON messages(project_id, session_id, file_id);
