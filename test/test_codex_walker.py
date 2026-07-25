"""Wholesale walker for the Codex provider (modalities M2).

``render_provider_wholesale`` renders every session of one provider under a
root into a project hierarchy (per-session pages + per-project combined page +
master index), grouping sessions by cwd. These tests exercise the walker
end-to-end on the synthetic fixture tree and pin the index-summary dict shape
against the Claude path so a key rename on either side goes RED rather than
silently producing an empty provider index.
"""

import json
from pathlib import Path

import pytest

from claude_code_log.converter import (
    process_projects_hierarchy,
    render_provider_wholesale,
)

FIXTURES = Path(__file__).parent / "test_data" / "codex"
SESSIONS_ROOT = FIXTURES / "sessions"
SYNTHETIC_PROJECT_DIR = "-workspace-synthetic-project"


def _rollout(tmp: Path, rel: str, thread_id: str, cwd: str | None) -> Path:
    payload: dict[str, object] = {"id": thread_id, "timestamp": "2026-01-02T00:00:00Z"}
    if cwd is not None:
        payload["cwd"] = cwd
    records = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "type": "session_meta",
            "payload": payload,
        },
        {
            "timestamp": "2026-01-02T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"hi {thread_id[:4]}"},
        },
        {
            "timestamp": "2026-01-02T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": f"bye {thread_id[:4]}"},
        },
    ]
    rel_path = Path(rel)
    path = tmp / rel_path.parent / f"rollout-{rel_path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _make_claude_session(path: Path, sid: str) -> None:
    """A minimal two-message Claude transcript (the default, non-provider path)."""
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": sid,
            "version": "1.0.0",
            "uuid": f"{sid}-0",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:01:00Z",
            "parentUuid": f"{sid}-0",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": sid,
            "version": "1.0.0",
            "uuid": f"{sid}-1",
            "message": {
                "id": f"{sid}-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet",
                "content": [{"type": "text", "text": "hi there"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# End-to-end: index + per-project combined + per-session pages.
# --------------------------------------------------------------------------
def test_walker_renders_index_combined_and_sessions(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    index = render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)

    assert index == out / "index.html"
    assert index.exists()
    # Two cwd-projects: the synthetic-project (root + parent + child threads)
    # and the no-cwd legacy session bucket. The archived session lives outside
    # the sessions root and must not be discovered.
    proj = out / SYNTHETIC_PROJECT_DIR
    assert (proj / "combined_transcripts.html").exists()
    assert (proj / "session-11111111-1111-4111-8111-111111111111.html").exists()
    assert (out / "no-project" / "combined_transcripts.html").exists()

    index_text = index.read_text(encoding="utf-8")
    # The index links to each project's combined page.
    assert f"{SYNTHETIC_PROJECT_DIR}/combined_transcripts.html" in index_text
    assert "no-project/combined_transcripts.html" in index_text


@pytest.mark.parametrize(
    ("fmt", "index_name", "combined_name"),
    [
        ("html", "index.html", "combined_transcripts.html"),
        ("md", "index.md", "combined_transcripts.md"),
        ("json", "all-projects-summary.json", "combined_transcripts.json"),
    ],
)
def test_walker_all_three_formats(
    tmp_path: Path, fmt: str, index_name: str, combined_name: str
) -> None:
    out = tmp_path / "ccl"
    index = render_provider_wholesale(
        "codex", SESSIONS_ROOT, out, output_format=fmt, silent=True
    )
    assert index == out / index_name
    assert index.exists()
    assert (out / SYNTHETIC_PROJECT_DIR / combined_name).exists()


def test_walker_groups_by_cwd(tmp_path: Path) -> None:
    tree = tmp_path / "sessions"
    _rollout(tree, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")
    _rollout(tree, "a/two.jsonl", "10000000-0000-4000-8000-000000000002", "/proj/a")
    _rollout(tree, "b/one.jsonl", "20000000-0000-4000-8000-000000000001", "/proj/b")
    _rollout(tree, "c/orphan.jsonl", "30000000-0000-4000-8000-000000000001", None)

    out = tmp_path / "ccl"
    render_provider_wholesale("codex", tree, out, silent=True)

    assert (out / "-proj-a" / "combined_transcripts.html").exists()
    assert (out / "-proj-b" / "combined_transcripts.html").exists()
    assert (out / "no-project" / "combined_transcripts.html").exists()
    # /proj/a has two sessions, both rendered.
    a_sessions = list((out / "-proj-a").glob("session-*.html"))
    assert len(a_sessions) == 2


# --------------------------------------------------------------------------
# --expand-paths / --filter-path (Obsidian projection over group-by-cwd).
# --------------------------------------------------------------------------
def test_walker_expand_paths_projects_to_real_tree(tmp_path: Path) -> None:
    """--expand-paths projects each cwd-project under its REAL path (not the flat
    -proj-a name), with output-root-relative POSIX index links. The no-cwd bucket
    has no path to expand and stays flat."""
    tree = tmp_path / "sessions"
    _rollout(tree, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")
    _rollout(tree, "b/one.jsonl", "20000000-0000-4000-8000-000000000001", "/proj/b")
    _rollout(tree, "c/orphan.jsonl", "30000000-0000-4000-8000-000000000001", None)

    out = tmp_path / "ccl"
    index = render_provider_wholesale(
        "codex", tree, out, expand_paths=True, silent=True
    )

    assert (out / "proj" / "a" / "combined_transcripts.html").exists()
    assert (out / "proj" / "b" / "combined_transcripts.html").exists()
    assert not (out / "-proj-a").exists()  # not the flat name
    assert (out / "no-project" / "combined_transcripts.html").exists()  # stays flat

    index_text = index.read_text(encoding="utf-8")
    # Links are relative to the output root, POSIX-separated, into the tree.
    assert "proj/a/combined_transcripts.html" in index_text
    assert "proj/b/combined_transcripts.html" in index_text
    assert "no-project/combined_transcripts.html" in index_text


def test_walker_filter_path_trims_matches_and_excludes_the_rest(
    tmp_path: Path,
) -> None:
    """--filter-path with --expand-paths trims the matched prefix from the
    destination and drops projects outside it."""
    tree = tmp_path / "sessions"
    _rollout(tree, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/home/me/a")
    _rollout(tree, "b/one.jsonl", "20000000-0000-4000-8000-000000000001", "/other/b")

    out = tmp_path / "ccl"
    render_provider_wholesale(
        "codex", tree, out, expand_paths=True, filter_path="/home/me", silent=True
    )
    assert (out / "a" / "combined_transcripts.html").exists()  # prefix trimmed
    assert not (out / "other").exists()  # outside the filter → excluded
    assert not (out / "b").exists()


def test_walker_filter_path_skips_the_no_cwd_bucket(tmp_path: Path) -> None:
    """A no-cwd session can't satisfy an absolute --filter-path prefix, so the
    bucket is skipped rather than routed through a lossy flat-name decode."""
    tree = tmp_path / "sessions"
    _rollout(tree, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/home/me/a")
    _rollout(tree, "c/orphan.jsonl", "30000000-0000-4000-8000-000000000001", None)

    out = tmp_path / "ccl"
    render_provider_wholesale(
        "codex", tree, out, expand_paths=True, filter_path="/home/me", silent=True
    )
    assert (out / "a" / "combined_transcripts.html").exists()
    assert not (out / "no-project").exists()


def test_walker_expand_paths_rerenders_despite_flat_cache(tmp_path: Path) -> None:
    """The wholesale cache identity is the OUTPUT dir (M3 decision). Expanding
    paths changes the dest dir → changes the cache key → the first expanded run
    re-renders every session even though a prior flat run is warm. This is
    by-design, pinned so it can't later read as a cache bug."""
    tree = tmp_path / "sessions"
    _rollout(tree, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")

    out = tmp_path / "ccl"
    render_provider_wholesale("codex", tree, out, silent=True)  # warm the flat cache
    assert (out / "-proj-a" / "combined_transcripts.html").exists()

    render_provider_wholesale("codex", tree, out, expand_paths=True, silent=True)
    # Different dest dir → rendered afresh, not skipped by the flat-keyed cache.
    assert (out / "proj" / "a" / "combined_transcripts.html").exists()


# --------------------------------------------------------------------------
# Index labelling: disambiguate colliding basenames, provider-aware title.
# --------------------------------------------------------------------------
def test_walker_index_disambiguates_colliding_worktree_labels(tmp_path: Path) -> None:
    """The real field-test defect: two worktrees both named ``codex`` rendered a
    single duplicated label and read as one project split in two. The index now
    disambiguates them by their differing parent component."""
    tree = tmp_path / "sessions"
    _rollout(
        tree,
        "a/one.jsonl",
        "10000000-0000-4000-8000-000000000001",
        "/home/me/projA/codex",
    )
    _rollout(
        tree,
        "b/one.jsonl",
        "20000000-0000-4000-8000-000000000001",
        "/home/me/projB/codex",
    )
    out = tmp_path / "ccl"
    index = render_provider_wholesale("codex", tree, out, silent=True)
    text = index.read_text(encoding="utf-8")
    assert "projA/codex" in text
    assert "projB/codex" in text


def test_walker_index_title_reflects_provider(tmp_path: Path) -> None:
    """The index of a Codex render must not be titled "Claude Code Projects"."""
    out = tmp_path / "ccl"
    index = render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)
    text = index.read_text(encoding="utf-8")
    assert "Codex Projects" in text
    assert "Claude Code Projects" not in text


def test_walker_combined_no_suppresses_combined_page(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale(
        "codex", SESSIONS_ROOT, out, write_combined=False, silent=True
    )
    proj = out / SYNTHETIC_PROJECT_DIR
    # No combined page is written…
    assert not (proj / "combined_transcripts.html").exists()
    # …but per-session pages are, and they carry no dangling back-link to the
    # (absent) combined transcript.
    session = proj / "session-11111111-1111-4111-8111-111111111111.html"
    assert session.exists()
    assert "combined_transcripts.html" not in session.read_text(encoding="utf-8")


def test_walker_no_individual_still_writes_combined_and_index(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale(
        "codex", SESSIONS_ROOT, out, write_individual=False, silent=True
    )
    proj = out / SYNTHETIC_PROJECT_DIR
    assert (proj / "combined_transcripts.html").exists()
    assert not list(proj.glob("session-*.html"))
    assert (out / "index.html").exists()


def test_walker_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    render_provider_wholesale("codex", SESSIONS_ROOT, first, silent=True)
    render_provider_wholesale("codex", SESSIONS_ROOT, second, silent=True)

    for rel in (
        "index.html",
        f"{SYNTHETIC_PROJECT_DIR}/combined_transcripts.html",
        f"{SYNTHETIC_PROJECT_DIR}/session-11111111-1111-4111-8111-111111111111.html",
    ):
        assert (first / rel).read_bytes() == (second / rel).read_bytes()


def test_walker_date_filter_excludes_out_of_range_sessions(tmp_path: Path) -> None:
    tree = tmp_path / "sessions"
    # One 2026-01 session, one 2020-01 session under distinct cwds.
    _rollout(tree, "new/s.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/new")
    old = _rollout(
        tree, "old/s.jsonl", "20000000-0000-4000-8000-000000000002", "/proj/old"
    )
    # Rewrite the old session's timestamps into 2020.
    old.write_text(
        old.read_text(encoding="utf-8").replace("2026-01-02", "2020-01-02"),
        encoding="utf-8",
    )

    out = tmp_path / "ccl"
    render_provider_wholesale("codex", tree, out, from_date="2025-01-01", silent=True)
    assert (out / "-proj-new").exists()
    # The 2020 session is filtered out → its project renders nothing → no dir.
    assert not (out / "-proj-old").exists()


# --------------------------------------------------------------------------
# REQUIRED drift pin: the walker's index-summary dict must be shape-identical
# to the Claude path's, so a key rename on either side goes RED instead of
# silently rendering an empty provider index.
# --------------------------------------------------------------------------
def test_index_summary_dict_shape_matches_claude_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_log.html.renderer import HtmlRenderer

    captured: list[list[dict[str, object]]] = []
    original = HtmlRenderer.generate_projects_index

    def _spy(self, project_summaries, *args, **kwargs):
        captured.append(project_summaries)
        return original(self, project_summaries, *args, **kwargs)

    monkeypatch.setattr(HtmlRenderer, "generate_projects_index", _spy)

    # Claude path: a minimal projects root with one project + one session.
    claude_root = tmp_path / "claude"
    project = claude_root / "-work-x"
    project.mkdir(parents=True)
    _make_claude_session(project / "sess.jsonl", "sess-1")
    process_projects_hierarchy(claude_root, silent=True)
    claude_summaries = captured[-1]

    # Walker path over the codex fixture tree.
    render_provider_wholesale("codex", SESSIONS_ROOT, tmp_path / "out", silent=True)
    walker_summaries = captured[-1]

    assert claude_summaries and walker_summaries
    claude_keys = set(claude_summaries[0])
    walker_keys = set(walker_summaries[0])
    assert claude_keys == walker_keys, (
        "project-summary dict drift — "
        f"claude-only={claude_keys - walker_keys}, "
        f"walker-only={walker_keys - claude_keys}"
    )

    claude_session_keys = set(claude_summaries[0]["sessions"][0])
    walker_session_keys = set(walker_summaries[0]["sessions"][0])
    assert claude_session_keys == walker_session_keys, (
        "session-summary dict drift — "
        f"claude-only={claude_session_keys - walker_session_keys}, "
        f"walker-only={walker_session_keys - claude_session_keys}"
    )


# --------------------------------------------------------------------------
# CLI dispatch: --provider wholesale reaches the walker; output-root rules.
# --------------------------------------------------------------------------
def _codex_home_with_sessions(tmp_path: Path) -> Path:
    """A throwaway CODEX_HOME whose sessions/ tree holds two cwd-projects."""
    home = tmp_path / "codex-home"
    sessions = home / "sessions" / "2026" / "01"
    _rollout(sessions, "a.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")
    _rollout(sessions, "b.jsonl", "20000000-0000-4000-8000-000000000002", "/proj/b")
    return home


def test_cli_bare_provider_runs_wholesale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    result = CliRunner().invoke(main, ["--provider", "codex", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert (out / "-proj-a" / "combined_transcripts.html").exists()
    assert (out / "-proj-b" / "combined_transcripts.html").exists()


def test_cli_provider_expand_paths_flips_combined_default_to_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared ``combined = "no" if expand_paths else "yes"`` default (Obsidian
    mode) now reaches provider wholesale — the point of the feature. With no
    --combined, --expand-paths yields per-session files and NO combined page; an
    explicit --combined=yes still writes the combined page. Default and override
    independently anchored."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))

    default_out = tmp_path / "default"
    r = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--all-projects",
            "--expand-paths",
            "-o",
            str(default_out),
        ],
    )
    assert r.exit_code == 0, r.output
    assert not list(default_out.rglob("combined_transcripts.html"))  # default → no
    assert list(default_out.rglob("session-*.html"))  # per-session still written

    override_out = tmp_path / "override"
    r2 = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--all-projects",
            "--expand-paths",
            "--combined",
            "yes",
            "-o",
            str(override_out),
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert list(override_out.rglob("combined_transcripts.html"))  # explicit override


def test_cli_provider_projects_dir_overrides_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--projects-dir selects the sessions root to walk (a subset/copy)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    # No CODEX_HOME needed — the explicit root is the fixture sessions tree.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    out = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        ["--provider", "codex", "--projects-dir", str(SESSIONS_ROOT), "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (out / SYNTHETIC_PROJECT_DIR / "combined_transcripts.html").exists()


def test_cli_provider_default_output_root_is_under_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECIDED #4: with no -o, output lands at <codex_home>/claude-code-log/,
    never inside the pristine sessions tree."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    home = _codex_home_with_sessions(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    result = CliRunner().invoke(main, ["--provider", "codex"])
    assert result.exit_code == 0, result.output
    assert (home / "claude-code-log" / "index.html").exists()
    # The sessions tree itself is untouched.
    assert not list((home / "sessions").rglob("index.html"))


def test_cli_provider_wholesale_rejects_file_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wholesale run writes many files; a file-shaped -o is a loud error."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    result = CliRunner().invoke(
        main, ["--provider", "codex", "-o", str(tmp_path / "one.html")]
    )
    assert result.exit_code != 0
    assert "directory, not a file" in result.output


def test_cli_provider_wholesale_accepts_projection_flags_but_tui_stays_illegal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--tui stays always-illegal in provider mode. --expand-paths/--filter-path
    were Claude-only too, but are now legal for wholesale (provider projects are
    synthetic group-by-cwd, so the flat name expands unambiguously) — while still
    rejected for single-session export, which has no multi-project projection."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))

    # --tui: always rejected in provider mode.
    tui = CliRunner().invoke(
        main, ["--provider", "codex", "--tui", "-o", str(tmp_path / "t")]
    )
    assert tui.exit_code != 0
    assert "does not support" in tui.output and "--tui" in tui.output

    # --expand-paths (and --filter-path with it): ACCEPTED for wholesale — the
    # run proceeds past the fence and renders, no "does not support" error.
    accepted = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--all-projects",
            "--expand-paths",
            "--filter-path",
            "/",
            "-o",
            str(tmp_path / "w"),
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert "does not support" not in accepted.output

    # ...but REJECTED for single-session export (--session-id).
    for flag in (["--expand-paths"], ["--filter-path", "/abs"]):
        result = CliRunner().invoke(
            main, ["--provider", "codex", "--session-id", "nope", *flag]
        )
        assert result.exit_code != 0, f"{flag} should be rejected for single-session"
        assert "does not support" in result.output and flag[0] in result.output


@pytest.mark.parametrize("extra", [["--page-size", "5"], ["--jobs", "2"]])
def test_cli_provider_wholesale_rejects_pagination_and_jobs_for_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    """Pagination and job-parallelism ride on cache machinery not yet wired for
    the provider walker, so they must be rejected loudly — never accepted and
    silently ignored."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    result = CliRunner().invoke(main, ["--provider", "codex", "-o", str(out), *extra])
    assert result.exit_code != 0
    assert "does not support" in result.output and extra[0] in result.output


# --------------------------------------------------------------------------
# M3 cache participation: render-skip, byte-stability, --no-cache.
#
# These use a content-TAMPER probe rather than mtimes: after a render we
# overwrite each page with a sentinel, render again, and check which sentinels
# survive. A surviving sentinel == the page was skipped; a replaced sentinel ==
# the page was re-rendered. This is robust against coarse filesystem mtime
# resolution and parallel-run timing.
# --------------------------------------------------------------------------
_TAMPER = "<!-- TAMPERED-SENTINEL -->"


def _tamper_all(out: Path) -> list[Path]:
    # Append (don't overwrite): the version marker lives in the first few lines
    # and drives the staleness check — clobbering it would force a re-render and
    # defeat the probe. Appending leaves the marker intact, so a skipped page
    # keeps the sentinel while a re-rendered page (full overwrite) loses it.
    pages = sorted(out.rglob("*.html"))
    for page in pages:
        with page.open("a", encoding="utf-8") as handle:
            handle.write("\n" + _TAMPER + "\n")
    return pages


def _survived(page: Path) -> bool:
    return _TAMPER in page.read_text(encoding="utf-8")


def test_walker_cache_db_lives_under_output_root(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)
    assert (out / "claude-code-log-cache.db").exists()
    # DECIDED #4: nothing is written into the sessions tree.
    assert not list(SESSIONS_ROOT.rglob("claude-code-log-cache.db"))


def test_walker_warm_run_is_byte_stable(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)
    cold = {
        str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*.html"))
    }

    render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)
    warm = {
        str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*.html"))
    }

    assert set(cold) == set(warm)
    assert all(cold[k] == warm[k] for k in cold)  # byte-stable warm vs cold


def test_walker_warm_run_skips_unchanged_pages(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)
    _tamper_all(out)

    render_provider_wholesale("codex", SESSIONS_ROOT, out, silent=True)

    for page in sorted(out.rglob("*.html")):
        rel = str(page.relative_to(out))
        if rel == "index.html":
            assert not _survived(page), "index must always be regenerated"
        else:
            assert _survived(page), f"{rel} should have been skipped (cache hit)"


def test_walker_cache_rerenders_only_changed_session(tmp_path: Path) -> None:
    tree = tmp_path / "sessions"
    a = _rollout(tree, "a.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")
    _rollout(tree, "b.jsonl", "20000000-0000-4000-8000-000000000002", "/proj/b")
    out = tmp_path / "ccl"

    render_provider_wholesale("codex", tree, out, silent=True)
    _tamper_all(out)

    # Grow session A's transcript (a new visible message → message-count drift,
    # which the cache detects regardless of the 1s mtime tolerance window).
    records = a.read_text(encoding="utf-8").rstrip("\n").split("\n")
    records.append(
        json.dumps(
            {
                "timestamp": "2026-01-02T00:00:03Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "one more turn"},
            }
        )
    )
    a.write_text("\n".join(records) + "\n", encoding="utf-8")

    render_provider_wholesale("codex", tree, out, silent=True)

    a_page = out / "-proj-a/session-10000000-0000-4000-8000-000000000001.html"
    b_page = out / "-proj-b/session-20000000-0000-4000-8000-000000000002.html"
    assert not _survived(a_page)  # changed session was re-rendered
    assert _survived(b_page)  # untouched session was skipped
    # A's project combined page also re-renders; B's does not.
    assert not _survived(out / "-proj-a/combined_transcripts.html")
    assert _survived(out / "-proj-b/combined_transcripts.html")


def test_walker_no_cache_rewrites_every_run(tmp_path: Path) -> None:
    out = tmp_path / "ccl"
    render_provider_wholesale("codex", SESSIONS_ROOT, out, use_cache=False, silent=True)
    _tamper_all(out)
    render_provider_wholesale("codex", SESSIONS_ROOT, out, use_cache=False, silent=True)

    # No cache DB is created, and every page is rewritten (no sentinel survives).
    assert not (out / "claude-code-log-cache.db").exists()
    for page in sorted(out.rglob("*.html")):
        assert not _survived(page), f"{page.relative_to(out)} should be rewritten"


# --------------------------------------------------------------------------
# M3 CLI flag flips: --no-cache / --clear-cache / --clear-output.
# --------------------------------------------------------------------------
def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_cli_provider_no_cache_renders_without_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    result = CliRunner().invoke(
        main, ["--provider", "codex", "--no-cache", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert not (out / "claude-code-log-cache.db").exists()


def test_cli_provider_clear_cache_removes_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    CliRunner().invoke(main, ["--provider", "codex", "-o", str(out)])
    assert (out / "claude-code-log-cache.db").exists()

    result = CliRunner().invoke(
        main, ["--provider", "codex", "--clear-cache", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert not (out / "claude-code-log-cache.db").exists()
    assert "Cleared provider cache" in result.output


def test_cli_provider_clear_output_removes_generated_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    CliRunner().invoke(main, ["--provider", "codex", "-o", str(out)])
    assert (out / "index.html").exists()
    assert list(out.rglob("session-*.html"))

    result = CliRunner().invoke(
        main, ["--provider", "codex", "--clear-output", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert not (out / "index.html").exists()
    assert not list(out.rglob("session-*.html"))
    assert not list(out.rglob("combined_transcripts*.html"))


def test_cli_provider_clear_output_never_touches_sessions_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DECIDED #4 pin: --clear-output removes generated files under the output
    root only; the pristine sessions tree is byte-identical afterwards and every
    rollout still present."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    home = _codex_home_with_sessions(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    sessions = home / "sessions"
    before = _snapshot_tree(sessions)
    assert before  # sanity: the tree has rollouts

    # Default output root is <codex_home>/claude-code-log/ (no -o).
    CliRunner().invoke(main, ["--provider", "codex"])
    result = CliRunner().invoke(main, ["--provider", "codex", "--clear-output"])
    assert result.exit_code == 0, result.output

    after = _snapshot_tree(sessions)
    assert after == before  # sessions tree byte-identical, every rollout intact


def test_cli_wholesale_for_provider_without_support_errors_loudly(
    tmp_path: Path,
) -> None:
    """A provider that doesn't implement the wholesale seams (agy) must fail
    LOUDLY, never crash or silently render nothing — the base default raises and
    the CLI surfaces it.

    --projects-dir supplies an explicit sessions root so the walker reaches
    discover_sessions_under (the NotImplementedError) deterministically, rather
    than short-circuiting on whether an agy data dir happens to exist in the
    environment (it does on a dev box, not on CI)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    root = tmp_path / "sessions"
    root.mkdir()
    result = CliRunner().invoke(
        main,
        ["--provider", "agy", "--projects-dir", str(root), "-o", str(tmp_path / "out")],
    )
    assert result.exit_code != 0
    assert "does not support wholesale rendering" in result.output


# --------------------------------------------------------------------------
# Review round: silent-divergence edges (monk MOD/LOW findings).
# --------------------------------------------------------------------------
def _sniff_only_rollout(path: Path, thread_id: str, cwd: str) -> Path:
    """A rollout whose NAME does not match rollout-*.jsonl but whose first line
    is a session_meta header (detected by sniff, not glob)."""
    records = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": cwd},
        },
        {
            "timestamp": "2026-01-02T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "sniff-only hello"},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_cli_sniff_only_directory_renders_not_silent_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory whose rollouts are recognised only by the session_meta sniff
    (non-rollout filenames) must render via the walker, NOT fall through to an
    empty Claude parse — symmetric with the single-file sniff path. Covers both
    auto-detect and explicit --provider."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.delenv("CODEX_HOME", raising=False)
    tree = tmp_path / "tree"
    _sniff_only_rollout(
        tree / "codex-session.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/s"
    )

    auto_out = tmp_path / "auto"
    auto = CliRunner().invoke(main, [str(tree), "-o", str(auto_out)])
    assert auto.exit_code == 0, auto.output
    assert "sniff-only hello" in (
        auto_out / "-proj-s" / "combined_transcripts.html"
    ).read_text(encoding="utf-8")

    explicit_out = tmp_path / "explicit"
    explicit = CliRunner().invoke(
        main, ["--provider", "codex", str(tree), "-o", str(explicit_out)]
    )
    assert explicit.exit_code == 0, explicit.output
    assert (explicit_out / "-proj-s" / "combined_transcripts.html").exists()


def test_cli_empty_provider_directory_errors_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory with no discoverable sessions must fail LOUDLY, never write
    an empty index and exit 0 (silent empty-success)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    empty = tmp_path / "empty"
    empty.mkdir()
    # A plain (non-rollout, non-sniff) .jsonl must not count.
    (empty / "notes.jsonl").write_text(
        json.dumps({"type": "user", "message": "hi"}) + "\n", encoding="utf-8"
    )
    result = CliRunner().invoke(
        main, ["--provider", "codex", str(empty), "-o", str(tmp_path / "out")]
    )
    assert result.exit_code != 0
    assert "No codex sessions found" in result.output


def test_cli_provider_clear_output_with_date_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--clear-output WITH a date filter clears then REGENERATES the filtered
    view (mirroring the Claude path), rather than clearing and leaving an empty
    directory."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    CliRunner().invoke(main, ["--provider", "codex", "-o", str(out)])
    assert list(out.rglob("session-*.html"))

    # A from-date that includes all fixture content: clear + rebuild.
    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--clear-output",
            "--from-date",
            "2020-01-01",
            "-o",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert list(out.rglob("session-*.html"))  # regenerated, not left empty


def test_cli_clear_output_without_date_clears_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast: with NO date filter, --clear-output clears and exits (no
    regeneration), so the directory is left empty."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    CliRunner().invoke(main, ["--provider", "codex", "-o", str(out)])
    result = CliRunner().invoke(
        main, ["--provider", "codex", "--clear-output", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert not (out / "index.html").exists()
    assert not list(out.rglob("session-*.html"))


def test_cli_all_projects_flag_runs_wholesale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--provider codex --all-projects` is an accepted synonym for bare
    wholesale — it walks the data dir into the project hierarchy, identically to
    bare `--provider codex` (was execution-verified only)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    out = tmp_path / "out"
    result = CliRunner().invoke(
        main, ["--provider", "codex", "--all-projects", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert (out / "-proj-a" / "combined_transcripts.html").exists()
    assert (out / "-proj-b" / "combined_transcripts.html").exists()


def test_cli_nonexistent_input_path_with_provider_errors_loudly(
    tmp_path: Path,
) -> None:
    """A nonexistent INPUT_PATH + --provider routes to single-file mode (a
    nonexistent path is not a directory) and errors loudly with rollout-not-found
    — never a silent empty render (was execution-verified only)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            str(tmp_path / "does-not-exist"),
            "-o",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "Codex rollout not found" in result.output


def test_cli_unknown_provider_is_clean_usage_error(tmp_path: Path) -> None:
    """An unknown --provider is a clean UsageError (exit 2), not a broad-except
    'Error converting file' (exit 1)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    result = CliRunner().invoke(
        main, ["--provider", "bogus", "-o", str(tmp_path / "out")]
    )
    assert result.exit_code == 2
    assert "Unknown provider: bogus" in result.output


def test_cli_single_session_still_rejects_cache_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache flags remain illegal in single-session mode (they only apply to
    the wholesale hierarchy)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    monkeypatch.setenv("CODEX_HOME", str(_codex_home_with_sessions(tmp_path)))
    result = CliRunner().invoke(
        main,
        ["--provider", "codex", "--session-id", "10000000", "--no-cache"],
    )
    assert result.exit_code != 0
    assert "does not support" in result.output and "--no-cache" in result.output
