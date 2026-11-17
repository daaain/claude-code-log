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
    data BLOB NOT NULL,           -- raw bytes, not base64, not zlib
    orphaned_at REAL              -- set by a sweep that found it unused
);
ALTER TABLE messages ADD COLUMN image_refs TEXT;  -- JSON array of digests
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

It is a JSON array so SQL can expand it with `json_each` and never needs
a list of bound variables, however many digests a batch references (see
the merge and the sweep below). The cache uses no JSON functions today:
JSON1 is built into SQLite from 3.38 and compiled into CPython's own
builds before that, but the floor is 3.35 (`MIN_SQLITE_VERSION`), so the
migration checks `json_each` works once, next to the version check, and
fails with a clear message rather than mid-merge.

### Write path

`CacheManager` serialisation (`_serialize_entry`, today
`zlib(json(entry.model_dump()))`) walks the dumped entry for
`{"type": "image", "source": {"type": "base64", "data": …}}` — in
`message.content` and nested in tool_results — and for each:

1. decodes the base64 (it is already validated where it renders; here an
   undecodable `data` is simply left inline, as today);
2. inserts it into `image_blobs`, or, if the digest is already there,
   clears its `orphaned_at` (`ON CONFLICT(digest) DO UPDATE SET
   orphaned_at = NULL`): a blob a sweep marked unused is in use again,
   and its grace period must start over if it is orphaned later;
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
copies blobs **before** rows, in the same transaction. The spool is
`ATTACH`ed to the cache connection for the merge, so the copy is pure
SQL, with no bound digest list to outgrow SQLite's variable limit:

- **Single transaction** (≤ `CLAUDE_CODE_LOG_ATOMIC_MERGE_MB`): every
  spool blob belongs to the merge.

  ```sql
  INSERT INTO main.image_blobs (digest, media_type, data)
  SELECT digest, media_type, data FROM spool.image_blobs WHERE true
  ON CONFLICT(digest) DO UPDATE SET orphaned_at = NULL;
  ```

  (`WHERE true` is SQLite's required disambiguation for an upsert on
  `INSERT … SELECT`. The upsert, as on the write path, restarts the
  grace period of a blob the merge puts back in use.)
- **Per-file commits** under the zero-total mark: each carries exactly
  its own rows' blobs, bound by one parameter, the file:

  ```sql
  INSERT INTO main.image_blobs (digest, media_type, data)
  SELECT b.digest, b.media_type, b.data FROM spool.image_blobs b
  WHERE b.digest IN (
      SELECT j.value FROM spool.messages s, json_each(s.image_refs) j
      WHERE s.file_id = ?
  )
  ON CONFLICT(digest) DO UPDATE SET orphaned_at = NULL;
  ```

**Reads during a refresh.** While a refresh runs, the incremental
refresh reads rows through a temp `messages` view that overlays the
spool on the cache (`_create_overlay`). Entries read that way can
reference blobs that are still only in the spool, so the overlay gets a
matching temp `image_blobs` view (the cache's blobs `UNION ALL` the
spool's) and the read path resolves through it.

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
references and fetch them in one query driven by the rows' own
`image_refs` (`json_each` over the batch's rows), not a bound list.
The row read and the blob query run in **one read transaction**, so
they see the same snapshot. Otherwise a write landing between them
could replace the batch's rows, and the blob query would follow the
new references and miss blobs the rows already read still need.

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
re-parsed (a salt bump, a refresh that replaces a file's rows).

**What keeps the write path safe is the transaction, not a lock.** A
blob is inserted in the same transaction as the rows that reference it
(above), so no other connection ever sees the blob without them, or
them without it.

**Readers are the hard part.** Only the all-projects pass takes the pass
lock (`process_projects_hierarchy`; a `serve --watch` tick goes through
it), while a single-project conversion and the TUI read the cache
without it. A reader can read a row in one statement and its blobs in
the next, and with phase 2 a render resolves bytes long after the entry
was loaded. If another process rewrote that file and deleted the blobs
in between, the lookup would miss. So deletion has a grace period:

1. The sweep runs at the end of an all-projects pass, under the pass
   lock, so two sweeps never race. Single-project conversions don't
   sweep.
2. It marks every blob no row references: `orphaned_at = now`, if not
   already set. Blobs referenced again (an identical image reappeared)
   get `orphaned_at = NULL`.
3. It deletes blobs orphaned for longer than a grace period (an hour, say:
   far longer than any load-to-render gap) that are still unreferenced:

```sql
WITH live(digest) AS (
    SELECT j.value FROM messages m, json_each(m.image_refs) j
    WHERE m.image_refs IS NOT NULL
)
DELETE FROM image_blobs
WHERE orphaned_at < :now - :grace
  AND digest NOT IN (SELECT digest FROM live);
```

The partial index on `image_refs` keeps the live scan to rows that hold
images. A sweep per pass, not per refresh, keeps it out of the hot path.
SQLite reuses the space freed; the existing WAL truncation at the end of
a pass is unchanged. `VACUUM` stays manual.

**A miss still has to be survivable**, since a grace period bounds
the window rather than closing it: a process left suspended for longer
than the grace period, say. What a miss means differs by phase:

- **Phase 1, at load time.** The read path's fallback (see Effort and
  risk) treats a missing blob as "file not cached" and re-parses the
  file from the JSONL. That is a correct answer: the load gets the
  file's current entries.
- **Phase 2, at render time.** The entry was loaded earlier and the blob
  has since gone, which takes its row being rewritten or deleted and
  then a grace period passing. Re-parsing the file now gives its
  *current* content, which may no longer hold that image, so it cannot
  be the fallback. But the entry is then known to be out of date: its
  source changed after it was loaded. The render emits the placeholder
  for that image, finishes, and **does not record the page as fresh**:
  it skips (or deletes) the page's `html_cache` / `html_pages` row, so
  the next pass re-renders it from the current content. That has to be
  explicit: a session page's freshness (`get_stale_sessions`) keys on
  its message count, library version, combined link and theme, not on
  its source files, so a rewrite that keeps the message count would
  otherwise leave the placeholder in place. Keeping an old image alive
  for a render of already-superseded data would buy nothing, so phase 2
  doesn't pin blobs for active renders.

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
