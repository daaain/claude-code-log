-- Remember a cache refresh that has not finished
-- Migration: 015
-- Description: Add a `refresh_pending` flag to `projects`.
-- A refresh writes each transcript file's entry rows in a transaction of
-- its own, and only afterwards the session rows and project aggregates
-- derived from them. Staleness reads both halves: a file counts as
-- modified when its `cached_files` row is older than the file, a
-- rendered session as stale when its `sessions` row disagrees with the
-- `html_cache` row of its last render. A refresh that stopped between
-- the two (a write lost to "database is locked", issue #332, or a kill)
-- left the files looking current and the session rows as old as the
-- renders, so no later run saw anything to do and the missing messages
-- were never rendered.
--
-- Every entry write sets the flag, in its own transaction; writing the
-- project aggregates, the last step of a complete refresh, clears it.
-- While it is set, `get_modified_files` reports every file, so the next
-- run refreshes the whole project and re-renders what changed.
--
-- Existing rows start SET: a cache written before this migration may
-- already hold the damage, and nothing else can tell. That costs one
-- whole-project refresh per project on the first run after upgrading,
-- served from the cached entries (no file is re-parsed for it). A Codex
-- project pays more: render_provider_wholesale picks the session pages to
-- re-render from get_modified_files, so that run re-renders every page.

ALTER TABLE projects ADD COLUMN refresh_pending INTEGER NOT NULL DEFAULT 0;

UPDATE projects SET refresh_pending = 1;
