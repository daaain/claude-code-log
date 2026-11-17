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
ALTER TABLE messages ADD COLUMN image_refs TEXT;  -- space-separated digests
CREATE INDEX idx_messages_image_refs ON messages(image_refs)
    WHERE image_refs IS NOT NULL;
```

`image_refs` lists the blobs a row's entry references. It is not needed
to read an entry back (the blob holds the references too). It is what
garbage collection reads (below), and it is a column on the row, not a
junction table keyed by `messages.id`, on purpose: rows are copied
between databases (spool → cache) and their ids change on the way. A
column in `refresh_spool.MESSAGE_COLUMNS` travels with every row copy
the cache already makes, with no id to remap.

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
4. sets the row's `image_refs` to the digests it replaced.

All in the transaction that writes the rows, so a committed row never
references a missing blob.

**The refresh spool** (`refresh_spool.py`) is its own SQLite file with
its own `messages` table, built from `MESSAGE_COLUMNS`. Adding
`image_refs` there gives the spool the column, and the merge
(`CacheManager.atomic_refresh`), which copies rows by
`MESSAGE_COLUMN_NAMES`, carries it with each row whatever id the row
lands at. The spool also gets its own `image_blobs`, and the merge
copies the blobs a batch of rows references (`INSERT OR IGNORE …
SELECT … WHERE digest IN (…)`) **before** the rows, in the same
transaction. That holds for both merge shapes: the single transaction
(≤ `CLAUDE_CODE_LOG_ATOMIC_MERGE_MB`) and each per-file commit under the
zero-total mark, which carries exactly its own rows' blobs.

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
(chunked under SQLite's variable limit), driven by the rows'
`image_refs`.

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

This takes image bytes out of the render footprint **for entries loaded
from the cache**, which is most runs: incremental runs, the
session-scoped path (§ 2.12) and watch ticks render cached entries.

A **cold parse** is different. Entries parsed from JSONL hold base64
`data`; the write path converts only the serialised copy. Two options,
to decide in phase 2:

- **Leave cold parses alone.** The saving is then warm-only, and the
  first run over an archive (or a re-parse after a salt bump) keeps
  today's footprint. Simplest.
- **Convert at parse time.** A validator on `ImageSource` swaps `data`
  for a digest and parks the bytes in a per-conversion pool keyed by
  digest, which the cache write then stores from. Each distinct image is
  then held once in memory, however many spliced entries carry it, and
  worker slices carry references. It needs the pool reachable wherever
  bytes are resolved (workers included), and a rule for entries parsed
  outside a conversion (tests, the TUI, plugins).

The fragment-store and fan-out memory valves are sized from transcript
bytes, so either version lets more projects take the fast paths on warm
runs; only the second helps a cold run. Phase 2 touches the public
`ImageSource` model and the plugin contract, so it needs its own review;
phase 1 stands without it.

### Garbage collection

Blobs outlive their rows when a file is rewritten, deleted, or
re-parsed (a salt bump, a refresh that replaces a file's rows). A sweep
at the end of a pass, under the pass lock, collects the live digests
from the rows that have any (the partial index keeps that to image-
bearing rows), splitting `image_refs` into a temp table, and deletes
the rest:

```sql
DELETE FROM image_blobs
WHERE digest NOT IN (SELECT digest FROM temp.live_digests);
```

Running it once per pass, not per refresh, keeps it out of the hot path,
and running it under the pass lock means no refresh can be between
writing a blob and committing the row that references it. Space freed
is reused by SQLite; the existing WAL truncation at the end of a pass is
unchanged. `VACUUM` stays manual.

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
  without its blob. Write the blobs first in every transaction, carry
  them with exactly the rows that reference them, and give the read
  path a fallback it does not have today: `_deserialize_entry` is called
  without error handling, so a missing reference must be caught there
  and turned into "file not cached" (re-parse from the JSONL), never a
  raised error or a silently image-less entry.
