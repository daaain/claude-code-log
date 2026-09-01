#!/usr/bin/env python3
"""The cached-content version, and why file freshness alone is not enough.

``messages.content`` holds ``zlib(json(entry.model_dump()))``, so a cached
blob is only as complete as the model that produced it. Freshness used to key
solely on source mtime, size and the sidecar fingerprint — all properties of
the file — so adding a field to a transcript model left every *unchanged*
transcript serving a blob written without it, with nothing to notice.

Issue #320 is that bug's first sighting: ``imagePasteIds`` shipped, and on
transcripts untouched since, it rehydrated as ``None`` while the JSONL carried
it — so ``[Image #N]`` placeholders with a recorded association were reported
as having none, rendered literally, and their images detached.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from claude_code_log.cache import (
    CacheManager,
    _cache_row_is_fresh,
    content_schema_version,
)
from claude_code_log.models import UserTranscriptEntry


def _entry(uuid: str, text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2025-07-03T16:15:00Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": uuid,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def _row(**overrides: object) -> sqlite3.Row:
    """A cached_files row, fresh by default."""
    fields = {
        "source_mtime": 1000.0,
        "source_size": 4096,
        "subagents_fingerprint": "",
        "content_version": content_schema_version(),
    }
    fields.update(overrides)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cols = ", ".join(fields)
    con.execute(f"CREATE TABLE t ({cols})")
    con.execute(
        f"INSERT INTO t VALUES ({', '.join('?' * len(fields))})",
        tuple(fields.values()),
    )
    return con.execute("SELECT * FROM t").fetchone()


class TestContentVersionParticipatesInFreshness:
    def test_matching_version_is_fresh(self):
        assert _cache_row_is_fresh(_row(), 1000.0, lambda: "", 4096) is True

    def test_a_different_version_is_stale(self):
        """The whole point: the file has not changed, and the row is stale
        anyway, because what we wrote about it no longer matches the shape we
        would write today."""
        assert (
            _cache_row_is_fresh(
                _row(content_version="deadbeef0000"), 1000.0, lambda: "", 4096
            )
            is False
        )

    def test_NULL_is_stale_not_accepted(self):
        """Deliberately unlike the pre-007 fingerprint and pre-011 size
        columns, which accept NULL as "no reason to think we missed
        anything". Those describe an *input*; this describes the completeness
        of our own output, so NULL means unknown-and-probably-incomplete —
        the #320 state — and must re-parse once."""
        assert (
            _cache_row_is_fresh(_row(content_version=None), 1000.0, lambda: "", 4096)
            is False
        )


class TestVersionTracksTheModels:
    def test_adding_a_field_moves_the_version(self, monkeypatch: pytest.MonkeyPatch):
        """The pin. A new field on a cached transcript model must invalidate
        cached blobs, and this asserts the mechanism rather than a habit: the
        version is *derived* from the declared field names, so nobody has to
        remember to bump anything.

        If this fails, the digest has stopped depending on the model fields
        and #320's failure mode is reachable again.
        """
        before = content_schema_version()
        content_schema_version.cache_clear()
        monkeypatch.setitem(
            UserTranscriptEntry.model_fields,
            "someNewlyAddedField",
            UserTranscriptEntry.model_fields["imagePasteIds"],
        )
        after = content_schema_version()
        content_schema_version.cache_clear()
        assert after != before, (
            "adding a field to a cached model left the content version "
            "unchanged, so existing cached blobs would keep being served "
            "without it (issue #320)"
        )

    def test_version_is_stable_across_calls(self):
        """It must not drift on its own: a digest that moved per process
        would mass-invalidate a multi-gigabyte cache on every run."""
        content_schema_version.cache_clear()
        first = content_schema_version()
        content_schema_version.cache_clear()
        assert content_schema_version() == first

    def test_version_is_short_and_opaque(self):
        v = content_schema_version()
        assert len(v) == 12 and all(c in "0123456789abcdef" for c in v)


class TestBothFreshnessEntryPointsSeeTheColumn:
    """Reaching the real SQL, which the hand-built rows above cannot.

    ``_cache_row_is_fresh`` is fed by two different queries — the per-file
    one behind ``is_file_cached()`` and the batched one behind
    ``get_modified_files()``. A row that does not select ``content_version``
    raises ``IndexError: No item with that key`` on a ``sqlite3.Row``, which
    the CLI reports as "Error converting file", so a query left un-updated
    breaks conversion outright rather than degrading. Tests that fabricate
    rows are blind to that by construction; these go through the real
    schema.
    """

    @staticmethod
    def _stamp(db: Path, value: object) -> None:
        con = sqlite3.connect(db)
        con.execute("UPDATE cached_files SET content_version = ?", (value,))
        con.commit()
        con.close()

    @pytest.fixture
    def cached(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "s1.jsonl").write_text(_entry("u1", "first"), encoding="utf-8")
        db = tmp_path / "c.db"
        cm = CacheManager(project, "test-version", db_path=db)
        cm.save_cached_entries(project / "s1.jsonl", [])
        return cm, project / "s1.jsonl", db

    def test_per_file_check_sees_a_foreign_version(self, cached):
        cm, jsonl, db = cached
        assert cm.is_file_cached(jsonl) is True, "sanity: freshly cached"
        self._stamp(db, "deadbeef0000")
        assert cm.is_file_cached(jsonl) is False

    def test_batched_check_sees_a_foreign_version(self, cached):
        cm, jsonl, db = cached
        assert cm.get_modified_files([jsonl]) == [], "sanity: freshly cached"
        self._stamp(db, "deadbeef0000")
        assert cm.get_modified_files([jsonl]) == [jsonl]

    def test_both_treat_the_migrated_NULL_as_stale(self, cached):
        """The state migration 013 leaves behind on an existing cache: the
        column exists, nothing has filled it, and every row must re-parse
        once. This is the #320 state, so both paths must report it."""
        cm, jsonl, db = cached
        self._stamp(db, None)
        assert cm.is_file_cached(jsonl) is False
        assert cm.get_modified_files([jsonl]) == [jsonl]

    def test_a_saved_row_records_the_current_version(self, cached):
        """The write paths must stamp it, or every row stays permanently
        stale and the cache never takes effect again."""
        _cm, _jsonl, db = cached
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        stored = con.execute("SELECT content_version FROM cached_files").fetchone()[0]
        con.close()
        assert stored == content_schema_version()
