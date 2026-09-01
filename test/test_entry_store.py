"""The per-conversion parsed-entry store (``entry_store.py``).

A watch tick materialises the same entries up to three times: the
incremental cache refresh parses each modified file from source, then the
closure load and the session-scoped render each rebuild them from the
rows the refresh has just written. The store keeps the first pass's list
and serves it to the other two (work/watch-mode.md, C14).

A store owned *across* ticks (which ``watch`` does) goes further: it pins
its entries to a byte offset plus a hash of the bytes below it, so a tick
can prove the file still starts with what it already parsed and read only
what was appended — and, when the rows are provably just those lines,
write only the new ones instead of rewriting every row.

Three layers of coverage, and the last two are the ones that matter:

- Unit tests for the store's own contract — stamp verification, copy
  isolation, budget, kill switch.
- An end-to-end equivalence test over a watch-shaped append, with the
  store on and off, on a fixture whose 170 sidechain entries exercise
  ``_integrate_agent_entries``. That transformation mutates entries **in
  place and is not idempotent** (it appends ``#agent-{id}`` to
  ``sessionId``), and today each consumer gets freshly deserialised
  objects; serving the same objects to two consumers would apply it
  twice. That is exactly what the copy isolation exists to prevent, so
  the equivalence test is its real proof.
- Parse and write equivalence for the resumable path: the byte reader
  against the text reader it replaces, a resumed parse against a fresh
  one, and — the bar a write-path change has to clear — the **cache
  rows** an append-only write leaves behind against the rows a full
  rewrite leaves, since the first bug of that kind is invisible in the
  rendered HTML.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from claude_code_log import converter
from claude_code_log.converter import convert_jsonl_to, load_transcript
from claude_code_log.entry_store import (
    ParsedEntryStore,
    entry_store_enabled,
    entry_store_forced,
    stamp_file,
)

FIXTURE_ROOT = Path(__file__).parent / "test_data" / "real_projects"
# Multi-session, and the one fixture with a substantial population of
# sidechain agent entries (170) — i.e. the mutation hazard above.
AGENT_PROJECT = FIXTURE_ROOT / "-Users-dain-workspace-coderabbit-review-helper"


def _copy_project(tmp_path: Path, source: Path = AGENT_PROJECT) -> Path:
    """Copy a fixture project, keeping its directory name (titles derive from it)."""
    work_dir = tmp_path / source.name
    shutil.copytree(source, work_dir)
    return work_dir


def _session_files(work_dir: Path) -> dict[str, bytes]:
    files = {p.name: p.read_bytes() for p in sorted(work_dir.glob("session-*.html"))}
    assert files, "conversion produced no session files"
    return files


def _append_entry(jsonl: Path, uuid: str, text: str) -> None:
    entry = {
        "type": "user",
        "timestamp": "2025-07-03T18:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": jsonl.stem,
        "version": "1.0.0",
        "uuid": uuid,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _spy_stores(monkeypatch: pytest.MonkeyPatch) -> list[ParsedEntryStore]:
    """Collect the stores a conversion creates, without changing behaviour."""
    created: list[ParsedEntryStore] = []
    original = converter._make_entry_store

    def _spy() -> Any:
        store = original()
        if store is not None:
            created.append(store)
        return store

    monkeypatch.setattr(converter, "_make_entry_store", _spy)
    return created


def _busiest_trunk(project: Path) -> Path:
    """The fixture's substantive trunk file (several others are empty)."""
    trunk = [p for p in project.glob("*.jsonl") if not p.name.startswith("agent-")]
    return max(trunk, key=lambda p: p.stat().st_size)


@pytest.fixture
def entries() -> list[Any]:
    """A real parsed entry list to put in the store."""
    parsed = load_transcript(_busiest_trunk(AGENT_PROJECT), silent=True)
    assert parsed, "fixture produced no entries"
    return parsed


class TestStoreContract:
    def test_round_trip_serves_the_held_entries(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        store = ParsedEntryStore()
        store.put(path, stamp_file(path), entries)

        served = store.get(path)
        assert served is not None
        assert len(served) == len(entries)
        assert store.hits == 1

    def test_handouts_are_independent_copies(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        """The `_integrate_agent_entries` hazard, at the unit level.

        One consumer's in-place mutation must not be visible to the next,
        or a non-idempotent transformation would be applied twice.
        """
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        store = ParsedEntryStore()
        store.put(path, stamp_file(path), entries)

        first = store.get(path)
        second = store.get(path)
        assert first is not None and second is not None
        assert first[0] is not second[0], "consumers share an object"

        before = getattr(second[0], "sessionId", None)
        first[0].sessionId = "mutated#agent-x"  # type: ignore[union-attr]
        assert getattr(second[0], "sessionId", None) == before
        assert getattr(entries[0], "sessionId", None) == before

        third = store.get(path)
        assert third is not None
        assert getattr(third[0], "sessionId", None) == before

    def test_declines_when_the_file_changed_since_the_parse(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        store = ParsedEntryStore()
        store.put(path, stamp_file(path), entries)

        path.write_text("xy", encoding="utf-8")  # a size change is enough
        assert store.get(path) is None
        assert store.misses == 1

    def test_declines_a_stale_stamp_taken_before_a_mid_parse_append(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        """A file that grew during the parse must not be served.

        The stamp is captured *before* the parse precisely so this case
        mismatches rather than serving a list its stamp misdescribes.
        """
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        stamp = stamp_file(path)
        path.write_text("x-grown-during-the-parse", encoding="utf-8")

        store = ParsedEntryStore()
        store.put(path, stamp, entries)
        assert store.get(path) is None

    def test_put_declines_without_a_stamp_or_entries(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        store = ParsedEntryStore()

        store.put(path, None, entries)
        assert store.get(path) is None

        store.put(path, stamp_file(path), [])
        assert store.get(path) is None
        assert store.held_bytes == 0

    def test_get_of_an_unknown_path_is_a_miss_not_an_error(
        self, tmp_path: Path
    ) -> None:
        store = ParsedEntryStore()
        assert store.get(tmp_path / "never-stored.jsonl") is None
        assert store.misses == 1

    def test_budget_evicts_in_insertion_order(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        first = tmp_path / "first.jsonl"
        second = tmp_path / "second.jsonl"
        first.write_text("a" * 100, encoding="utf-8")
        second.write_text("b" * 100, encoding="utf-8")

        store = ParsedEntryStore(budget_bytes=150)
        store.put(first, stamp_file(first), entries)
        store.put(second, stamp_file(second), entries)

        assert store.get(first) is None, "oldest should have been evicted"
        assert store.get(second) is not None
        assert store.held_bytes <= 150

    def test_held_prefixes_are_charged_and_evicted_too(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        """`watch` owns one store for hours — prefixes cannot grow forever."""
        first = tmp_path / "first.jsonl"
        second = tmp_path / "second.jsonl"

        store = ParsedEntryStore(budget_bytes=150)
        store.put_prefix(first, 100, b"d1", entries, set(), 1)
        assert store.held_bytes == 100

        store.put_prefix(second, 100, b"d2", entries, set(), 1)
        assert store.get_prefix(first) is None, "oldest should have been evicted"
        assert store.get_prefix(second) is not None
        assert store.held_bytes <= 150

        # Re-holding the same file replaces its charge rather than adding
        # one: a watched file is re-held on every tick that touches it.
        store.put_prefix(second, 100, b"d3", entries, set(), 1)
        assert store.held_bytes == 100
        store.drop_prefix(second)
        assert store.held_bytes == 0

    def test_a_file_larger_than_the_budget_is_declined(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        path = tmp_path / "big.jsonl"
        path.write_text("a" * 500, encoding="utf-8")
        store = ParsedEntryStore(budget_bytes=100)
        store.put(path, stamp_file(path), entries)

        assert store.get(path) is None
        assert store.declines == 1


class TestEnvironment:
    @pytest.mark.parametrize("value", ["0", "off", "false", "OFF"])
    def test_kill_switch(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", value)
        assert not entry_store_enabled()
        assert converter._make_entry_store() is None

    @pytest.mark.parametrize("value", ["", "1", "on", "anything"])
    def test_enabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", value)
        assert entry_store_enabled()
        assert converter._make_entry_store() is not None

    def test_forced_only_on_an_explicit_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", "1")
        assert entry_store_forced()
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", "")
        assert not entry_store_forced()


class TestWatchTick:
    """The shape the store exists for: a pure append over a warm cache."""

    def _grown_project(self, tmp_path: Path) -> tuple[Path, Path]:
        work_dir = _copy_project(tmp_path)
        convert_jsonl_to("html", work_dir, silent=True, write_combined=False)
        return work_dir, _busiest_trunk(work_dir)

    def test_the_store_is_used_on_an_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, the equivalence test below would pass vacuously."""
        work_dir, jsonl = self._grown_project(tmp_path)
        _append_entry(jsonl, "entry-store-append-1", "a new message arrives")

        stores = _spy_stores(monkeypatch)
        convert_jsonl_to("html", work_dir, silent=True, write_combined=False)

        assert stores, "no store was created"
        assert sum(s.hits for s in stores) > 0, (
            "the appended file was re-materialised from the cache instead of "
            "being served from the store"
        )

    def test_a_cold_conversion_stores_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residency is bounded by what changed, not by the project.

        Only ``_incremental_cache_refresh`` fills the store, so a cold
        run — and by the same token the streaming page loads, which are
        never handed one — carries no extra memory.
        """
        work_dir = _copy_project(tmp_path)
        stores = _spy_stores(monkeypatch)
        convert_jsonl_to("html", work_dir, silent=True, write_combined=False)

        assert stores, "no store was created"
        assert all(s.held_bytes == 0 for s in stores)
        assert all(s.hits == 0 for s in stores)

    def test_output_is_byte_identical_with_and_without_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two copies advance through the same append; bytes must match."""
        on_dir, on_jsonl = self._grown_project(tmp_path / "on")
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", "0")
        off_dir, off_jsonl = self._grown_project(tmp_path / "off")

        for jsonl in (on_jsonl, off_jsonl):
            _append_entry(jsonl, "entry-store-append-1", "a new message arrives")

        convert_jsonl_to("html", off_dir, silent=True, write_combined=False)
        monkeypatch.delenv("CLAUDE_CODE_LOG_ENTRY_STORE")
        convert_jsonl_to("html", on_dir, silent=True, write_combined=False)

        assert _session_files(on_dir) == _session_files(off_dir)

    def test_agent_session_ids_are_not_double_suffixed(
        self, tmp_path: Path, entries: list[Any]
    ) -> None:
        """The concrete failure a shared (uncopied) list would produce.

        ``_integrate_agent_entries`` appends ``#agent-{id}`` to
        ``sessionId`` unconditionally, so one list handed to two
        consumers — which is precisely what the closure load and the
        session-scoped render are — comes out as ``…#agent-X#agent-X``
        for the second. Asserted where the transformation happens rather
        than in the rendered page, which does not surface the synthetic
        id verbatim.
        """
        path = tmp_path / "a.jsonl"
        path.write_text("x", encoding="utf-8")
        store = ParsedEntryStore()
        store.put(path, stamp_file(path), entries)

        doubled = re.compile(r"#agent-.*#agent-")
        suffixed = 0
        for consumer in range(2):
            served = store.get(path)
            assert served is not None
            converter._integrate_agent_entries(served)
            ids = [getattr(e, "sessionId", None) for e in served]
            suffixed = sum(1 for sid in ids if sid and "#agent-" in sid)
            assert suffixed, (
                "fixture yielded no agent-suffixed session ids — this test "
                "would pass vacuously"
            )
            for sid in ids:
                assert sid is None or not doubled.search(sid), (
                    f"consumer {consumer} saw a doubly-suffixed id: {sid}"
                )

    def test_repeated_ticks_stay_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Several appends in a row, store on vs off, byte-for-byte."""
        on_dir, on_jsonl = self._grown_project(tmp_path / "on")
        monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", "0")
        off_dir, off_jsonl = self._grown_project(tmp_path / "off")
        monkeypatch.delenv("CLAUDE_CODE_LOG_ENTRY_STORE")

        for tick in range(3):
            for jsonl in (on_jsonl, off_jsonl):
                _append_entry(jsonl, f"tick-{tick}", f"message {tick}")
            monkeypatch.setenv("CLAUDE_CODE_LOG_ENTRY_STORE", "0")
            convert_jsonl_to("html", off_dir, silent=True, write_combined=False)
            monkeypatch.delenv("CLAUDE_CODE_LOG_ENTRY_STORE")
            convert_jsonl_to("html", on_dir, silent=True, write_combined=False)

            assert _session_files(on_dir) == _session_files(off_dir), (
                f"diverged on tick {tick}"
            )


# ---------------------------------------------------------------------------
# Resumable parsing and append-only writes
# ---------------------------------------------------------------------------


def _dump(entries: list[Any]) -> list[dict[str, Any]]:
    return [e.model_dump() for e in entries]


def _synthetic_project(tmp_path: Path, sessions: int = 2, lines: int = 6) -> Path:
    """A project with no subagents — the shape the append-only write covers."""
    project = tmp_path / "-Users-dain-workspace-synthetic"
    project.mkdir(parents=True)
    for s in range(sessions):
        sid = f"5eaf00d0-0000-4000-8000-00000000000{s}"
        jsonl = project / f"{sid}.jsonl"
        with jsonl.open("w", encoding="utf-8") as f:
            parent = None
            for i in range(lines):
                uuid = f"{sid[:-2]}{s}{i}"
                f.write(
                    json.dumps(
                        {
                            "type": "user" if i % 2 == 0 else "assistant",
                            "timestamp": f"2025-07-03T18:0{i}:00Z",
                            "parentUuid": parent,
                            "isSidechain": False,
                            "userType": "human",
                            "cwd": "/tmp",
                            "sessionId": sid,
                            "version": "1.0.0",
                            "uuid": uuid,
                            "message": (
                                {
                                    "role": "user",
                                    "content": [{"type": "text", "text": f"q{i}"}],
                                }
                                if i % 2 == 0
                                else {
                                    "role": "assistant",
                                    "model": "claude-opus-5",
                                    "content": [{"type": "text", "text": f"a{i}"}],
                                    "usage": {"input_tokens": 1, "output_tokens": 1},
                                }
                            ),
                        }
                    )
                    + "\n"
                )
                parent = uuid
    return project


def _message_rows(project: Path) -> list[tuple[Any, ...]]:
    """Every cached message row, in table order, content included."""
    import sqlite3

    db = project.parent / "claude-code-log-cache.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            tuple(r)
            for r in con.execute(
                """SELECT cf.file_name, m.type, m.timestamp, m.session_id,
                          m._uuid, m._parent_uuid, m.content
                   FROM messages m JOIN cached_files cf ON m.file_id = cf.id
                   ORDER BY cf.file_name, m.id"""
            )
        ]
    finally:
        con.close()


class TestByteParseEquivalence:
    """The byte reader must produce exactly what the text reader produced."""

    @pytest.mark.parametrize(
        "fixture",
        [p.name for p in sorted(AGENT_PROJECT.glob("*.jsonl")) if p.stat().st_size],
    )
    def test_whole_file(self, fixture: str) -> None:
        path = AGENT_PROJECT / fixture
        text_path = _dump(load_transcript(path, silent=True))
        byte_path = _dump(
            load_transcript(path, silent=True, entry_store=ParsedEntryStore())
        )
        assert text_path == byte_path

    def test_resumed_parse_equals_a_fresh_one(self, tmp_path: Path) -> None:
        source = _busiest_trunk(AGENT_PROJECT)
        raw = source.read_bytes()
        body = [ln for ln in raw.split(b"\n") if ln.strip()]
        work = tmp_path / source.name

        work.write_bytes(b"\n".join(body[:-3]) + b"\n")
        store = ParsedEntryStore()
        load_transcript(work, silent=True, entry_store=store)

        work.write_bytes(b"\n".join(body) + b"\n")  # the append
        resumed = _dump(load_transcript(work, silent=True, entry_store=store))
        fresh = _dump(load_transcript(work, silent=True))

        assert store.prefix_hits == 1
        assert resumed == fresh

    def test_rewritten_history_drops_the_prefix(self, tmp_path: Path) -> None:
        """A rewound/replayed session must not be resumed onto."""
        source = _busiest_trunk(AGENT_PROJECT)
        body = [ln for ln in source.read_bytes().split(b"\n") if ln.strip()]
        work = tmp_path / source.name

        work.write_bytes(b"\n".join(body[:-3]) + b"\n")
        store = ParsedEntryStore()
        load_transcript(work, silent=True, entry_store=store)

        # Same length, different history: the size/mtime check alone would
        # miss this; the prefix hash is what catches it.
        rewritten = list(body[:-3])
        rewritten[1] = rewritten[-1]
        work.write_bytes(b"\n".join(rewritten + body[-3:]) + b"\n")

        resumed = _dump(load_transcript(work, silent=True, entry_store=store))
        fresh = _dump(load_transcript(work, silent=True))
        assert store.prefix_misses == 1
        assert resumed == fresh

    def test_a_torn_final_line_is_picked_up_next_time(self, tmp_path: Path) -> None:
        """Mid-append bytes stay out of the prefix (C12)."""
        work = tmp_path / "torn.jsonl"
        entry = {
            "type": "user",
            "timestamp": "2025-07-03T18:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "torn",
            "version": "1.0.0",
            "uuid": "torn-1",
            "message": {"role": "user", "content": [{"type": "text", "text": "one"}]},
        }
        complete = json.dumps(entry) + "\n"
        second = json.dumps({**entry, "uuid": "torn-2"}) + "\n"

        work.write_text(complete + second[:20], encoding="utf-8")  # torn tail
        store = ParsedEntryStore()
        first = load_transcript(work, silent=True, entry_store=store)
        assert len(first) == 1

        work.write_text(complete + second, encoding="utf-8")  # it lands
        resumed = _dump(load_transcript(work, silent=True, entry_store=store))
        assert _dump(load_transcript(work, silent=True)) == resumed
        assert len(resumed) == 2

    def test_an_unterminated_final_line_is_not_parsed_twice(
        self, tmp_path: Path
    ) -> None:
        """A final line whose newline hasn't landed yet parses, but isn't held.

        The other half of C12: the torn tail above fails to parse, so
        holding it would be harmless. A *complete* record whose trailing
        newline hasn't been flushed yet parses fine — and its bytes are
        still below the prefix cut, so holding its entry would make the
        next tick parse the same line a second time. Two of this repo's
        own fixtures end without a trailing newline, so this is not only
        a mid-append shape.
        """
        work = tmp_path / "unterminated.jsonl"
        entry = {
            "type": "user",
            "timestamp": "2025-07-03T18:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "unterminated",
            "version": "1.0.0",
            "uuid": "u-1",
            "message": {"role": "user", "content": [{"type": "text", "text": "one"}]},
        }
        lines = [json.dumps({**entry, "uuid": f"u-{n}"}) for n in (1, 2, 3)]
        fourth = json.dumps({**entry, "uuid": "u-4"})

        # Ends mid-line: the third record is whole, its newline is not there.
        work.write_text("\n".join(lines), encoding="utf-8")
        store = ParsedEntryStore()
        first = load_transcript(work, silent=True, entry_store=store)
        assert [e.uuid for e in first] == ["u-1", "u-2", "u-3"]  # type: ignore[union-attr]

        work.write_text("\n".join(lines + [fourth]) + "\n", encoding="utf-8")
        resumed = _dump(load_transcript(work, silent=True, entry_store=store))
        assert _dump(load_transcript(work, silent=True)) == resumed


def _count_append_proposals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every time the *caller's* gate offered rows for appending.

    Separate from :func:`_count_append_only_writes` on purpose: the two
    are different layers. This one is the parse-side proof ("the rows are
    just this file's own new lines"); that one is the row-side check
    ("the table still holds what we think we wrote"). Asserting only on
    the write would let a broken gate hide behind the check.
    """
    proposals: list[int] = []
    original = converter._appended_rows

    def _spy(*args: Any, **kwargs: Any) -> Any:
        out = original(*args, **kwargs)
        if out is not None:
            proposals.append(len(out))
        return out

    monkeypatch.setattr(converter, "_appended_rows", _spy)
    return proposals


def _count_append_only_writes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every append-only cache write that actually succeeded."""
    from claude_code_log.cache import CacheManager

    calls: list[int] = []
    original = CacheManager.extend_cached_entries

    def _spy(self: Any, jsonl_path: Path, all_entries: Any, appended: Any, **kw: Any):
        result = original(self, jsonl_path, all_entries, appended, **kw)
        if result:
            calls.append(len(appended))
        return result

    monkeypatch.setattr(CacheManager, "extend_cached_entries", _spy)
    return calls


class TestAppendOnlyWrites:
    """Only the new rows are written — and the table must not tell."""

    def _tick(self, project: Path, store: Any) -> None:
        convert_jsonl_to(
            "html",
            project,
            silent=True,
            write_combined=False,
            generate_individual_sessions=True,
            entry_store=store,
        )

    def test_rows_match_a_full_rewrite_over_repeated_appends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bar for a write-path change: DB state, not just HTML."""
        extended = _count_append_only_writes(monkeypatch)
        appended = _synthetic_project(tmp_path / "appended")
        rewritten = _synthetic_project(tmp_path / "rewritten")
        store = ParsedEntryStore()
        self._tick(appended, store)
        self._tick(rewritten, None)

        for tick in range(4):
            for project in (appended, rewritten):
                target = sorted(project.glob("*.jsonl"))[0]
                _append_entry(target, f"grow-{tick}", f"message {tick}")
            self._tick(appended, store)
            self._tick(rewritten, None)

            assert _message_rows(appended) == _message_rows(rewritten), (
                f"cache rows diverged on tick {tick}"
            )
            assert _session_files(appended).keys() == _session_files(rewritten).keys()

        assert store.prefix_hits > 0, "the resumable path never engaged"
        assert extended, (
            "no append-only write happened — this equivalence test would "
            "have compared two full rewrites and proved nothing"
        )

    def test_rows_match_when_a_tick_lands_on_an_unterminated_line(
        self, tmp_path: Path
    ) -> None:
        """A tick that sees a whole record without its newline yet.

        The parse-side twin of this is in
        ``TestByteParseEquivalence``; this is the half that would show up
        in the cache, where a re-parsed line becomes a duplicate *row*
        rather than a transient duplicate entry.
        """
        appended = _synthetic_project(tmp_path / "appended")
        rewritten = _synthetic_project(tmp_path / "rewritten")
        store = ParsedEntryStore()

        def both(mutate: Any) -> None:
            for project, held in ((appended, store), (rewritten, None)):
                mutate(sorted(project.glob("*.jsonl"))[0])
                self._tick(project, held)

        both(lambda _target: None)  # cold: nothing is held yet
        both(lambda target: _append_entry(target, "grow", "a first append"))

        def torn(target: Path) -> None:  # a whole record, newline not yet
            _append_entry(target, "whole", "flushed without its newline")
            target.write_bytes(target.read_bytes().rstrip(b"\n"))

        both(torn)

        def lands(target: Path) -> None:
            with target.open("a", encoding="utf-8") as f:
                f.write("\n")
            _append_entry(target, "after", "the write that follows it")

        both(lands)

        assert store.prefix_hits > 0, "the resumable path never engaged"
        assert _message_rows(appended) == _message_rows(rewritten)

    def test_a_growing_agent_file_inserts_rows_mid_sequence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why the append-only write refuses agent-bearing transcripts.

        A trunk's cached rows are not its lines: each referenced agent's
        transcript is spliced in at its anchor. When a subagent is still
        running — the normal case under ``watch`` — that block *grows*, so
        rows land in the middle of the sequence even though the trunk file
        itself only gained lines at the end. Treating that as an append
        would leave the agent's new entries out of the cache.

        Three ticks, because the hazard needs a resumable prefix to exist
        first: cold, an append that establishes one, then an append that
        lands alongside a growing agent file.
        """
        extended = _count_append_only_writes(monkeypatch)
        proposed = _count_append_proposals(monkeypatch)
        work_dir = _copy_project(tmp_path)
        control = _copy_project(tmp_path / "control")

        def advance(project: Path, store: Any, tick: int, grow_agent: bool) -> None:
            _append_entry(
                _busiest_trunk(project), f"trunk-{tick}", f"trunk message {tick}"
            )
            if grow_agent:
                agent = project / "agent-668b5ac2.jsonl"
                assert agent.exists(), "fixture lost its agent transcript"
                _append_entry(agent, f"agent-{tick}", f"agent message {tick}")
            self._tick(project, store)

        store = ParsedEntryStore()
        self._tick(work_dir, store)
        self._tick(control, None)
        advance(work_dir, store, 0, grow_agent=False)  # establishes a prefix
        advance(control, None, 0, grow_agent=False)
        advance(work_dir, store, 1, grow_agent=True)  # the hazard
        advance(control, None, 1, grow_agent=True)

        assert store.prefix_hits > 0, "no prefix was ever resumed from"
        assert _message_rows(work_dir) == _message_rows(control)
        # The gate must refuse to *offer* these rows. Asserting only on
        # the write below would pass even with the gate gone, because the
        # row-count check then catches the bad offer — measured: it
        # proposes a wrong 96-entry slice and the check refuses it. Good
        # defence in depth, useless as a test of the gate.
        assert not proposed, (
            f"the gate offered {proposed} rows for an agent-bearing file, "
            "whose cached rows carry spliced agent blocks"
        )
        assert not extended, "an agent-bearing file took the append-only path"

    def test_extend_refuses_when_the_rows_are_not_what_we_wrote(
        self, tmp_path: Path
    ) -> None:
        """The cross-process guard: another writer changed the row count."""
        from claude_code_log.cache import CacheManager, get_library_version

        project = _synthetic_project(tmp_path)
        cache = CacheManager(project, get_library_version())
        target = sorted(project.glob("*.jsonl"))[0]
        entries = load_transcript(target, cache, silent=True)

        # Pretend the file has one more entry than the cache holds *and*
        # that only that one is new — i.e. a count the table disagrees with.
        assert not cache.extend_cached_entries(target, entries[:-2], entries[-1:])
        # And the honest case still works.
        assert cache.extend_cached_entries(target, entries + entries[-1:], entries[-1:])

    def test_the_count_and_the_insert_hold_one_write_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Why that guard needs a transaction to be a guard at all.

        Python's sqlite3 opens a transaction on the first write, not on a
        SELECT, so between the row count and the insert another writer
        sharing the cache — a second `watch`, a TUI beside one — could
        append rows the count would have refused, and both appends would
        land. Asserted by proving the lock is held *while* the append
        runs: a second connection cannot write at that moment.
        """
        import sqlite3

        from claude_code_log.cache import CacheManager, get_library_version

        project = _synthetic_project(tmp_path)
        cache = CacheManager(project, get_library_version())
        target = sorted(project.glob("*.jsonl"))[0]
        entries = load_transcript(target, cache, silent=True)

        db = project.parent / "claude-code-log-cache.db"
        outcome: list[str] = []
        original = CacheManager._append_under_lock

        def _spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            other = sqlite3.connect(db, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                outcome.append("wrote")
                other.rollback()
            except sqlite3.OperationalError as exc:
                outcome.append(str(exc))
            finally:
                other.close()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(CacheManager, "_append_under_lock", _spy)
        assert cache.extend_cached_entries(target, entries + entries[-1:], entries[-1:])
        assert outcome and "locked" in outcome[0], (
            f"another writer got in during the checked append: {outcome}"
        )


class TestSessionLoadOrdering:
    """`load_session_entries` must not reorder when its index changes.

    Migration 012's session index carries `timestamp` before `file_id`
    specifically so the planner can satisfy this query's filter *and* its
    `ORDER BY` from one index, instead of walking the whole project to
    load a single session (84.5 ms -> 7.2 ms for the twelve busiest
    sessions of a 19k-row archive).

    An index that changes the returned order would be a silent rendering
    change, and ties are not rare — real archives have tens of thousands
    of rows sharing a timestamp with another row. This pins the property
    against a fixture built to be hostile: duplicate timestamps, rows
    split across two files within one session, and NULL timestamps.
    """

    @staticmethod
    def _entry(sid: str, uuid: str, ts: Any, text: str) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": "user",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": sid,
            "version": "1.0.0",
            "uuid": uuid,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        if ts is not None:
            entry["timestamp"] = ts
        return entry

    def test_order_matches_an_explicit_timestamp_sort(self, tmp_path: Path) -> None:
        from claude_code_log.cache import CacheManager, get_library_version

        project = tmp_path / "-Users-dain-workspace-ordering"
        project.mkdir(parents=True)
        sid = "5eaf00d0-0000-4000-8000-000000000099"

        # One session, two files, interleaved and duplicated timestamps.
        first = project / f"{sid}.jsonl"
        second = project / "5eaf00d0-0000-4000-8000-000000000100.jsonl"
        with first.open("w", encoding="utf-8") as f:
            for i, ts in enumerate(
                ["2025-07-03T18:00:00Z", "2025-07-03T18:00:00Z", "2025-07-03T18:02:00Z"]
            ):
                f.write(json.dumps(self._entry(sid, f"a{i}", ts, f"a{i}")) + "\n")
        with second.open("w", encoding="utf-8") as f:
            for i, ts in enumerate(
                ["2025-07-03T18:00:00Z", "2025-07-03T18:01:00Z", "2025-07-03T18:02:00Z"]
            ):
                f.write(json.dumps(self._entry(sid, f"b{i}", ts, f"b{i}")) + "\n")

        convert_jsonl_to("html", project, silent=True, write_combined=False)
        cache = CacheManager(project, get_library_version())

        entries = cache.load_session_entries(sid)
        assert len(entries) == 6, "fixture did not land in one session"

        stamps = [getattr(e, "timestamp", None) for e in entries]
        non_null = [s for s in stamps if s]
        assert non_null == sorted(non_null), (
            f"session load came back out of timestamp order: {stamps}"
        )
        assert len({getattr(e, "uuid", None) for e in entries}) == 6

        # The index must be the one serving it — otherwise this test would
        # keep passing while the query silently went back to scanning.
        with cache._get_connection() as conn:
            plan = [
                r[-1]
                for r in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT content FROM messages "
                    "WHERE project_id = ? AND session_id = ? "
                    "ORDER BY timestamp NULLS LAST",
                    (cache._project_id, sid),
                )
            ]
        assert any("idx_messages_project_session_ts" in p for p in plan), (
            f"session load is not using its index — plan was {plan}"
        )
        assert not any("TEMP B-TREE" in p.upper() for p in plan), (
            f"the index no longer satisfies the ORDER BY — plan was {plan}"
        )


class TestHierarchyCallerOwnedStore:
    """A caller may own the store across `process_projects_hierarchy` calls.

    `watch` already does this for the single-project path, which is what
    lets a tick resume its parse from the bytes the previous tick read.
    `serve --watch` converts the *hierarchy* instead, so without a
    caller-owned store every tick built a fresh one and re-parsed each
    changed file whole.

    The hierarchy fans stale projects out over a process pool, and a
    store holding parsed entries is no use across a `spawn` — so it
    reaches only the inline conversion. That is the watch steady state:
    one live project stale per tick means `resolved_jobs == 1` and no
    pool.
    """

    def _archive(self, tmp_path: Path) -> tuple[Path, Path]:
        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        work_dir = _copy_project(projects)
        return projects, _busiest_trunk(work_dir)

    def _tick(self, projects: Path, store: Any = None) -> None:
        converter.process_projects_hierarchy(
            projects, silent=True, write_combined=False, entry_store=store
        )

    def test_a_caller_owned_store_resumes_across_ticks(self, tmp_path: Path) -> None:
        """Three ticks: cold, one that holds a prefix, one that resumes it."""
        projects, jsonl = self._archive(tmp_path)
        store = ParsedEntryStore()

        self._tick(projects, store)
        _append_entry(jsonl, "hierarchy-store-1", "a new message arrives")
        self._tick(projects, store)
        _append_entry(jsonl, "hierarchy-store-2", "and another")
        self._tick(projects, store)

        assert store.prefix_hits > 0, (
            "the third tick re-read the whole file instead of resuming from "
            "the prefix the second tick held"
        )

    def test_per_conversion_stores_cannot_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contrast that gives the test above its meaning.

        Without a caller-owned store each tick builds its own, which dies
        with the call — so no tick can ever resume from another's prefix.
        """
        projects, jsonl = self._archive(tmp_path)
        stores = _spy_stores(monkeypatch)

        self._tick(projects)
        _append_entry(jsonl, "hierarchy-store-1", "a new message arrives")
        self._tick(projects)
        _append_entry(jsonl, "hierarchy-store-2", "and another")
        self._tick(projects)

        assert stores, "no store was created"
        assert all(s.prefix_hits == 0 for s in stores)

    def test_output_is_byte_identical_with_a_caller_owned_store(
        self, tmp_path: Path
    ) -> None:
        """The bar any store change has to clear: the bytes must not move."""
        on_projects, on_jsonl = self._archive(tmp_path / "on")
        off_projects, off_jsonl = self._archive(tmp_path / "off")
        store = ParsedEntryStore()

        self._tick(on_projects, store)
        self._tick(off_projects)
        for jsonl in (on_jsonl, off_jsonl):
            _append_entry(jsonl, "hierarchy-store-1", "a new message arrives")
        self._tick(on_projects, store)
        self._tick(off_projects)

        assert _session_files(on_jsonl.parent) == _session_files(off_jsonl.parent)
