-- Store each cached image once, as raw bytes, outside the entry blobs
-- Migration: 017
-- Description: `image_blobs` table and `messages.image_refs` column.
-- An entry's `{"type": "image", "source": {"type": "base64", ...}}` is
-- written to `messages.content` with a reference in place of its base64
-- (`{"type": "ref", "digest": ..., "media_type": ...}`), and the decoded
-- bytes go here once per distinct image, however many rows carry it (a
-- sub-agent's rows are also spliced into its trunk's). The reader puts the
-- base64 back. `image_refs` lists the digests a row references, as a JSON
-- array: it travels with every row copy the cache makes (spool to cache),
-- whatever id the row lands at, and SQL expands it with `json_each`.
--
-- `orphaned_at` is set by the sweep at the end of an all-projects pass
-- when no row references the blob, and cleared when one does again; a
-- blob orphaned for longer than the grace period is deleted. See
-- dev-docs/application_model.md (cache image store).
--
-- Existing rows keep their inline base64, which the reader still reads;
-- the content-schema salt bump that ships with this re-parses them.

CREATE TABLE IF NOT EXISTS image_blobs (
    digest TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    data BLOB NOT NULL,
    orphaned_at REAL
);

ALTER TABLE messages ADD COLUMN image_refs TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_image_refs ON messages(image_refs)
    WHERE image_refs IS NOT NULL;
