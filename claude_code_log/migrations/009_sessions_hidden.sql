-- Hidden session rows for incremental cache refresh
-- Migration: 009
-- Description: The cache historically persisted only sessions a human
-- would render (warmup-only and empty/agent-only sessions were filtered
-- before the write). The stage-4 incremental cache refresh
-- (work/render-format-once.md) computes project aggregates by *delta*
-- over per-session rows, which requires every session's contribution to
-- be on record — including the filtered ones. So the writer now persists
-- all sessions from one unfiltered compute_session_data pass, flagging
-- warmup/empty rows hidden=1, and every read site that means "sessions a
-- human would render" filters hidden=0 (preserving prior behaviour
-- byte-for-byte). Rows written before this migration simply lack hidden
-- rows; the incremental refresh detects that (a session present in the
-- messages table with no sessions row that is not newly created) and
-- declines to the full refresh, which backfills them.

ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
