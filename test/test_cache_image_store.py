"""The cache stores each image once, outside the entry blobs.

An entry's image source is written as a reference to ``image_blobs``, which
holds the decoded bytes once per distinct image; reading the entry puts the
base64 back, so every consumer sees the entry as parsed.
"""

from __future__ import annotations

import base64
import io
import json
import random
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Dict, List

import pytest

from claude_code_log.cache import CacheManager, get_library_version, sweep_image_blobs
from claude_code_log.cache_images import (
    MIN_EXTRACTED_CHARS,
    dump_entry,
    restore_images,
)
from claude_code_log.converter import ensure_fresh_cache, load_transcript
from test.conftest import bump_mtime


def _png(seed: int, size: int = 48) -> str:
    """A noisy PNG, base64-encoded: well past the inline threshold."""
    from PIL import Image

    rng = random.Random(seed)
    img = Image.new("RGB", (size, size))
    img.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(size * size)
        ]
    )
    out = io.BytesIO()
    img.save(out, "PNG")
    data = base64.b64encode(out.getvalue()).decode("ascii")
    assert len(data) >= MIN_EXTRACTED_CHARS
    return data


def _image(data: str) -> Dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _user(uuid: str, sid: str, parent: Any, content: List[Any]) -> Dict[str, Any]:
    return {
        "type": "user",
        "timestamp": f"2025-07-01T10:00:{len(uuid):02d}Z",
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": sid,
        "version": "2.1.0",
        "uuid": uuid,
        "message": {"role": "user", "content": content},
    }


def _write(path: Path, *entries: Dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _manager(project: Path) -> CacheManager:
    return CacheManager(
        project, get_library_version(), db_path=project.parent / "cache.db"
    )


def _db(project: Path) -> sqlite3.Connection:
    return sqlite3.connect(project.parent / "cache.db")


def _blob_count(project: Path) -> int:
    with _db(project) as conn:
        count = conn.execute("SELECT COUNT(*) FROM image_blobs").fetchone()[0]
    conn.close()
    return count


def _dangling(project: Path) -> List[str]:
    """Digests some row references that the cache does not hold."""
    with _db(project) as conn:
        rows = conn.execute(
            "SELECT DISTINCT j.value FROM messages m, json_each(m.image_refs) j"
            " WHERE m.image_refs IS NOT NULL"
            " AND j.value NOT IN (SELECT digest FROM image_blobs)"
        ).fetchall()
    conn.close()
    return [r[0] for r in rows]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    return project


# ---- the swap itself -------------------------------------------------------


class TestDumpAndRestore:
    def test_round_trip_is_lossless(self) -> None:
        big = _png(1)
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "look"},
                    _image(big),
                    {
                        "type": "tool_result",
                        "content": [_image(big), _image(_png(2))],
                    },
                ]
            }
        }
        original = json.dumps(entry, separators=(",", ":"))
        text, refs, blobs = dump_entry(json.loads(original))
        assert big not in text
        assert len(refs) == 3 and len(blobs) == 2

        restored = json.loads(text)
        restore_images(restored, {b.digest: (b.media_type, b.data) for b in blobs})
        assert json.dumps(restored, separators=(",", ":")) == original

    @pytest.mark.parametrize(
        "source",
        [
            {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
            {"type": "base64", "media_type": "image/png", "data": "@" * 4096},
            {"type": "base64", "media_type": "image/png", "data": "QQ" * 2048 + "="},
            {
                "type": "base64",
                "media_type": "image/png",
                "data": "A" * 4096,
                "extra": 1,
            },
            {"type": "url", "url": "https://example.com/a.png"},
        ],
        ids=["small", "not-base64", "not-canonical", "extra-key", "url"],
    )
    def test_what_cannot_be_swapped_exactly_stays_inline(
        self, source: Dict[str, Any]
    ) -> None:
        entry = {"message": {"content": [{"type": "image", "source": source}]}}
        original = json.dumps(entry, separators=(",", ":"))
        text, refs, blobs = dump_entry(json.loads(original))
        assert (text, refs, blobs) == (original, [], [])


# ---- the cache ---------------------------------------------------------------


def test_the_cache_holds_each_image_once_and_restores_it(project: Path) -> None:
    shared = _png(3)
    _write(
        project / "s1.jsonl",
        _user("u1", "s1", None, [{"type": "text", "text": "a"}, _image(shared)]),
        _user("u22", "s1", "u1", [_image(shared), _image(_png(4))]),
    )
    _write(project / "s2.jsonl", _user("u333", "s2", None, [_image(shared)]))
    parsed = {
        path.name: [e.model_dump() for e in load_transcript(path, silent=True)]
        for path in sorted(project.glob("*.jsonl"))
    }

    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)

    assert _blob_count(project) == 2
    with _db(project) as conn:
        stored = [
            zlib.decompress(row[0]).decode("utf-8")
            for row in conn.execute("SELECT content FROM messages")
        ]
    conn.close()
    assert not any(shared in text for text in stored)
    assert _dangling(project) == []

    fresh = _manager(project)
    for path in sorted(project.glob("*.jsonl")):
        loaded = fresh.load_cached_entries(path)
        assert loaded is not None
        assert [e.model_dump() for e in loaded] == parsed[path.name]


def test_a_refresh_through_the_spool_carries_its_images(project: Path) -> None:
    """A project already cached refreshes in one merge from a spool: the
    new rows' images come with them, and nothing references a missing
    blob, whichever way the merge commits."""
    path = project / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_image(_png(5))]))
    ensure_fresh_cache(project, _manager(project), silent=True)

    late = _png(6)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user("u22", "s1", "u1", [_image(late)])) + "\n")
    _write(project / "s2.jsonl", _user("u333", "s2", None, [_image(late)]))
    ensure_fresh_cache(project, _manager(project), silent=True)

    assert _blob_count(project) == 2
    assert _dangling(project) == []
    loaded = _manager(project).load_cached_entries(path)
    assert loaded is not None
    assert late in json.dumps([e.model_dump() for e in loaded])


@pytest.mark.parametrize("merge_mb", ["0", "1000"], ids=["per-file", "at-once"])
def test_both_merge_shapes_write_blobs_before_rows(
    project: Path, monkeypatch: pytest.MonkeyPatch, merge_mb: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", merge_mb)
    for n in range(3):
        _write(
            project / f"s{n}.jsonl", _user(f"u{n}", f"s{n}", None, [_image(_png(n))])
        )
    ensure_fresh_cache(project, _manager(project), silent=True)
    for n in range(3):
        _write(
            project / f"s{n}.jsonl",
            _user(f"u{n}", f"s{n}", None, [_image(_png(10 + n))]),
        )
        bump_mtime(project / f"s{n}.jsonl")
    ensure_fresh_cache(project, _manager(project), silent=True)

    assert _dangling(project) == []
    for n in range(3):
        loaded = _manager(project).load_cached_entries(project / f"s{n}.jsonl")
        assert loaded is not None
        assert _png(10 + n) in json.dumps([e.model_dump() for e in loaded])


def test_the_sweep_deletes_only_long_unreferenced_blobs(project: Path) -> None:
    path = project / "s1.jsonl"
    old, kept = _png(7), _png(8)
    _write(path, _user("u1", "s1", None, [_image(old), _image(kept)]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    _write(path, _user("u1", "s1", None, [_image(kept)]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    db_path = project.parent / "cache.db"

    # First sight of the orphan: marked, not deleted.
    assert sweep_image_blobs(db_path, grace_seconds=0) == 0
    assert _blob_count(project) == 2
    # Marked longer than the grace period: deleted. The referenced one stays.
    assert sweep_image_blobs(db_path, grace_seconds=-1) == 1
    assert _blob_count(project) == 1
    assert _dangling(project) == []


def test_a_blob_referenced_again_is_unmarked(project: Path) -> None:
    path = project / "s1.jsonl"
    image = _png(9)
    _write(path, _user("u1", "s1", None, [_image(image)]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    _write(path, _user("u1", "s1", None, [{"type": "text", "text": "gone"}]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    db_path = project.parent / "cache.db"
    sweep_image_blobs(db_path)

    _write(path, _user("u1", "s1", None, [_image(image)]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    with _db(project) as conn:
        marks = conn.execute("SELECT orphaned_at FROM image_blobs").fetchall()
    conn.close()
    assert marks == [(None,)]
    assert sweep_image_blobs(db_path, grace_seconds=-1) == 0


def test_a_missing_blob_reads_as_not_cached(project: Path) -> None:
    path = project / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_image(_png(11))]))
    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)
    with _db(project) as conn:
        conn.execute("DELETE FROM image_blobs")
    conn.close()

    assert manager.load_cached_entries(path) is None


def test_an_archived_session_exports_its_images(project: Path) -> None:
    image = _png(12)
    _write(project / "s1.jsonl", _user("u1", "s1", None, [_image(image)]))
    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)

    lines = manager.export_session_to_jsonl("s1")
    assert len(lines) == 1 and image in lines[0]
    entries = manager.load_session_entries("s1")
    assert image in json.dumps([e.model_dump() for e in entries])
