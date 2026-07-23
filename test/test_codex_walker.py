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

    def _spy(self: HtmlRenderer, project_summaries, *args, **kwargs):  # type: ignore[no-untyped-def]
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

    claude_session_keys = set(claude_summaries[0]["sessions"][0])  # type: ignore[index]
    walker_session_keys = set(walker_summaries[0]["sessions"][0])  # type: ignore[index]
    assert claude_session_keys == walker_session_keys, (
        "session-summary dict drift — "
        f"claude-only={claude_session_keys - walker_session_keys}, "
        f"walker-only={walker_session_keys - claude_session_keys}"
    )
