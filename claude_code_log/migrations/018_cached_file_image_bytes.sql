-- How much of each cached file is images it never holds in memory
-- Migration: 018
-- Description: Add an `image_bytes` column to `cached_files`.
-- The memory checks (the fragment-store and streaming valves, the render
-- fan-out's worker cap, the parsed-entry store) size a conversion from
-- its transcript bytes, calibrated on what a parsed entry costs per byte
-- of its line. An image's base64 is no longer held: parsing swaps it for
-- a reference (`image_store`), and drops a Read result's second copy
-- outright. On image-heavy projects those are most of the bytes (64-67%
-- of the trunk files of the two heaviest measured), so the checks
-- overestimated a conversion's footprint about threefold.
--
-- `image_bytes` is the characters of the file's own lines the parse took
-- out that way, so the checks can subtract them. Existing rows get 0,
-- which keeps the old, larger estimate until the file is parsed again.

ALTER TABLE cached_files ADD COLUMN image_bytes INTEGER NOT NULL DEFAULT 0;
