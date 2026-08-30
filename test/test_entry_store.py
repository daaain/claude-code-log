"""The per-conversion parsed-entry store (``entry_store.py``).

A watch tick materialises the same entries up to three times: the
incremental cache refresh parses each modified file from source, then the
closure load and the session-scoped render each rebuild them from the
rows the refresh has just written. The store keeps the first pass's list
and serves it to the other two (work/watch-mode.md, C14).

Two layers of coverage, and the second is the one that matters:

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
