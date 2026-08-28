"""Equivalence and gating tests for the incremental cache refresh.

Streaming stage 4 (work/render-format-once.md): when source files
changed over a populated cache, ``ensure_fresh_cache`` refreshes from
the modified files' bounded closure (``_incremental_cache_refresh``)
instead of loading the whole project. The acceptance bar is stricter
than byte-identity of rendered output: the *cache database state* —
session rows (hidden ones included), project aggregates, and all three
sidecar tables — must equal what the full-load refresh writes, because
every later partial load leans on those facts.

Each scenario runs twice from identical copies: copy A with the
incremental path allowed, copy B with ``CLAUDE_CODE_LOG_INCREMENTAL_
CACHE=0`` (the full refresh). Both then compare on DB state and
rendered bytes. Engagement (or decline) is asserted via a spy so a
scenario can never pass vacuously through the wrong path.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import convert_jsonl_to

from test.test_streaming_render import _entry, _make_session, _write_jsonl

PAGE_SIZE = 6


@pytest.fixture(autouse=True)
def _pinned_env(monkeypatch: pytest.MonkeyPatch):
    # Isolate the variable under test: render via the full path on both
    # copies, and start from the incremental default (enabled).
    monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "0")
    monkeypatch.delenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", raising=False)


def _spy_incremental(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record each _incremental_cache_refresh outcome (True = handled)."""
    results: list[bool] = []
    real = converter._incremental_cache_refresh  # pyright: ignore[reportPrivateUsage]

    def wrapped(*args: Any, **kwargs: Any) -> bool:
        result = real(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(converter, "_incremental_cache_refresh", wrapped)
    return results


def _bump_mtime(path: Path) -> None:
    """Push a file's mtime past the cache's 1-second freshness tolerance.

    Real appends happen minutes after the last refresh; in a test the
    mutation lands milliseconds later, inside ``_cache_row_is_fresh``'s
    tolerance window, and would read as fresh.
    """
    import os

    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2.0))


def _base_project(root: Path) -> Path:
    """Three sessions: A with explicit uuids (fork/resume targets),
    plus two plain ones. Timestamps ascend a1 < b < c0."""
    p = root / "proj"
    p.mkdir(parents=True)
    _write_jsonl(
        p / "sess-a.jsonl",
        [
            _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "Start"),
            _entry(
                "assistant", "sess-a", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-a", "a3", "a2", "2025-07-01T10:02:00.000Z", "More"),
        ],
    )
    _write_jsonl(
        p / "sess-b.jsonl", _make_session("sess-b", "2025-07-02T10:00:00.000Z", 5)
    )
    _write_jsonl(
        p / "sess-c0.jsonl", _make_session("sess-c0", "2025-07-03T10:00:00.000Z", 5)
    )
    return p


def _convert(project: Path) -> None:
    convert_jsonl_to("html", project, silent=True, page_size=PAGE_SIZE)


def _html_files(project: Path) -> dict[str, bytes]:
    return {f.name: f.read_bytes() for f in sorted(project.glob("*.html"))}


def _db_state(project: Path) -> dict[str, Any]:
    """Comparable cache state: session rows, aggregates, sidecar tables.

    Volatile columns (autoincrement ids, project ids, timestamps of the
    write itself) are excluded; everything else must match between the
    incremental and full refreshes.
    """
    cm = CacheManager(project, get_library_version())
    with cm._get_connection() as conn:  # pyright: ignore[reportPrivateUsage]
        conn.row_factory = sqlite3.Row
        sessions = sorted(
            (
                row["session_id"],
                row["summary"],
                row["ai_title"],
                row["first_timestamp"],
                row["last_timestamp"],
                row["message_count"],
                row["first_user_message"],
                row["cwd"],
                row["total_input_tokens"],
                row["total_output_tokens"],
                row["total_cache_creation_tokens"],
                row["total_cache_read_tokens"],
                row["team_name"],
                row["hidden"],
                row["residual_count"],
            )
            for row in conn.execute("SELECT * FROM sessions")
        )
        projects = [
            (
                row["total_message_count"],
                row["total_input_tokens"],
                row["total_output_tokens"],
                row["total_cache_creation_tokens"],
                row["total_cache_read_tokens"],
                row["earliest_timestamp"],
                row["latest_timestamp"],
            )
            for row in conn.execute("SELECT * FROM projects")
        ]
        parents = sorted(
            tuple(row)
            for row in conn.execute(
                "SELECT session_id, parent_session_id, attachment_uuid"
                " FROM session_parents"
            )
        )
        junctions = sorted(
            tuple(row)
            for row in conn.execute(
                "SELECT uuid, session_id, target_session_id, seq FROM junction_uuids"
            )
        )
        winners = sorted(
            tuple(row)
            for row in conn.execute("SELECT uuid, winner_session_id FROM dedup_winners")
        )
    return {
        "sessions": sessions,
        "projects": projects,
        "parents": parents,
        "junctions": junctions,
        "winners": winners,
    }


def _run_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    *,
    expect_incremental: bool,
) -> None:
    """Build two identical copies, convert, mutate, reconvert, compare."""
    inc_dir = _base_project(tmp_path / "inc")
    full_dir = _base_project(tmp_path / "full")
    _convert(inc_dir)
    _convert(full_dir)

    mutate(inc_dir)
    mutate(full_dir)

    results = _spy_incremental(monkeypatch)
    _convert(inc_dir)
    if expect_incremental:
        assert results == [True], "incremental refresh should have handled the update"
    else:
        assert results == [] or results == [False], (
            "scenario expected to decline (or never attempt) the incremental path"
        )

    monkeypatch.setenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", "0")
    _convert(full_dir)

    assert _db_state(inc_dir) == _db_state(full_dir)
    assert _html_files(inc_dir) == _html_files(full_dir)


class TestIncrementalEquivalence:
    def test_new_session_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(p: Path) -> None:
            _write_jsonl(
                p / "sess-d.jsonl",
                _make_session("sess-d", "2025-07-04T10:00:00.000Z", 5),
            )

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)

    def test_appended_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(p: Path) -> None:
            extra = [
                _entry(
                    "user", "sess-a", "a4", "a3", "2025-07-05T10:00:00.000Z", "Again"
                ),
                _entry(
                    "assistant",
                    "sess-a",
                    "a5",
                    "a4",
                    "2025-07-05T10:01:00.000Z",
                    "Sure",
                ),
            ]
            with open(p / "sess-a.jsonl", "a", encoding="utf-8") as f:
                import json

                for e in extra:
                    f.write(json.dumps(e) + "\n")
            _bump_mtime(p / "sess-a.jsonl")

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)

    def test_resume_replay_coupling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # New session resumes A: replays a2 under its own sessionId (a
        # cross-session duplicate whose election must keep A's copy) and
        # attaches at it — closure must pull A in as dup partner and
        # attachment owner, and a junction appears on A's a2.
        def mutate(p: Path) -> None:
            _write_jsonl(
                p / "sess-r.jsonl",
                [
                    _entry(
                        "assistant",
                        "sess-r",
                        "a2",
                        "a1",
                        "2025-07-01T10:01:00.000Z",
                        "Reply",
                    ),
                    _entry(
                        "user", "sess-r", "r1", "a2", "2025-07-06T09:00:00.000Z", "Back"
                    ),
                    _entry(
                        "assistant",
                        "sess-r",
                        "r2",
                        "r1",
                        "2025-07-06T09:01:00.000Z",
                        "Hi again",
                    ),
                ],
            )

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)

    def test_fork_adds_junction_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Base already has a fork C at a3 (junction a3 -> [C]). A second
        # fork D at the same point must MERGE into the target list — the
        # old sidecar row alone would erase D (the native-junction
        # deviation in _load_sessions_partial).
        def add_fork(p: Path, sid: str, first_ts: str) -> None:
            _write_jsonl(
                p / f"{sid}.jsonl",
                [
                    _entry("user", sid, f"{sid}-1", "a3", first_ts, "Forked"),
                    _entry(
                        "assistant",
                        sid,
                        f"{sid}-2",
                        f"{sid}-1",
                        first_ts.replace("T12:0", "T12:1"),
                        "Fork reply",
                    ),
                ],
            )

        inc_dir = _base_project(tmp_path / "inc")
        full_dir = _base_project(tmp_path / "full")
        for d in (inc_dir, full_dir):
            add_fork(d, "sess-c1", "2025-07-06T12:00:00.000Z")
        _convert(inc_dir)
        _convert(full_dir)

        for d in (inc_dir, full_dir):
            add_fork(d, "sess-d1", "2025-07-07T12:00:00.000Z")

        results = _spy_incremental(monkeypatch)
        _convert(inc_dir)
        assert results == [True]
        monkeypatch.setenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", "0")
        _convert(full_dir)

        state = _db_state(inc_dir)
        assert state == _db_state(full_dir)
        assert _html_files(inc_dir) == _html_files(full_dir)
        # The junction on a3 must now carry BOTH forks, chronologically.
        a3_targets = [t for u, _s, t, _q in state["junctions"] if u == "a3"]
        assert a3_targets == ["sess-c1", "sess-d1"]

    def test_cross_file_summary_for_closure_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A summary for session A lives in ANOTHER session's file; when
        # A's row is recomputed, that file must be pulled in or the
        # partial summary view drifts from the full one.
        def add_summary_file(p: Path) -> None:
            _write_jsonl(
                p / "sess-z.jsonl",
                _make_session("sess-z", "2025-07-05T08:00:00.000Z", 5)
                + [{"type": "summary", "summary": "A's summary", "leafUuid": "a3"}],
            )

        inc_dir = _base_project(tmp_path / "inc")
        full_dir = _base_project(tmp_path / "full")
        for d in (inc_dir, full_dir):
            add_summary_file(d)
        _convert(inc_dir)
        _convert(full_dir)

        def mutate(p: Path) -> None:
            import json

            extra = _entry(
                "user", "sess-a", "a4", "a3", "2025-07-08T10:00:00.000Z", "Later"
            )
            with open(p / "sess-a.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(extra) + "\n")
            _bump_mtime(p / "sess-a.jsonl")

        mutate(inc_dir)
        mutate(full_dir)

        results = _spy_incremental(monkeypatch)
        _convert(inc_dir)
        assert results == [True]
        monkeypatch.setenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", "0")
        _convert(full_dir)

        assert _db_state(inc_dir) == _db_state(full_dir)
        assert _html_files(inc_dir) == _html_files(full_dir)

    def test_new_summary_titles_an_untouched_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The forward metadata direction, and a real bug the 803MB
        # holdback run caught: a modified file gains a summary whose
        # leafUuid belongs to ANOTHER session. That session's own
        # entries never changed, so nothing else would pull it into the
        # closure — yet its cached `summary` column must be recomputed.
        # Without the summary-leaf closure the incremental run leaves it
        # None while the full refresh fills it in (invisible in the
        # rendered bytes of this fixture, which is why the bar is DB
        # state).
        def mutate(p: Path) -> None:
            import json

            with open(p / "sess-b.jsonl", "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "summary",
                            "summary": "A's late summary",
                            "leafUuid": "a2",
                        }
                    )
                    + "\n"
                )
            _bump_mtime(p / "sess-b.jsonl")

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)

        # And the summary really did land on A (not a vacuous pass).
        state = _db_state(tmp_path / "inc" / "proj")
        summaries = {sid: summary for sid, summary, *_rest in state["sessions"]}
        assert summaries["sess-a"] == "A's late summary"

    def test_new_ai_title_for_another_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ai-title arm of the forward direction. Its target sits in
        # the messages table's session_id column, so the closure reaches
        # it through the modified file's own session set rather than the
        # summary-leaf resolution — but the *count* still has to come
        # from the session's residual_count, since ai-titles are entries
        # compute_session_data skips.
        def mutate(p: Path) -> None:
            import json

            with open(p / "sess-b.jsonl", "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "ai-title",
                            "aiTitle": "Titled from elsewhere",
                            "sessionId": "sess-a",
                            "timestamp": "2025-07-08T11:00:00.000Z",
                        }
                    )
                    + "\n"
                )
            _bump_mtime(p / "sess-b.jsonl")

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)

        state = _db_state(tmp_path / "inc" / "proj")
        titles = {
            sid: ai_title for sid, _summary, ai_title, *_rest in state["sessions"]
        }
        assert titles["sess-a"] == "Titled from elsewhere"

    def test_attachments_in_a_new_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attachments dropped by traversal must not inflate the total.

        A real-archive holdback run caught this: cached message rows are
        the *parsed* list, but ``total_message_count`` is the *traversed*
        one, and an attachment whose parent never resolves is parsed yet
        never traversed. Counting attachments from cached rows over-counted
        the project total by exactly the dropped ones (+16 there). They are
        now carried by each session's ``residual_count``, computed from the
        traversed entries, so the delta sees only survivors.
        """

        def mutate(p: Path) -> None:
            entries: list[dict[str, Any]] = _make_session(
                "sess-d", "2025-07-04T10:00:00.000Z", 4
            )
            # One attachment on a real parent (traversed, counted) and one
            # orphaned (parsed, dropped) — the two must be told apart.
            for idx, parent in enumerate(("sess-d-u0", "nonexistent-parent")):
                entries.append(
                    {
                        "type": "attachment",
                        "timestamp": f"2025-07-04T10:1{idx}:00.000Z",
                        "parentUuid": parent,
                        "isSidechain": False,
                        "userType": "human",
                        "cwd": "/tmp",
                        "sessionId": "sess-d",
                        "version": "1.0.0",
                        "uuid": f"sess-d-att{idx}",
                        "attachment": {"type": "new_diagnostics", "diagnostics": []},
                    }
                )
            _write_jsonl(p / "sess-d.jsonl", entries)

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=True)


class TestIncrementalDeclines:
    def test_shrunk_file_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(p: Path) -> None:
            path = p / "sess-b.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            _bump_mtime(path)

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=False)

    def test_deleted_file_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(p: Path) -> None:
            (p / "sess-b.jsonl").unlink()
            # Something must still be modified or the cache is fresh and
            # neither path runs; touch another session with an append.
            import json

            extra = _entry(
                "user", "sess-a", "a4", "a3", "2025-07-08T10:00:00.000Z", "Later"
            )
            with open(p / "sess-a.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(extra) + "\n")
            _bump_mtime(p / "sess-a.jsonl")

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=False)

    def test_kill_switch_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", "0")

        def mutate(p: Path) -> None:
            _write_jsonl(
                p / "sess-d.jsonl",
                _make_session("sess-d", "2025-07-04T10:00:00.000Z", 5),
            )

        # expect_incremental=False: the guard prevents even the attempt.
        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=False)

    def test_majority_modified_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def mutate(p: Path) -> None:
            for sid in ("sess-d", "sess-e", "sess-f", "sess-g", "sess-h"):
                _write_jsonl(
                    p / f"{sid}.jsonl",
                    _make_session(sid, "2025-07-09T10:00:00.000Z", 5),
                )

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=False)

    def test_missing_session_row_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates a pre-migration-009 cache: a session with entries but
        # no sessions row has an unknown old contribution.
        def mutate(p: Path) -> None:
            cm = CacheManager(p, get_library_version())
            with cm._get_connection() as conn:  # pyright: ignore[reportPrivateUsage]
                conn.execute("DELETE FROM sessions WHERE session_id = 'sess-a'")
                conn.commit()
            import json

            extra = _entry(
                "user", "sess-a", "a4", "a3", "2025-07-08T10:00:00.000Z", "Later"
            )
            with open(p / "sess-a.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(extra) + "\n")
            _bump_mtime(p / "sess-a.jsonl")

        _run_scenario(tmp_path, monkeypatch, mutate, expect_incremental=False)


class TestIncrementalStability:
    def test_second_run_is_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # After an incremental refresh, the very next conversion must
        # find the cache fresh — no refresh path runs at all.
        project = _base_project(tmp_path / "p")
        _convert(project)
        _write_jsonl(
            project / "sess-d.jsonl",
            _make_session("sess-d", "2025-07-04T10:00:00.000Z", 5),
        )
        results = _spy_incremental(monkeypatch)
        _convert(project)
        assert results == [True]
        _convert(project)
        assert results == [True], "second run must not attempt any refresh"

    def test_new_session_never_loads_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stage 4's headline: a new session costs no whole-project load.

        With streaming allowed for the render and the incremental path
        for the refresh, a conversion after a new session appears must
        never call ``load_directory_transcripts`` — the last
        full-residency step in the pipeline. Monkeypatched to raise, so
        a regression fails loudly instead of quietly costing residency.
        """
        monkeypatch.delenv("CLAUDE_CODE_LOG_STREAMING", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_LOG_STREAMING", "1")
        project = _base_project(tmp_path / "p")
        _convert(project)
        _write_jsonl(
            project / "sess-d.jsonl",
            _make_session("sess-d", "2025-07-04T10:00:00.000Z", 5),
        )

        def _fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "load_directory_transcripts was called — a new session "
                "should cost neither a full cache refresh nor a full render load"
            )

        monkeypatch.setattr(converter, "load_directory_transcripts", _fail)
        _convert(project)
        assert (project / "session-sess-d.html").exists()
