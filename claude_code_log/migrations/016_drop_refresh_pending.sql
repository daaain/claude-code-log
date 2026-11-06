-- Retire the refresh_pending flag: a refresh now commits whole or not at all
-- Migration: 016
-- Description: Drop `projects.refresh_pending` (migration 015), keeping its heal.
-- A cache refresh runs as one transaction (`CacheManager.atomic_refresh`):
-- its entry rows go to a spool while it parses, and the merge at its end
-- commits them together with the session rows, the aggregates and the
-- sidecar. A refresh that stops anywhere leaves the cache as it was, so
-- the flag that marked an unfinished refresh has nothing to mark.
--
-- A project the flag still marks is one whose derived rows may predate
-- its entry rows: a cache written before 015 and upgraded through it
-- (015 marks every project it finds, since nothing else can tell), or
-- one 015's own runs left pending. Zeroing its message total keeps the
-- heal: a project whose total is zero takes the full refresh on its
-- next run, served from the cached entries since no file is modified,
-- which recomputes every derived row, the same repair at the same price.
--
-- ALTER TABLE ... DROP COLUMN needs SQLite 3.35 (2021-03).

UPDATE projects SET total_message_count = 0 WHERE refresh_pending = 1;

ALTER TABLE projects DROP COLUMN refresh_pending;
