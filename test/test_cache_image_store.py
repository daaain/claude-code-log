"""Images are held by reference, and the cache stores each one once.

Parsing swaps an image's source for a reference and holds its bytes
(`image_store`); the cache stores them in ``image_blobs``, once per distinct
image, and lets go of them in memory once that commits. Entries read back
from the cache keep the reference, and an image's bytes are read from the
cache only where it is written out.
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
from claude_code_log import image_store
from claude_code_log.cache_images import dump_entry, restore_images
from claude_code_log.image_store import (
    MIN_EXTRACTED_CHARS,
    digest_of,
    held_bytes,
    image_base64,
    image_bytes,
    inline_images,
)
from claude_code_log.models import ImageContent, ImageRef, ImageSource
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


def _inlined(entries: Any) -> str:
    """The entries' JSON with every image written out whole, as an export
    writes it."""
    dumped = [e.model_dump() for e in entries]
    inline_images(dumped)
    return json.dumps(dumped)


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
    assert late in _inlined(loaded)


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
        assert _png(10 + n) in _inlined(loaded)


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
    assert image in _inlined(entries)


def test_each_reference_keeps_its_own_media_type(project: Path) -> None:
    """Identical bytes under two media types share one blob; each source
    still reads back with its own."""
    data = _png(13)
    jpeg_labelled = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }
    _write(project / "s1.jsonl", _user("u1", "s1", None, [_image(data)]))
    _write(project / "s2.jsonl", _user("u22", "s2", None, [jpeg_labelled]))
    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)
    assert _blob_count(project) == 1

    for name, sid, media_type in (
        ("s1.jsonl", "s1", "image/png"),
        ("s2.jsonl", "s2", "image/jpeg"),
    ):
        loaded = _manager(project).load_cached_entries(project / name)
        assert loaded is not None
        dumped = json.dumps([e.model_dump() for e in loaded])
        assert f'"media_type": "{media_type}"' in dumped
        assert f'"media_type":"{media_type}"' in manager.export_session_to_jsonl(sid)[0]


def test_the_render_s_read_keeps_a_file_whose_image_is_gone(project: Path) -> None:
    """The snapshot read may not parse the file again: an image gone from
    the cache has no bytes (a placeholder), and the file's other entries
    stay."""
    path = project / "s1.jsonl"
    _write(
        path,
        _user("u1", "s1", None, [{"type": "text", "text": "kept"}]),
        _user("u22", "s1", "u1", [_image(_png(14))]),
    )
    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)
    with _db(project) as conn:
        conn.execute("DELETE FROM image_blobs")
    conn.close()

    loaded = manager.load_snapshot_entries(path)
    assert loaded is not None and len(loaded) == 2
    assert "kept" in json.dumps([e.model_dump() for e in loaded])
    image = loaded[1].message.content[0]  # type: ignore[union-attr]
    assert isinstance(image, ImageContent) and image_bytes(image.source) is None
    assert '"data": ""' in _inlined(loaded)


# ---- held by reference -------------------------------------------------------


def _text(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use(uuid: str, sid: str, parent: str) -> Dict[str, Any]:
    """An assistant's Read call, ``t1``."""
    return {
        **_user(uuid, sid, parent, []),
        "type": "assistant",
        "message": {
            "id": f"msg_{uuid}",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/a.png"},
                }
            ],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _tool_result_user(uuid: str, sid: str, data: str) -> Dict[str, Any]:
    return _user(
        uuid,
        sid,
        None,
        [{"type": "tool_result", "tool_use_id": "t1", "content": [_image(data)]}],
    )


class TestParsedEntriesHoldReferences:
    def test_an_image_is_a_reference_whose_bytes_are_held(self, project: Path) -> None:
        data = _png(20)
        path = project / "s1.jsonl"
        _write(path, _user("u1", "s1", None, [_image(data)]))

        (entry,) = load_transcript(path, silent=True)
        image = entry.message.content[0]  # type: ignore[union-attr]
        assert isinstance(image, ImageContent)
        assert isinstance(image.source, ImageRef)
        assert image.source.media_type == "image/png"
        assert image.source.digest == digest_of(base64.b64decode(data))
        assert image_base64(image.source) == data
        # The entry itself carries none of the base64.
        assert data not in json.dumps(entry.model_dump())

    def test_a_tool_result_s_image_is_a_reference_too(self, project: Path) -> None:
        data = _png(21)
        path = project / "s1.jsonl"
        _write(path, _tool_result_user("u1", "s1", data))

        (entry,) = load_transcript(path, silent=True)
        result = entry.message.content[0]  # type: ignore[union-attr]
        source = result.content[0]["source"]  # type: ignore[union-attr,index]
        assert source["type"] == "ref"
        assert image_base64(source) == data

    def test_a_small_image_stays_inline(self, project: Path) -> None:
        tiny = "iVBORw0KGgo="
        path = project / "s1.jsonl"
        _write(path, _user("u1", "s1", None, [_image(tiny)]))

        (entry,) = load_transcript(path, silent=True)
        image = entry.message.content[0]  # type: ignore[union-attr]
        assert isinstance(image, ImageContent)
        assert isinstance(image.source, ImageSource)
        assert image.source.data == tiny


def test_the_cache_takes_the_held_bytes_and_serves_them_back(project: Path) -> None:
    """Once the cache has committed an image, memory lets go of it, and its
    bytes come back from the cache, in this process or another."""
    data = _png(22)
    digest = digest_of(base64.b64decode(data))
    _write(project / "s1.jsonl", _user("u1", "s1", None, [_image(data)]))
    manager = _manager(project)
    ensure_fresh_cache(project, manager, silent=True)

    assert held_bytes(digest) is None
    source = {"type": "ref", "digest": digest, "media_type": "image/png"}
    assert image_base64(source) == data

    # A fresh process knows the cache once it opens it.
    image_store.reset()
    assert image_bytes(source) is None
    _manager(project)
    assert image_base64(source) == data


def test_the_spool_merge_lets_go_of_the_held_bytes(project: Path) -> None:
    path = project / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_image(_png(23))]))
    ensure_fresh_cache(project, _manager(project), silent=True)

    late = _png(24)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user("u22", "s1", "u1", [_image(late)])) + "\n")
    ensure_fresh_cache(project, _manager(project), silent=True)

    assert held_bytes(digest_of(base64.b64decode(late))) is None
    assert _dangling(project) == []


def test_a_write_that_rolls_back_keeps_the_bytes_held(project: Path) -> None:
    from claude_code_log.cache import write_transaction

    data = _png(25)
    digest = digest_of(base64.b64decode(data))
    path = project / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_image(data)]))
    entries = load_transcript(path, silent=True)
    manager = _manager(project)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with manager._get_connection() as conn, write_transaction(conn):
            manager._replace_file_rows(
                conn,
                path,
                [manager._serialize_entry(e, 0) for e in entries],
                path.stat(),
                None,
            )
            raise Boom
    assert held_bytes(digest) == base64.b64decode(data)


class TestDumpEntryWithReferences:
    def test_a_held_reference_brings_its_blob(self) -> None:
        raw = base64.b64decode(_png(26))
        ref = image_store.hold_image(raw, "image/png")
        entry = {"message": {"content": [{"type": "image", "source": dict(ref)}]}}

        text, refs, blobs = dump_entry(entry)
        assert refs == [ref["digest"]]
        assert [(b.digest, b.media_type, b.data) for b in blobs] == [
            (ref["digest"], "image/png", raw)
        ]
        assert json.loads(text) == entry

    def test_a_reference_no_longer_held_is_the_cache_s_already(self) -> None:
        source = {"type": "ref", "digest": "0" * 32, "media_type": "image/png"}
        entry = {"message": {"content": [{"type": "image", "source": source}]}}

        _, refs, blobs = dump_entry(entry)
        assert refs == ["0" * 32] and blobs == []


def test_without_a_cache_the_bytes_stay_held(tmp_path: Path) -> None:
    """A single transcript converts with no cache: its images are embedded
    from the held bytes, which the conversion lets go of as it ends."""
    from claude_code_log.converter import convert_jsonl_to

    data = _png(27)
    path = tmp_path / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_text("look"), _image(data)]))
    out = convert_jsonl_to(
        "html", path, image_export_mode="embedded", silent=True, use_cache=False
    )
    assert data in out.read_text(encoding="utf-8")
    # Written out: nothing reads them again, so the conversion lets go.
    assert image_store.held_images() == {}


def test_a_warm_render_finds_exported_images_without_their_bytes(
    project: Path,
) -> None:
    """An exported image is named by the reference's digest, so rendering
    entries from the cache reuses it even when no bytes can be read at all
    (in HTML's compressed export and the plain one Markdown uses)."""
    from claude_code_log.converter import convert_jsonl_to
    from claude_code_log.renderer import get_renderer

    path = project / "s1.jsonl"
    _write(path, _user("u1", "s1", None, [_text("look"), _image(_png(28))]))
    for fmt in ("html", "md"):
        convert_jsonl_to(fmt, project, image_export_mode="referenced", silent=True)
    images = sorted(p.name for p in (project / "images").iterdir())
    assert len(images) == 2

    # The conversion's own cache, at its default place.
    loaded = CacheManager(project, get_library_version()).load_cached_entries(path)
    assert loaded is not None
    image_store.reset()
    image = loaded[0].message.content[1]  # type: ignore[union-attr]
    assert isinstance(image, ImageContent) and image_bytes(image.source) is None

    rendered = "".join(
        get_renderer(fmt, "referenced").generate(loaded, "t", output_dir=project) or ""
        for fmt in ("html", "md")
    )
    assert all(f"images/{name}" in rendered for name in images)
    assert sorted(p.name for p in (project / "images").iterdir()) == images


def test_json_export_writes_images_out_whole(tmp_path: Path) -> None:
    from claude_code_log.converter import convert_jsonl_to

    pasted, read = _png(29), _png(30)
    path = tmp_path / "s1.jsonl"
    _write(
        path,
        _user("u1", "s1", None, [_text("look"), _image(pasted)]),
        _tool_use("a1", "s1", "u1"),
        {**_tool_result_user("u22", "s1", read), "parentUuid": "a1"},
    )
    out = convert_jsonl_to("json", path, silent=True, use_cache=False)
    text = out.read_text(encoding="utf-8")
    assert pasted in text and read in text
    assert '"ref"' not in text


def test_a_render_worker_finds_the_parent_s_images(project: Path) -> None:
    """Workers are spawned: they read images from the parent's caches, and
    get any the parent holds that no cache has stored."""
    from claude_code_log.render_pool import _init_render_worker, make_render_pool

    stored, held = _png(31), _png(32)
    _write(project / "s1.jsonl", _user("u1", "s1", None, [_image(stored)]))
    ensure_fresh_cache(project, _manager(project), silent=True)
    held_ref = image_store.hold_image(base64.b64decode(held), "image/png")
    stored_ref = {
        "type": "ref",
        "digest": digest_of(base64.b64decode(stored)),
        "media_type": "image/png",
    }

    pool = make_render_pool(
        format="html",
        project_dir=project,
        output_dir=project,
        from_date=None,
        to_date=None,
        depth="full",  # type: ignore[arg-type]
        compact=False,
        no_timestamps=False,
        no_recaps=False,
        image_export_mode="embedded",
        archive_search_link=None,
        library_version=get_library_version(),
        max_workers=2,
    )
    # What the pool hands its workers as it starts, without starting it.
    from dataclasses import replace

    setup = replace(
        pool._setup,
        image_caches=[str(p) for p in image_store.registered_caches()],
        held_images=image_store.held_images(),
    )
    image_store.reset()
    _init_render_worker(setup)
    assert image_base64(stored_ref) == stored
    assert image_base64(held_ref) == held


def test_plugins_read_images_through_the_public_helpers() -> None:
    from claude_code_log import plugins

    raw = base64.b64decode(_png(33))
    ref = image_store.hold_image(raw, "image/png")
    assert plugins.image_bytes(ref) == raw
    assert plugins.image_base64(ref) == base64.b64encode(raw).decode("ascii")
    assert plugins.is_safe_source(ref)
    assert not plugins.is_safe_source({**ref, "media_type": "image/svg+xml"})


class TestForgedReferences:
    """Claude Code writes images inline; a reference in a transcript is
    forged, and must neither name a file nor reach a cached image."""

    def test_a_transcript_s_reference_becomes_a_placeholder(
        self, project: Path
    ) -> None:
        # An image another session's cache holds, which the forger knows.
        stored = _png(40)
        _write(project / "s1.jsonl", _user("u1", "s1", None, [_image(stored)]))
        ensure_fresh_cache(project, _manager(project), silent=True)
        digest = digest_of(base64.b64decode(stored))
        forged = {"type": "ref", "digest": digest, "media_type": "image/png"}
        path = project / "s2.jsonl"
        _write(
            path,
            _user(
                "u2",
                "s2",
                None,
                [
                    {"type": "image", "source": dict(forged)},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "image", "source": dict(forged)}],
                    },
                ],
            ),
        )

        (entry,) = load_transcript(path, silent=True)
        dumped = json.dumps(entry.model_dump())
        assert digest not in dumped
        image = entry.message.content[0]  # type: ignore[union-attr]
        assert isinstance(image, ImageContent)
        assert image_bytes(image.source) is None

    @pytest.mark.parametrize(
        "digest", ["../../escape", "A" * 32, "0" * 31, "0" * 32 + "/x"]
    )
    def test_only_a_hex_digest_is_a_reference(self, digest: str) -> None:
        with pytest.raises(ValueError):
            ImageRef(type="ref", digest=digest, media_type="image/png")
        source = {"type": "ref", "digest": digest, "media_type": "image/png"}
        assert image_store.image_digest(source) is None
        assert image_bytes(source) is None


# ---- what the memory checks size from ----------------------------------------


def _read_result(uuid: str, sid: str, parent: Any, data: str) -> Dict[str, Any]:
    """A Read of an image: the image block, and the copy in toolUseResult."""
    return {
        **_tool_result_user(uuid, sid, data),
        "parentUuid": parent,
        "toolUseResult": {
            "type": "image",
            "file": {"base64": data, "type": "image/png", "originalSize": 1},
        },
    }


class TestResidentBytes:
    """The memory checks size a conversion from its transcript bytes less
    the image base64 a parse doesn't hold."""

    def test_take_images_counts_what_the_entry_will_not_hold(self) -> None:
        pasted, read = _png(50), _png(51)
        entry = _user("u1", "s1", None, [_image(pasted), _image("iVBORw0KGgo=")])
        assert image_store.take_images(entry) == len(pasted)
        entry = _read_result("u2", "s1", None, read)
        # The image block, and the Read copy the model drops.
        assert image_store.take_images(entry) == 2 * len(read)

    def test_the_cache_records_each_file_s_images(self, project: Path) -> None:
        from claude_code_log.converter import _resident_project_bytes
        from claude_code_log.utils import project_transcript_bytes

        pasted, read = _png(52), _png(53)
        path = project / "s1.jsonl"
        _write(
            path,
            _user("u1", "s1", None, [_text("look"), _image(pasted)]),
            _tool_use("a1", "s1", "u1"),
            _read_result("u22", "s1", "a1", read),
        )
        manager = _manager(project)
        ensure_fresh_cache(project, manager, silent=True)

        expected = len(pasted) + 2 * len(read)
        assert manager.image_bytes_by_file() == {"s1.jsonl": expected}
        assert (
            _resident_project_bytes(project, manager)
            == project_transcript_bytes(project) - expected
        )

    def test_a_refresh_through_the_spool_records_the_whole_file(
        self, project: Path
    ) -> None:
        path = project / "s1.jsonl"
        first, late = _png(54), _png(55)
        _write(path, _user("u1", "s1", None, [_text("a"), _image(first)]))
        ensure_fresh_cache(project, _manager(project), silent=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(_user("u22", "s1", "u1", [_text("b"), _image(late)])) + "\n"
            )
        manager = _manager(project)
        ensure_fresh_cache(project, manager, silent=True)
        assert manager.image_bytes_by_file() == {"s1.jsonl": len(first) + len(late)}

    def test_a_resumed_parse_counts_the_held_prefix_too(self, project: Path) -> None:
        """A watch tick parses only what was appended, from a held prefix:
        the file's count still covers all of it."""
        from claude_code_log.entry_store import ParsedEntryStore

        path = project / "s1.jsonl"
        first, late = _png(56), _png(57)
        _write(path, _user("u1", "s1", None, [_text("a"), _image(first)]))
        store = ParsedEntryStore()
        manager = _manager(project)
        load_transcript(path, manager, silent=True, entry_store=store)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(_user("u22", "s1", "u1", [_text("b"), _image(late)])) + "\n"
            )
        load_transcript(path, manager, silent=True, entry_store=store)

        assert store.prefix_hits == 1
        assert manager.image_bytes_by_file() == {"s1.jsonl": len(first) + len(late)}
        # And the store charges for the text alone.
        assert store.held_bytes == path.stat().st_size - len(first) - len(late)

    def test_the_entry_store_charges_a_file_less_its_images(self) -> None:
        from claude_code_log.entry_store import ParsedEntryStore

        store = ParsedEntryStore()
        stamp = (10_000, 0, None)
        store.put(Path("/x.jsonl"), stamp, [object()], image_chars=7_000)  # type: ignore[list-item]
        assert store.held_bytes == 3_000
        store.put(Path("/x.jsonl"), stamp, [object()])  # type: ignore[list-item]
        assert store.held_bytes == 10_000

    def test_a_file_without_a_count_is_taken_whole(self, tmp_path: Path) -> None:
        from claude_code_log.utils import resident_bytes

        a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        a.write_text("x" * 100)
        b.write_text("y" * 50)
        assert resident_bytes([a, b], {"a.jsonl": 60}) == 90
        # A stale count larger than the file never goes negative.
        assert resident_bytes([a], {"a.jsonl": 500}) == 0
