# Cache image store: images out of the entry blobs

**Status:** proposal. The first, cheap step shipped with the AVIF image
export (branch `feat/hashed-images`): Read results drop
`toolUseResult.file.base64` at parse time (`UserTranscriptEntry`
validator, `_CONTENT_SCHEMA_SALT` 2). This doc is the rest.

## Why

Measured on a real 6.2GB archive (127 projects, `.ccl-data/projects`):

| | Cache | Of which: image base64 (decompressed) |
|---|---|---|
| Before (1.8.0) | 5.57GB | 4.17GB of 7.66GB entry JSON |
| After dropping the Read copy | 4.13GB | 2.27GB of 5.76GB (1.71GB of the 3.11GB stored) |
| This proposal (estimate) | ~3.4GB | ≤ 1.03GB raw: each distinct image once |

Where the image bytes come from:

- **The rendered copy.** An `image` block in `message.content` (a pasted
  image, or a tool_result's image — mostly Read screenshots). 1.45GB of
  base64 across the archive's transcripts, 1.37GB of it distinct images:
  images rarely repeat.
- **The Read copy** (`toolUseResult.file.base64`): 1.12GB in the
  transcripts. Nothing reads it. **Gone** with the first step.
- **Splicing.** A transcript's cached rows carry its sub-agents'
  transcripts spliced in (`spliced_agent_scope`, dev-docs
  application_model § 2.3), and each `agent-*.jsonl` is cached as its own
  file too. One image-heavy project measured 44,760 rows for 25,520
  uuids — 17,520 duplicate rows. A sub-agent's images are stored once
  per file that carries them.
- **Encoding.** Base64 is 4/3 of the bytes, and zlib (`CONTENT_COMPRESSION_LEVEL`)
  gets almost nothing back from base64 of already-compressed JPEG/PNG.

So after the first step the cache still holds each rendered image
base64-encoded, zlib-wrapped for no gain, and once per file whose rows
carry it.

## Proposal

Store each distinct image once, as raw bytes, in its own table, and keep
only a reference in the entry blob.

### Schema (migration 017)

```sql
CREATE TABLE image_blobs (
    digest TEXT PRIMARY KEY,      -- blake2b-128 of the decoded bytes, hex
    media_type TEXT NOT NULL,
    data BLOB NOT NULL            -- raw bytes, not base64, not zlib
);
CREATE TABLE message_images (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    digest TEXT NOT NULL
);
CREATE INDEX message_images_digest ON message_images(digest);
CREATE INDEX message_images_message ON message_images(message_id);
```

`message_images` is what makes garbage collection cheap (below); it is
not needed to read an entry back.

### Write path

`CacheManager` serialisation (`_serialize_entry`, today
`zlib(json(entry.model_dump()))`) walks the dumped entry for
`{"type": "image", "source": {"type": "base64", "data": …}}` — in
`message.content` and nested in tool_results — and for each:

1. decodes the base64 (it is already validated where it renders; here an
   undecodable `data` is simply left inline, as today);
2. `INSERT OR IGNORE INTO image_blobs`;
3. replaces `source` with `{"type": "ref", "digest": …, "media_type": …}`
   in the dumped dict before it is JSON-encoded;
4. records `(message_id, digest)` in `message_images` once the row id is
   known.

All in the transaction that writes the rows, so a committed row never
references a missing blob.

**The refresh spool** (`refresh_spool.py`) is its own SQLite file with
its own `messages` table. It gets the same two tables, and the merge
(`CacheManager.atomic_refresh`) copies `image_blobs` with `INSERT OR
IGNORE … SELECT` before it moves the rows. Both merge shapes need it:
the single transaction (≤ `CLAUDE_CODE_LOG_ATOMIC_MERGE_MB`) and the
per-file commits under the zero-total mark. A per-file commit must carry
the blobs its rows reference in the same transaction.

**The entry store's append-resume** (§ 2.16) appends only the new rows of
a grown file. Appending goes through the same serialiser, so it inserts
any new blobs with them; nothing else changes there.

### Read path — phase 1: rehydrate on read

`_deserialize_entry` (`json.loads(zlib.decompress(...))`) walks the dict
for `"type": "ref"` sources and puts the base64 back from `image_blobs`
before `create_transcript_entry`. Every consumer downstream sees exactly
today's models: `ImageSource.type` stays `Literal["base64"]`, plugins'
`is_safe_image_source(media_type, data)` keeps working, render workers
are fed the same entries.

Fetch per batch, not per row: loads already read many rows at once
(`load_cached_entries` and friends), so collect the digests a batch
references and fetch them with one `SELECT … WHERE digest IN (…)`
(chunked under SQLite's variable limit), or a join on `message_images`.

This phase saves disk and cache I/O. It saves no RAM: entries in memory
hold the base64 as they do today.

### Read path — phase 2 (optional): lazy images

Keep the reference in the model (`ImageSource.data` optional plus a
`digest`), and resolve bytes only where an image is emitted:

- `image_export.export_compressed_image` needs the bytes only when the
  file is not on disk yet. Today its file name hashes the encoder tag
  plus the **bytes**; change it to hash the tag plus the **source
  digest**, so a reference alone finds an existing file. (A one-time
  rename of every exported image — bump `_ENCODING_REVISION`.)
- Embedded mode, Markdown and the plugin API need the bytes: resolve
  through one accessor (`image_bytes(source)`), never `source.data`.
- Render workers are fed entry slices across the process boundary
  (§ 2.10); fed references, a worker that must write a new file needs
  read access to the blob table (a read-only connection; WAL allows it).

This is what takes image bytes out of the parse/render footprint — the
fragment-store and fan-out memory valves are sized from transcript bytes,
so it would also let more projects take the fast paths. It touches the
public `ImageSource` model and the plugin contract, so it needs its own
review; phase 1 stands without it.

### Garbage collection

Blobs outlive their rows when a file is rewritten, deleted, or
re-parsed (a salt bump, a refresh that replaces a file's rows).
`message_images` cascades with `messages` (cache connections already set
`PRAGMA foreign_keys = ON`; the spool's need it too), and a sweep at the
end of a pass, under the pass lock, removes the rest:

```sql
DELETE FROM image_blobs
WHERE digest NOT IN (SELECT digest FROM message_images);
```

Indexed, this is cheap, and running it once per pass (not per refresh)
keeps it out of the hot path. Space freed is reused by SQLite; the
existing WAL truncation at the end of a pass is unchanged. `VACUUM` stays
manual.

### Cache invalidation

Bump `_CONTENT_SCHEMA_SALT` to 3 with the migration: every file is
re-parsed once and rewritten in the new shape. A pre-017 cache has no
`"ref"` sources, so the reader needs no fallback for old blobs beyond
the salt.

## Expected result

Measured on the cache the first step produced: replacing each image's
`data` with a reference shrinks the stored entry blobs from 3.11GB to
1.40GB, so images account for 1.71GB of it (base64 under zlib, once
per carrying file). Storing each distinct image once, raw, puts back at
most 1.03GB. The cache goes from 4.13GB to **~3.4GB** — another ~17%,
on top of the 26% the first step took.

That is a modest disk win for 1.5–2 days. Phase 1 is worth doing mostly
as the groundwork for phase 2, whose prize is memory: today every image
rides in every parsed entry, every fed render worker and every entry
store, at 4/3 of its size.

What is left in the cache after both is mostly text: 1.40GB of entry
blobs (themselves partly spliced duplicates), the `messages` table's
other columns and page overhead (~0.6GB), and ~0.45GB of its indexes
(`dbstat`).

## Not proposed

- **Stop splicing sub-agents into trunk rows.** It would remove the
  duplication at its source, for text as well as images, but splicing is
  what the DAG, sidecar fingerprints, watch mode and the incremental
  refresh are built on (§ 2.3, § 2.14, § 2.16). Far larger, and the
  image table gets most of the byte win without it.
- **Cache only the AVIF.** Lossy: embedded mode, Markdown and plugins
  are owed the original bytes.
- **Don't cache images; re-read them from the JSONL by byte offset.**
  The entry store pins offsets, but a rewritten source would leave the
  cache pointing at the wrong bytes — the cache exists so a run need not
  trust the source to be unchanged mid-read.

## Effort and risk

- **Phase 1:** 1.5–2 days. The serialiser/deserialiser change is small;
  the time goes on the spool + merge (both shapes), the per-file commit
  path, GC, the migration, and tests: a round trip that preserves every
  image, a merge that fails part-way leaving no dangling reference, a
  rewrite that frees its blobs, the sweep, and the existing
  cache/refresh/entry-store suites passing unchanged.
- **Phase 2:** another 1–2 days, mostly the accessor refactor and
  worker access, plus a plugin-API note.
- **Risk** concentrates in the atomic-refresh merge: a row committed
  without its blob would render a placeholder until the next re-parse.
  Write the blobs first in every transaction, and make the deserialiser
  fall back to re-parsing the file (as it does for an undecodable blob)
  when a reference is missing.
