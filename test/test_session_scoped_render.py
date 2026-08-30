"""Byte-equivalence and gating tests for the session-scoped incremental path.

When the cache is fresh and only session files are stale, the conversion
regenerates them from those sessions' own JSONL plus the persisted
cross-session sidecar instead of loading the whole project
(``converter._load_stale_session_transcripts``). The acceptance bar is the
same as for every other render optimisation on this branch: byte-identical
output to the full-load path.

Two layers of coverage:

- Real fixture projects (multi-session, agents, workflows) prove the
  partial path engages and reproduces the full path's bytes — with the
  full loader monkeypatched to *raise*, so a silent fallback can't make
  the comparison vacuous.
- A synthetic project pins the cross-session mechanics deterministically:
  a resume-replayed prefix (shared uuids whose dedup winner is another
  session), a fork with a cross-session parent link (junction + parent
  patch), and a two-deep fork chain (ancestor stub lines).

The fixture projects barely exercise cross-session coupling (that is the
§ 4.8 lesson from work/render-format-once.md), so hash runs over real
archives remain the wider net; the synthetic project keeps the mechanics
pinned in CI.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, SessionSidecar, get_library_version
from claude_code_log.converter import convert_jsonl_to

FIXTURE_ROOT = Path(__file__).parent / "test_data" / "real_projects"

# Multi-session project with agents and Pygments/Markdown-heavy output.
MULTI_SESSION_PROJECT = FIXTURE_ROOT / "-Users-dain-workspace-coderabbit-review-helper"
# The one fixture with a uuid shared across session files.
SHARED_UUID_PROJECT = FIXTURE_ROOT / "-Users-dain-workspace-claude-code-log-sample"


def _copy_project(tmp_path: Path, source: Path) -> Path:
    """Copy a fixture project, keeping its own directory name.

    The rendered title derives from the directory name, so renaming the
    copy would change every page's bytes for no reason.
    """
    work_dir = tmp_path / source.name
    shutil.copytree(source, work_dir)
    return work_dir


def _convert(work_dir: Path) -> None:
    convert_jsonl_to("html", work_dir, silent=True)


def _session_files(work_dir: Path) -> dict[str, bytes]:
    files = {p.name: p.read_bytes() for p in sorted(work_dir.glob("session-*.html"))}
    assert files, "conversion produced no session files"
    return files


def _forbid_full_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a fall-through to the full loader fail the test loudly."""

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "full load_directory_transcripts was called — the session-scoped "
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


class TestSessionScopedEquivalence:
    def test_partial_regeneration_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _copy_project(tmp_path, MULTI_SESSION_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        # Delete two session files; everything else (combined, cache) is
        # fresh, so the second run must take the session-scoped path.
        stale = sorted(baseline)[:2]
        for name in stale:
            (work_dir / name).unlink()

        _forbid_full_load(monkeypatch)
        _convert(work_dir)

        regenerated = _session_files(work_dir)
        assert set(regenerated) == set(baseline)
        for name in baseline:
            assert regenerated[name] == baseline[name], (
                f"session-scoped regeneration changed the bytes of {name}"
            )

    def test_all_sessions_stale_still_partial_and_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shared-uuid fixture: deleting every session file makes the
        # "partial" set the whole project — a degenerate but valid case
        # that must still reproduce the full path's bytes (including the
        # one cross-file duplicated uuid's dedup).
        work_dir = _copy_project(tmp_path, SHARED_UUID_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        for name in baseline:
            (work_dir / name).unlink()

        _forbid_full_load(monkeypatch)
        _convert(work_dir)

        regenerated = _session_files(work_dir)
        assert regenerated == baseline

    def test_report_counts_partial_regenerations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _copy_project(tmp_path, MULTI_SESSION_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        stale = sorted(baseline)[:3]
        for name in stale:
            (work_dir / name).unlink()

        _forbid_full_load(monkeypatch)
        report = converter.RegenerationReport()
        convert_jsonl_to("html", work_dir, silent=True, report=report)
        assert report.sessions_regenerated == len(stale)
        assert report.combined_regenerated is False


class TestSessionScopedGating:
    def test_kill_switch_forces_full_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _copy_project(tmp_path, MULTI_SESSION_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        name = sorted(baseline)[0]
        (work_dir / name).unlink()

        monkeypatch.setenv("CLAUDE_CODE_LOG_SESSION_SCOPED", "0")
        calls = _spy_full_load(monkeypatch)
        _convert(work_dir)

        assert calls, "kill switch should have routed through the full loader"
        assert _session_files(work_dir)[name] == baseline[name]

    def test_declines_without_sidecar_then_repopulates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _copy_project(tmp_path, MULTI_SESSION_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        # Simulate a cache from before migration 008: drop the marker row.
        cache = CacheManager(work_dir, get_library_version())
        with cache._get_connection() as conn:  # pyright: ignore[reportPrivateUsage]
            conn.execute("DELETE FROM sidecar_state")
            conn.commit()
        assert cache.load_session_sidecar() is None

        name = sorted(baseline)[0]
        (work_dir / name).unlink()

        calls = _spy_full_load(monkeypatch)
        _convert(work_dir)

        assert calls, "missing sidecar must decline to the full loader"
        assert _session_files(work_dir)[name] == baseline[name]
        # The full load repopulated the sidecar for next time.
        assert cache.load_session_sidecar() is not None

    def test_archived_stale_session_skips_load_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = _copy_project(tmp_path, MULTI_SESSION_PROJECT)
        _convert(work_dir)
        baseline = _session_files(work_dir)

        # Archive one session: source gone, rendered file gone, cache rows
        # remain. The full path would load the whole project and render
        # nothing for it (the session isn't in the loaded messages); the
        # session-scoped path reaches the same outcome without loading —
        # and still regenerates OTHER stale sessions alongside.
        archived = sorted(baseline)[0]
        archived_sid = archived[len("session-") : -len(".html")]
        (work_dir / archived).unlink()
        (work_dir / f"{archived_sid}.jsonl").unlink()
        subagent_dir = work_dir / archived_sid
        if subagent_dir.exists():
            shutil.rmtree(subagent_dir)

        other = sorted(baseline)[1]
        (work_dir / other).unlink()

        _forbid_full_load(monkeypatch)
        _convert(work_dir)

        after = _session_files(work_dir)
        assert archived not in after, "archived session cannot be re-rendered"
        assert after[other] == baseline[other]

    def test_partially_missing_file_spanning_session_declines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A surviving mapped source means the session is not archived."""
        project = tmp_path / "file-spanning-project"
        project.mkdir()
        _write_jsonl(
            project / "sess-a.jsonl",
            [
                _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "A"),
                # E starts in A's file and continues in its stem-named file.
                _entry("user", "sess-e", "e1", None, "2025-07-01T11:00:00.000Z", "E"),
            ],
        )
        _write_jsonl(
            project / "sess-e.jsonl",
            [
                _entry(
                    "assistant",
                    "sess-e",
                    "e2",
                    "e1",
                    "2025-07-01T11:01:00.000Z",
                    "E reply",
                )
            ],
        )
        _convert(project)
        assert (project / "session-sess-e.html").exists()

        # The cached map still names both sources, but only sess-a.jsonl
        # survives. A full load renders E's surviving first entry.
        (project / "sess-e.jsonl").unlink()
        (project / "session-sess-e.html").unlink()

        calls = _spy_full_load(monkeypatch)
        _convert(project)

        assert calls, "a partially missing source set must decline to the full path"
        rendered = project / "session-sess-e.html"
        assert rendered.exists()
        assert b">E<" in rendered.read_bytes()


class TestSessionSidecarCache:
    def test_round_trip(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        cache = CacheManager(project, get_library_version())
        assert cache.load_session_sidecar() is None

        sidecar = SessionSidecar(
            parents={
                "child": ("parent", "uuid-attach"),
                "branchless": ("parent", None),
            },
            junctions={
                "uuid-attach": ("parent", ["child", "other"]),
                "solo": ("parent", ["child"]),
            },
            dedup_winners={"shared-uuid": "parent"},
        )
        cache.save_session_sidecar(sidecar)

        loaded = cache.load_session_sidecar()
        assert loaded == sidecar

        # Wholesale rewrite replaces, never merges.
        smaller = SessionSidecar(parents={}, junctions={}, dedup_winners={})
        cache.save_session_sidecar(smaller)
        assert cache.load_session_sidecar() == smaller

    def test_read_only_manager_never_writes(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        # Initialise the DB with a writable manager first.
        CacheManager(project, get_library_version())
        ro = CacheManager(project, get_library_version(), read_only=True)
        ro.save_session_sidecar(
            SessionSidecar(parents={}, junctions={}, dedup_winners={})
        )
        assert ro.load_session_sidecar() is None


# ---------------------------------------------------------------------------
# Synthetic cross-session project: pins the mechanics the fixtures lack.
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


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


@pytest.fixture()
def cross_session_project(tmp_path: Path) -> Path:
    """Four sessions wired with every cross-session mechanism:

    - ``sess-a``: the root conversation.
    - ``sess-b``: a resume of A — replays A's ``a2`` under its own
      sessionId (shared uuid; the whole-project dedup keeps A's copy
      because A's first timestamp is earlier), then continues from it.
    - ``sess-c``: a fork — its first entry's parent is A's ``a3``
      (cross-session parent link, junction on ``a3``).
    - ``sess-d``: forked from C — rendering D alone needs ancestor
      stub lines for C *and* A to resolve its depth chain.
    """
    project = tmp_path / "cross-session-project"
    project.mkdir()
    _write_jsonl(
        project / "sess-a.jsonl",
        [
            _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "Start"),
            _entry(
                "assistant", "sess-a", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-a", "a3", "a2", "2025-07-01T10:02:00.000Z", "More"),
        ],
    )
    _write_jsonl(
        project / "sess-b.jsonl",
        [
            # Replayed prefix: same uuid a2, B's sessionId. B's first
            # timestamp (10:01) is strictly later than A's (10:00), so the
            # dedup winner is unambiguously A.
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
        project / "sess-c.jsonl",
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
        project / "sess-d.jsonl",
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
    return project


class TestCrossSessionMechanics:
    @pytest.mark.parametrize(
        "stale_sessions",
        [
            ["sess-b"],  # dedup-winner drop + parent patch via sidecar
            ["sess-a"],  # junction patch: fork markers to unloaded B and C
            ["sess-d"],  # ancestor stubs: depth chain D -> C -> A
            ["sess-b", "sess-c"],  # multi-session subset
        ],
    )
    def test_partial_render_matches_full(
        self,
        cross_session_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        stale_sessions: list[str],
    ) -> None:
        _convert(cross_session_project)
        baseline = _session_files(cross_session_project)

        for sid in stale_sessions:
            (cross_session_project / f"session-{sid}.html").unlink()

        _forbid_full_load(monkeypatch)
        _convert(cross_session_project)

        assert _session_files(cross_session_project) == baseline

    def test_sidecar_contents(self, cross_session_project: Path) -> None:
        _convert(cross_session_project)
        cache = CacheManager(cross_session_project, get_library_version())
        sidecar = cache.load_session_sidecar()
        assert sidecar is not None
        # A's copy of a2 wins; B's replay is the loser the winner map records.
        assert sidecar.dedup_winners == {"a2": "sess-a"}
        assert sidecar.parents["sess-b"] == ("sess-a", "a2")
        assert sidecar.parents["sess-c"] == ("sess-a", "a3")
        assert sidecar.parents["sess-d"] == ("sess-c", "c2")
        assert sidecar.junctions["a3"] == ("sess-a", ["sess-c"])


# ---------------------------------------------------------------------------
# Branch-winner project: a duplicated uuid whose surviving copy sits on a
# *branch* line of its session.
# ---------------------------------------------------------------------------


@pytest.fixture()
def branch_replay_project(tmp_path: Path) -> Path:
    """A session with an in-session branch, replayed by a resume.

    ``sess-a`` trunk is a1←a2←a3; a second child of ``a2``
    (``branch-root-01`` ← ``branch-msg-0002``) forms a branch line, which
    the DAG names ``sess-a@branch-root-0``. ``sess-r`` resumes from the
    branch: it replays both branch entries under its own sessionId and
    continues from them, so the whole-project dedup winner for those
    uuids is sess-a — whose surviving copies sit on the *branch* line.
    The sidecar must record the raw ``sess-a`` (the id partial loads
    compare raw entries against), not the branch-qualified line id: a
    ``{trunk}@{uuid12}`` winner matches no raw entry.sessionId, so every
    copy would be dropped and the branch silently deleted from partial
    renders (found by the streaming hash runs on the reference archive).
    """
    project = tmp_path / "branch-replay-project"
    project.mkdir()
    _write_jsonl(
        project / "sess-a.jsonl",
        [
            _entry("user", "sess-a", "a1", None, "2025-07-01T10:00:00.000Z", "Start"),
            _entry(
                "assistant", "sess-a", "a2", "a1", "2025-07-01T10:01:00.000Z", "Reply"
            ),
            _entry("user", "sess-a", "a3", "a2", "2025-07-01T10:02:00.000Z", "More"),
            # The branch: a second child of a2, later than a3.
            _entry(
                "user",
                "sess-a",
                "branch-root-01",
                "a2",
                "2025-07-01T10:10:00.000Z",
                "Branching",
            ),
            _entry(
                "assistant",
                "sess-a",
                "branch-msg-0002",
                "branch-root-01",
                "2025-07-01T10:11:00.000Z",
                "Branch reply",
            ),
        ],
    )
    _write_jsonl(
        project / "sess-r.jsonl",
        [
            # Replayed branch prefix under sess-r's id (later session ⇒
            # sess-a's copies win project-wide).
            _entry(
                "user",
                "sess-r",
                "branch-root-01",
                "a2",
                "2025-07-01T10:10:00.000Z",
                "Branching",
            ),
            _entry(
                "assistant",
                "sess-r",
                "branch-msg-0002",
                "branch-root-01",
                "2025-07-01T10:11:00.000Z",
                "Branch reply",
            ),
            _entry(
                "user",
                "sess-r",
                "r1",
                "branch-msg-0002",
                "2025-07-01T12:00:00.000Z",
                "Resumed from branch",
            ),
            _entry(
                "assistant",
                "sess-r",
                "r2",
                "r1",
                "2025-07-01T12:01:00.000Z",
                "Resumed reply",
            ),
        ],
    )
    return project


class TestBranchWinnerMechanics:
    def test_sidecar_records_raw_session_ids(self, branch_replay_project: Path) -> None:
        _convert(branch_replay_project)
        cache = CacheManager(branch_replay_project, get_library_version())
        sidecar = cache.load_session_sidecar()
        assert sidecar is not None
        assert sidecar.dedup_winners == {
            "branch-root-01": "sess-a",
            "branch-msg-0002": "sess-a",
        }

    @pytest.mark.parametrize("stale_sessions", [["sess-a"], ["sess-r"]])
    def test_partial_render_keeps_the_branch(
        self,
        branch_replay_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        stale_sessions: list[str],
    ) -> None:
        _convert(branch_replay_project)
        baseline = _session_files(branch_replay_project)
        # The branch is really in the baseline, or this pins nothing.
        assert b"branch-root-0" in baseline["session-sess-a.html"]

        for sid in stale_sessions:
            (branch_replay_project / f"session-{sid}.html").unlink()

        _forbid_full_load(monkeypatch)
        _convert(branch_replay_project)

        assert _session_files(branch_replay_project) == baseline

    def test_branch_qualified_winner_from_old_sidecar_still_enforces(
        self, branch_replay_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A sidecar persisted by the pre-fix code carries the
        # branch-qualified line id; the load-time normalization must make
        # it enforce as the raw trunk id instead of dropping every copy.
        _convert(branch_replay_project)
        baseline = _session_files(branch_replay_project)

        cache = CacheManager(branch_replay_project, get_library_version())
        with cache._get_connection() as conn:  # pyright: ignore[reportPrivateUsage]
            conn.execute(
                "UPDATE dedup_winners SET winner_session_id = ?",
                ("sess-a@branch-root-0",),
            )
            conn.commit()

        for name in ("session-sess-a.html", "session-sess-r.html"):
            (branch_replay_project / name).unlink()

        _forbid_full_load(monkeypatch)
        _convert(branch_replay_project)

        assert _session_files(branch_replay_project) == baseline
