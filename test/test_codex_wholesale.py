"""Root-scoped wholesale primitives for the Codex provider (modalities M2).

The wholesale walker needs to discover and load every session under an
arbitrary root — the provider's own data dir, or a directory handed in as an
INPUT_PATH — while keeping sibling context (fork-prefix stripping) that the
standalone ``load_session_from_path`` deliberately drops. These tests pin the
two new provider-neutral seams (``discover_sessions_under`` /
``load_session_under``) and the index memoization that keeps a wholesale run
from re-reading every rollout header once per session (O(n²)).
"""

import json
from pathlib import Path

import pytest

from claude_code_log.models import (
    AssistantTranscriptEntry,
    TranscriptEntry,
    UserTranscriptEntry,
)
from claude_code_log.providers.agy import AgyProvider
from claude_code_log.providers.codex import CodexProvider

FIXTURES = Path(__file__).parent / "test_data" / "codex"
SESSIONS_ROOT = FIXTURES / "sessions"
PARENT_ID = "22222222-2222-4222-8222-222222222222"
CHILD_ID = "33333333-3333-4333-8333-333333333333"


def _visible_text(entries: list[TranscriptEntry]) -> list[str]:
    texts: list[str] = []
    for entry in entries:
        if not isinstance(entry, (UserTranscriptEntry, AssistantTranscriptEntry)):
            continue
        for item in entry.message.content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                texts.append(text)
    return texts


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
            "payload": {"type": "user_message", "message": f"hi from {thread_id[:4]}"},
        },
        {
            "timestamp": "2026-01-02T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": f"bye from {thread_id[:4]}",
            },
        },
    ]
    # Discovery only walks ``rollout-*.jsonl`` — prefix the basename so the
    # synthetic tree is found the same way a real sessions tree is.
    rel_path = Path(rel)
    path = tmp / rel_path.parent / f"rollout-{rel_path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# discover_sessions_under
# --------------------------------------------------------------------------
def test_discover_under_matches_data_dir_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovering the fixture ``sessions/`` root directly must reproduce
    exactly what ``discover_sessions`` (pinned to CODEX_HOME) yields, including
    the child's inherited-prefix lineage — sibling context is honored under an
    explicit root."""
    monkeypatch.setenv("CODEX_HOME", str(FIXTURES))
    provider = CodexProvider()

    via_data_dir = {s.session_id: s.project_path for s in provider.discover_sessions()}
    via_root = {
        s.session_id: s.project_path
        for s in CodexProvider().discover_sessions_under(SESSIONS_ROOT)
    }
    assert via_root == via_data_dir

    child = next(
        s
        for s in CodexProvider().discover_sessions_under(SESSIONS_ROOT)
        if s.session_id == CHILD_ID
    )
    # The frozen lineage fields only populate when the parent is a discoverable
    # sibling within the walked root.
    assert getattr(child, "parent_thread_id") == PARENT_ID
    assert getattr(child, "inherited_prefix_records") == 2


def test_discover_under_groups_by_cwd(tmp_path: Path) -> None:
    """A mini sessions root of 2 cwds × 2 sessions + 1 no-cwd — the walker's
    grouping input: each session carries its cwd as ``project_path`` (None for
    the no-cwd bucket)."""
    _rollout(tmp_path, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/proj/a")
    _rollout(tmp_path, "a/two.jsonl", "10000000-0000-4000-8000-000000000002", "/proj/a")
    _rollout(tmp_path, "b/one.jsonl", "20000000-0000-4000-8000-000000000001", "/proj/b")
    _rollout(tmp_path, "b/two.jsonl", "20000000-0000-4000-8000-000000000002", "/proj/b")
    _rollout(tmp_path, "c/orphan.jsonl", "30000000-0000-4000-8000-000000000001", None)

    infos = list(CodexProvider().discover_sessions_under(tmp_path))
    by_cwd: dict[str | None, int] = {}
    for info in infos:
        key = str(info.project_path) if info.project_path is not None else None
        by_cwd[key] = by_cwd.get(key, 0) + 1

    assert by_cwd == {"/proj/a": 2, "/proj/b": 2, None: 1}


def test_discover_under_is_deterministic(tmp_path: Path) -> None:
    _rollout(tmp_path, "x/one.jsonl", "10000000-0000-4000-8000-000000000001", "/p")
    _rollout(tmp_path, "x/two.jsonl", "10000000-0000-4000-8000-000000000002", "/p")
    _rollout(tmp_path, "y/three.jsonl", "20000000-0000-4000-8000-000000000003", "/q")

    first = [s.session_id for s in CodexProvider().discover_sessions_under(tmp_path)]
    second = [s.session_id for s in CodexProvider().discover_sessions_under(tmp_path)]
    assert first == second
    assert first == sorted(first)  # discovery is a deterministic sorted walk


# --------------------------------------------------------------------------
# load_session_under
# --------------------------------------------------------------------------
def test_load_under_round_trips(tmp_path: Path) -> None:
    tid = "10000000-0000-4000-8000-000000000001"
    _rollout(tmp_path, "a/one.jsonl", tid, "/proj/a")
    entries = list(CodexProvider().load_session_under(tmp_path, tid))
    assert _visible_text(entries) == ["hi from 1000", "bye from 1000"]


def test_load_under_strips_inherited_prefix_vs_standalone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason ``load_session_under`` exists: within a root it strips the
    child's inherited prefix (present as a sibling parent), whereas the
    standalone ``load_session_from_path`` renders the file verbatim. The rooted
    load must therefore yield strictly fewer entries."""
    monkeypatch.setenv("CODEX_HOME", str(FIXTURES))
    provider = CodexProvider()
    child_file = next(SESSIONS_ROOT.rglob(f"*{CHILD_ID}.jsonl"))

    rooted = list(provider.load_session_under(SESSIONS_ROOT, CHILD_ID))
    standalone = list(CodexProvider().load_session_from_path(child_file))
    assert 0 < len(rooted) < len(standalone)


def test_load_under_missing_id_raises(tmp_path: Path) -> None:
    _rollout(tmp_path, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/p")
    with pytest.raises(FileNotFoundError):
        list(CodexProvider().load_session_under(tmp_path, "deadbeef-dead-4dead-8dead"))


# --------------------------------------------------------------------------
# index memoization (perf guard: one index build per root per run)
# --------------------------------------------------------------------------
def test_session_index_is_memoized_per_root(tmp_path: Path) -> None:
    _rollout(tmp_path, "a/one.jsonl", "10000000-0000-4000-8000-000000000001", "/p")
    provider = CodexProvider()

    walked: list[Path] = []
    original = provider._rollout_paths

    def _spy(root: Path) -> list[Path]:
        walked.append(root)
        return original(root)

    # Swap the bound method on this throwaway instance to count tree walks.
    provider._rollout_paths = _spy  # type: ignore[method-assign]

    first = provider._session_index(tmp_path)
    second = provider._session_index(tmp_path)
    assert first is second  # same cached object
    assert len(walked) == 1  # the second lookup did not re-walk the tree


# --------------------------------------------------------------------------
# base default: providers that don't support wholesale rendering opt out loudly
# --------------------------------------------------------------------------
def test_wholesale_default_raises_for_non_participating_provider(
    tmp_path: Path,
) -> None:
    provider = AgyProvider()
    with pytest.raises(NotImplementedError):
        list(provider.discover_sessions_under(tmp_path))
    with pytest.raises(NotImplementedError):
        list(provider.load_session_under(tmp_path, "some-id"))
