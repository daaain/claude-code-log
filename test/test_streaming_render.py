"""Byte-equivalence and gating tests for the page-granular streaming path.

On a memory-tight machine (or under ``CLAUDE_CODE_LOG_STREAMING=1``) a
paginated HTML conversion plans its pages from cached session data and
then loads/renders/drops one page's sessions at a time
(``converter._stream_paginated_conversion``) instead of loading the whole
project. The acceptance bar is the branch's usual one: byte-identical
output to the full-load path.

Layers of coverage, mirroring ``test_session_scoped_render.py``:

- A real fixture project proves the streamed pass engages and reproduces
  the full path's bytes — with the full loader monkeypatched to *raise*
  where the cache is already fresh, so a silent fallback can't make the
  comparison vacuous.
- A synthetic cross-session project pins the cross-*page* mechanics: a
  resume-replayed prefix and a fork whose junction target sits on another
  page, split across two pages deterministically.
- A file-spanning project pins the co-resident-session trap: a source
  file holding entries of two sessions on *different* pages must never
  let one page's load render the other page's session from a partial
  slice (the ``restrict_to_sessions`` guard).

The fixtures barely exercise deep coupling (the § 4.8 lesson from
work/render-format-once.md), so hash runs over real archives remain the
wider net; these keep the mechanics pinned in CI.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import convert_jsonl_to

FIXTURE_ROOT = Path(__file__).parent / "test_data" / "real_projects"
MULTI_SESSION_PROJECT = FIXTURE_ROOT / "-Users-dain-workspace-coderabbit-review-helper"

# Small enough that the two-session fixture paginates into two pages.
FIXTURE_PAGE_SIZE = 30


def _copy_project(dest_root: Path, source: Path) -> Path:
    """Copy a project under ``dest_root``, keeping its directory name.

    The rendered title derives from the directory name, so baseline and
    streamed copies must share it (their *parents* differ instead).
    """
    work_dir = dest_root / source.name
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, work_dir)
    return work_dir


def _convert(work_dir: Path, page_size: int = FIXTURE_PAGE_SIZE) -> None:
    convert_jsonl_to("html", work_dir, silent=True, page_size=page_size)


def _html_files(work_dir: Path) -> dict[str, bytes]:
    files = {p.name: p.read_bytes() for p in sorted(work_dir.glob("*.html"))}
    assert files, "conversion produced no HTML files"
    return files


def _forbid_full_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a fall-through to the full loader fail the test loudly."""

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "full load_directory_transcripts was called — the streaming "
            "path declined when the test expected it to engage"
        )

    monkeypatch.setattr(converter, "load_directory_transcripts", _fail)


def _spy_full_load(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count calls into the full loader without changing its behaviour."""
    calls: list[int] = []
    real = converter.load_directory_transcripts

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(converter, "load_directory_transcripts", _wrapped)
    return calls


@pytest.fixture()
def fixture_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture project fully converted via the full path (streaming off)."""
    monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
    work_dir = _copy_project(tmp_path / "baseline", MULTI_SESSION_PROJECT)
    _convert(work_dir)
    monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING")
    return work_dir


class TestStreamingEquivalence:
    def test_fully_streamed_conversion_is_byte_identical(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fixture_baseline: Path,
    ) -> None:
        # A virgin project converted with streaming forced: the cache
        # refresh full-loads once (stage 3 leaves ensure_fresh_cache
        # as-is), then every page and session file renders through the
        # streamed partial loads.
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        streamed_dir = _copy_project(tmp_path / "streamed", MULTI_SESSION_PROJECT)
        calls = _spy_full_load(monkeypatch)
        _convert(streamed_dir)

        assert calls == [1], (
            "expected exactly the cache-refresh full load; the render "
            "must have streamed (a second call means Phase 2 loaded)"
        )
        assert _html_files(streamed_dir) == _html_files(fixture_baseline)
        # More than one page actually exists, or this test is vacuous.
        assert (streamed_dir / "combined_transcripts_2.html").exists()

    def test_streamed_regeneration_is_byte_identical(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fixture_baseline: Path,
    ) -> None:
        baseline = _html_files(fixture_baseline)

        # Delete one page and one session file: the cache stays fresh, so
        # the streamed run must not touch the full loader at all.
        victims = [
            next(n for n in baseline if n.startswith("combined_transcripts_2")),
            next(n for n in sorted(baseline) if n.startswith("session-")),
        ]
        for name in victims:
            (fixture_baseline / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        _forbid_full_load(monkeypatch)
        _convert(fixture_baseline)

        assert _html_files(fixture_baseline) == baseline

    def test_report_counts_streamed_work(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fixture_baseline: Path,
    ) -> None:
        baseline = _html_files(fixture_baseline)
        session_files = [n for n in sorted(baseline) if n.startswith("session-")]
        (fixture_baseline / session_files[0]).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        _forbid_full_load(monkeypatch)
        report = converter.RegenerationReport()
        convert_jsonl_to(
            "html",
            fixture_baseline,
            silent=True,
            page_size=FIXTURE_PAGE_SIZE,
            report=report,
        )
        assert report.sessions_regenerated == 1
        assert report.combined_regenerated is False

        # Now a page: combined work is reported, session count stays 0.
        page_name = next(n for n in baseline if n.startswith("combined_transcripts_2"))
        (fixture_baseline / page_name).unlink()
        report = converter.RegenerationReport()
        convert_jsonl_to(
            "html",
            fixture_baseline,
            silent=True,
            page_size=FIXTURE_PAGE_SIZE,
            report=report,
        )
        assert report.combined_regenerated is True
        assert report.sessions_regenerated == 0
        assert _html_files(fixture_baseline) == baseline


class TestStreamingGating:
    def test_kill_switch_forces_full_path(
        self, monkeypatch: pytest.MonkeyPatch, fixture_baseline: Path
    ) -> None:
        baseline = _html_files(fixture_baseline)
        name = next(n for n in baseline if n.startswith("combined_transcripts_2"))
        (fixture_baseline / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
        calls = _spy_full_load(monkeypatch)
        _convert(fixture_baseline)

        assert calls, "kill switch should have routed through the full loader"
        assert _html_files(fixture_baseline)[name] == baseline[name]

    def test_auto_mode_streams_dense_work_only_when_memory_is_tight(
        self, monkeypatch: pytest.MonkeyPatch, fixture_baseline: Path
    ) -> None:
        from claude_code_log import render_pool

        baseline = _html_files(fixture_baseline)
        name = next(n for n in baseline if n.startswith("combined_transcripts_2"))

        # Plenty of memory: one stale page of the fixture's two is half
        # the plan — over the sparse threshold, so the full path runs
        # (sparse work on a roomy machine is TestSparseGate's territory).
        monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING", raising=False)
        monkeypatch.setattr(render_pool, "available_memory_bytes", lambda: 1 << 40)
        (fixture_baseline / name).unlink()
        calls = _spy_full_load(monkeypatch)
        _convert(fixture_baseline)
        assert calls, "roomy memory must keep the full-load path for dense work"
        assert _html_files(fixture_baseline)[name] == baseline[name]

        # Tight memory: the streamed path takes over, full loader untouched.
        monkeypatch.setattr(render_pool, "available_memory_bytes", lambda: 1)
        (fixture_baseline / name).unlink()
        _forbid_full_load(monkeypatch)
        _convert(fixture_baseline)
        assert _html_files(fixture_baseline)[name] == baseline[name]

    def test_declines_without_sidecar(
        self, monkeypatch: pytest.MonkeyPatch, fixture_baseline: Path
    ) -> None:
        baseline = _html_files(fixture_baseline)
        cache = CacheManager(fixture_baseline, get_library_version())
        with cache._get_connection() as conn:  # pyright: ignore[reportPrivateUsage]
            conn.execute("DELETE FROM sidecar_state")
            conn.commit()

        name = next(n for n in baseline if n.startswith("combined_transcripts_2"))
        (fixture_baseline / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        calls = _spy_full_load(monkeypatch)
        _convert(fixture_baseline)

        assert calls, "missing sidecar must decline to the full loader"
        assert _html_files(fixture_baseline)[name] == baseline[name]

    def test_non_paginated_project_never_streams(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the default page size the fixture fits one combined file:
        # not paginated, so the streaming gate never opens even forced.
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        work_dir = _copy_project(tmp_path / "single", MULTI_SESSION_PROJECT)
        calls = _spy_full_load(monkeypatch)
        convert_jsonl_to("html", work_dir, silent=True)
        assert len(calls) >= 2, (
            "expected cache refresh + Phase 2 full load on the non-paginated path"
        )
        assert (work_dir / "combined_transcripts.html").exists()
        assert not (work_dir / "combined_transcripts_2.html").exists()


class TestStreamingPageMaintenance:
    def test_page_size_change_regenerates_all_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_baseline: Path
    ) -> None:
        # Full-path baseline at the new page size, for comparison.
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
        resized = _copy_project(tmp_path / "resized", MULTI_SESSION_PROJECT)
        _convert(resized, page_size=20)
        expected = _html_files(resized)

        # Streamed run over the old-page-size project.
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        _forbid_full_load(monkeypatch)
        _convert(fixture_baseline, page_size=20)

        assert _html_files(fixture_baseline) == expected

    def test_new_session_streams_after_cache_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prepare two identical copies, then add a session to both. The
        # streamed copy converts with exactly one full load (the cache
        # refresh, which also re-persists the sidecar) — the render itself
        # must stream and still match the full path's bytes.
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
        full_dir = _copy_project(tmp_path / "full", MULTI_SESSION_PROJECT)
        streamed_dir = _copy_project(tmp_path / "streamed", MULTI_SESSION_PROJECT)
        _convert(full_dir)
        _convert(streamed_dir)

        new_session = _make_session("sess-new", "2025-07-02T09:00:00.000Z", 4)
        for project in (full_dir, streamed_dir):
            _write_jsonl(project / "sess-new.jsonl", new_session)

        _convert(full_dir)

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        calls = _spy_full_load(monkeypatch)
        _convert(streamed_dir)

        assert calls == [1], "expected only the cache-refresh full load"
        assert _html_files(streamed_dir) == _html_files(full_dir)
        assert (streamed_dir / "session-sess-new.html").exists()


# ---------------------------------------------------------------------------
# Synthetic projects: cross-page coupling and the file-spanning trap.
# ---------------------------------------------------------------------------


def _entry(
    kind: str,
    session: str,
    uuid: str,
    parent: str | None,
    ts: str,
    text: str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": kind,
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session,
        "version": "1.0.0",
        "uuid": uuid,
    }
    if kind == "user":
        base["message"] = {"role": "user", "content": [{"type": "text", "text": text}]}
    else:
        base["requestId"] = f"req_{uuid}"
        base["message"] = {
            "id": uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    return base


def _make_session(sid: str, first_ts: str, count: int) -> list[dict[str, Any]]:
    """A simple linear session of ``count`` alternating entries.

    ``first_ts`` must sit on a whole hour; entries step one minute apart
    (``count`` < 60).
    """
    entries: list[dict[str, Any]] = []
    prev: str | None = None
    for i in range(count):
        uuid = f"{sid}-u{i}"
        ts = f"{first_ts[:14]}{i:02d}:00.000Z"
        kind = "user" if i % 2 == 0 else "assistant"
        entries.append(_entry(kind, sid, uuid, prev, ts, f"{sid} message {i}"))
        prev = uuid
    return entries


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


@pytest.fixture()
def cross_page_project_source(tmp_path: Path) -> Path:
    """Four sessions whose cross-session couplings span the page split.

    With ``page_size=4`` the assignment is page 1 = [sess-a, sess-b],
    page 2 = [sess-c, sess-d]:

    - ``sess-b`` resumes A (replays ``a2``; whole-project winner is A).
    - ``sess-c`` forks from A's ``a3`` — the junction on page 1 targets a
      session on page 2, and C's parent lives on page 1.
    - ``sess-d`` forks from C — its depth chain crosses back to A.
    """
    source = tmp_path / "source" / "cross-page-project"
    source.mkdir(parents=True)
    _write_jsonl(
        source / "sess-a.jsonl",
        [
            _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "Start"),
            _entry(
                "assistant", "sess-a", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-a", "a3", "a2", "2025-07-01T10:02:00.000Z", "More"),
        ],
    )
    _write_jsonl(
        source / "sess-b.jsonl",
        [
            _entry(
                "assistant", "sess-b", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-b", "b1", "a2", "2025-07-01T11:00:00.000Z", "Resumed"),
            _entry(
                "assistant",
                "sess-b",
                "b2",
                "b1",
                "2025-07-01T11:01:00.000Z",
                "Resumed reply",
            ),
        ],
    )
    _write_jsonl(
        source / "sess-c.jsonl",
        [
            _entry("user", "sess-c", "c1", "a3", "2025-07-01T12:00:00.000Z", "Forked"),
            _entry(
                "assistant",
                "sess-c",
                "c2",
                "c1",
                "2025-07-01T12:01:00.000Z",
                "Fork reply",
            ),
        ],
    )
    _write_jsonl(
        source / "sess-d.jsonl",
        [
            _entry("user", "sess-d", "d1", "c2", "2025-07-01T13:00:00.000Z", "Deeper"),
            _entry(
                "assistant",
                "sess-d",
                "d2",
                "d1",
                "2025-07-01T13:01:00.000Z",
                "Deep reply",
            ),
        ],
    )
    return source


class TestCrossPageMechanics:
    PAGE_SIZE = 4

    def _baseline(
        self, tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
        full_dir = _copy_project(tmp_path / "full", source)
        streamed_dir = _copy_project(tmp_path / "streamed", source)
        _convert(full_dir, page_size=self.PAGE_SIZE)
        _convert(streamed_dir, page_size=self.PAGE_SIZE)
        # The coupling only means anything if the split really happened.
        assert (full_dir / "combined_transcripts_2.html").exists()
        return full_dir, streamed_dir

    @pytest.mark.parametrize(
        "stale",
        [
            ["combined_transcripts.html"],  # junction to a page-2 session
            ["combined_transcripts_2.html"],  # parents on page 1
            ["session-sess-b.html", "combined_transcripts_2.html"],
            [
                "combined_transcripts.html",
                "combined_transcripts_2.html",
                "session-sess-a.html",
                "session-sess-d.html",
            ],
        ],
    )
    def test_streamed_regeneration_matches_full(
        self,
        tmp_path: Path,
        cross_page_project_source: Path,
        monkeypatch: pytest.MonkeyPatch,
        stale: list[str],
    ) -> None:
        full_dir, streamed_dir = self._baseline(
            tmp_path, cross_page_project_source, monkeypatch
        )
        baseline = _html_files(full_dir)
        assert _html_files(streamed_dir) == baseline

        for name in stale:
            (streamed_dir / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        _forbid_full_load(monkeypatch)
        _convert(streamed_dir, page_size=self.PAGE_SIZE)

        assert _html_files(streamed_dir) == baseline


@pytest.fixture()
def spanning_file_project_source(tmp_path: Path) -> Path:
    """Two sessions on different pages sharing a source file.

    ``sess-a.jsonl`` holds all of A *plus the first entry of E* (a
    continuation written into the previous session's file — the real
    archive shape discovery 1 of phase 8 documented); ``sess-e.jsonl``
    holds the rest of E. With ``page_size=2``, A is page 1 and E page 2,
    so page 1's load carries a partially-loaded E.
    """
    source = tmp_path / "source" / "spanning-file-project"
    source.mkdir(parents=True)
    _write_jsonl(
        source / "sess-a.jsonl",
        [
            _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "Start"),
            _entry(
                "assistant", "sess-a", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-a", "a3", "a2", "2025-07-01T10:02:00.000Z", "More"),
            # E begins here, in A's file.
            _entry("user", "sess-e", "e1", None, "2025-07-01T14:00:00.000Z", "New one"),
        ],
    )
    _write_jsonl(
        source / "sess-e.jsonl",
        [
            _entry(
                "assistant", "sess-e", "e2", "e1", "2025-07-01T14:01:00.000Z", "Go on"
            ),
            _entry("user", "sess-e", "e3", "e2", "2025-07-01T14:02:00.000Z", "Done"),
        ],
    )
    return source


class TestFileSpanningSessions:
    PAGE_SIZE = 2

    def test_both_sessions_stale_never_renders_a_truncated_session(
        self,
        tmp_path: Path,
        spanning_file_project_source: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
        full_dir = _copy_project(tmp_path / "full", spanning_file_project_source)
        streamed_dir = _copy_project(
            tmp_path / "streamed", spanning_file_project_source
        )
        _convert(full_dir, page_size=self.PAGE_SIZE)
        _convert(streamed_dir, page_size=self.PAGE_SIZE)
        baseline = _html_files(full_dir)
        assert _html_files(streamed_dir) == baseline
        assert (full_dir / "combined_transcripts_2.html").exists()

        # Page 1 AND both session files stale. The stale combined page is
        # load-bearing: with only sessions stale, the stage-2
        # session-scoped path (which loads every stale session's files
        # together) handles the run before streaming is consulted. With
        # page 1 stale the streaming loop runs, and its page-1 load
        # (sess-a.jsonl only, for A) carries E's first entry — without the
        # restrict guard the session pass renders a truncated
        # session-sess-e.html whose cache row then reads current forever
        # (verified: the mutation reproduces exactly that).
        for name in (
            "combined_transcripts.html",
            "session-sess-a.html",
            "session-sess-e.html",
        ):
            (streamed_dir / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        _forbid_full_load(monkeypatch)
        _convert(streamed_dir, page_size=self.PAGE_SIZE)

        assert _html_files(streamed_dir) == baseline


@pytest.fixture()
def many_page_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Six single-session pages, converted via the full path.

    Each session's 5 messages exceed ``page_size=4``, so every session
    closes its own page — six pages, letting the sparse gate's 1/3
    threshold land strictly between one stale page (sparse) and four
    (dense).
    """
    source = tmp_path / "many" / "many-page-project"
    source.mkdir(parents=True)
    for i in range(6):
        sid = f"sess-{i}"
        _write_jsonl(
            source / f"{sid}.jsonl",
            _make_session(sid, f"2025-07-0{i + 1}T10:00:00.000Z", 5),
        )
    monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
    convert_jsonl_to("html", source, silent=True, page_size=4)
    monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING")
    assert (source / "combined_transcripts_6.html").exists()
    return source


class TestSparseGate:
    """Auto mode on a roomy machine streams page-sparse work only.

    The benchmark behind the policy (scripts/bench_render.py, 137MB/26
    pages, 8 cores): sparse regeneration streamed at 2.0s/276MB against
    the fan-out full path's 3.3s/542MB, while a full rebuild streamed at
    11.1s against the fan-out's 6.7s — so auto mode streams below
    ``_STREAMING_MAX_SPARSE_FRACTION`` of pages needing work and declines
    to the full load + fan-out above it. (Force mode ignores the gate;
    tight memory streams regardless — the pre-existing valve.)
    """

    def _roomy_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from claude_code_log import render_pool

        monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING", raising=False)
        monkeypatch.setattr(render_pool, "available_memory_bytes", lambda: 1 << 40)

    def test_sparse_work_streams_even_with_roomy_memory(
        self, many_page_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = _html_files(many_page_project)
        self._roomy_auto(monkeypatch)

        # One stale page of six (< 1/3): the daily-run shape.
        (many_page_project / "combined_transcripts_3.html").unlink()
        _forbid_full_load(monkeypatch)
        convert_jsonl_to("html", many_page_project, silent=True, page_size=4)

        assert _html_files(many_page_project) == baseline

    def test_dense_work_declines_to_the_full_path(
        self, many_page_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = _html_files(many_page_project)
        self._roomy_auto(monkeypatch)

        # Four stale pages of six (> 1/3): a rebuild-shaped run.
        for n in ("", "_2", "_3", "_4"):
            (many_page_project / f"combined_transcripts{n}.html").unlink()
        calls = _spy_full_load(monkeypatch)
        convert_jsonl_to("html", many_page_project, silent=True, page_size=4)

        assert calls, "dense work on a roomy machine must take the full path"
        assert _html_files(many_page_project) == baseline

    def test_stale_session_counts_its_page_toward_the_gate(
        self, many_page_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stale session file with a current combined output is handled
        # by the session-scoped path before streaming is consulted — so
        # make its page stale too, and pair it with a *different* page's
        # stale session: two pages of six need work, still sparse.
        baseline = _html_files(many_page_project)
        self._roomy_auto(monkeypatch)

        (many_page_project / "combined_transcripts_2.html").unlink()
        (many_page_project / "session-sess-4.html").unlink()
        _forbid_full_load(monkeypatch)
        convert_jsonl_to("html", many_page_project, silent=True, page_size=4)

        assert _html_files(many_page_project) == baseline
