"""Characterization tests for the converter's session-metadata
derivation paths — the safety net for opp 9 (C9b).

opp 9 will extract `compute_session_data` / `compute_project_aggregates`
out of `_update_cache_with_session_data` and route both
`_build_session_data_from_messages` (pagination cache-miss fallback)
and the inline-aggregate loop in `process_projects_hierarchy`
through them. That refactor changes two behaviors *deliberately*
(maintainer decisions D1 + D2):

- **D1 — un-keyed assistant `usage` is now COUNTED** (the cache path
  currently zeroes it; the fallback already counts it).
- **D2 — `PassthroughTranscriptEntry` is now COUNTED in
  `message_count`** (the cache path already counts it; the fallback
  currently excludes it).

These tests pin the CURRENT behavior at each call site so the C9b
PR's diff makes the D1/D2 decisions visible as test deltas (a test
file change is part of C9b, not a regression). Anything else moving
is a real regression and must stop C9b.

The fixture deliberately exercises both D1 and D2 triggers:

- One assistant with a `requestId` and `usage` (the typical case).
- One assistant **without** a `requestId` (D1 trigger).
- One `PassthroughTranscriptEntry` (D2 trigger).
- One user and one assistant in a *second* session (to pin
  per-session totals and the session-id set).

A second smaller fixture (with no D1/D2 entries) pins the typical
case so C9b regressions on it are caught even on the most common
shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.cache import CacheManager
from claude_code_log.cli import get_library_version
from claude_code_log.converter import (
    _build_session_data_from_messages,
    _update_cache_with_session_data,
    load_transcript,
    process_projects_hierarchy,
)


def _symlinks_supported() -> bool:
    """Whether the OS lets this process create symlinks.

    On Windows, ``os.symlink`` raises ``OSError [WinError 1314]`` unless the
    process is elevated or Developer Mode is on, so the symlink-based tests
    below can't run there. Probe once at import time.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        link = Path(td) / "probe-link"
        try:
            link.symlink_to(Path(td))
        except (OSError, NotImplementedError):
            return False
    return True


_SYMLINKS_SUPPORTED = _symlinks_supported()


# ----- fixture builders ----------------------------------------------------


def _user_entry(
    uuid: str,
    parent_uuid: str | None,
    text: str,
    *,
    session_id: str,
    timestamp: str,
    cwd: str = "/tmp/proj",
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "human",
        "cwd": cwd,
        "sessionId": session_id,
        "version": "1.0.0",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_entry(
    uuid: str,
    parent_uuid: str | None,
    text: str,
    *,
    session_id: str,
    timestamp: str,
    request_id: str | None,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_creation: int = 2,
    cache_read: int = 3,
    cwd: str = "/tmp/proj",
) -> dict[str, Any]:
    """``request_id=None`` omits the key entirely — mirrors real
    pre-requestId transcripts and triggers D1 behavior at the
    call sites that drop un-keyed usage."""
    entry: dict[str, Any] = {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "human",
        "cwd": cwd,
        "sessionId": session_id,
        "version": "1.0.0",
        "message": {
            "id": uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if request_id is not None:
        entry["requestId"] = request_id
    return entry


def _passthrough_entry(
    uuid: str,
    parent_uuid: str | None,
    *,
    session_id: str,
    timestamp: str,
) -> dict[str, Any]:
    """A ``type: progress`` Passthrough — has uuid/parentUuid and
    participates in the DAG chain but carries no conversational
    content. Triggers D2 behavior at the call sites that include or
    exclude it from ``message_count``."""
    return {
        "type": "progress",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "sessionId": session_id,
        "timestamp": timestamp,
        "isSidechain": False,
    }


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


# ----- fixtures ------------------------------------------------------------


@pytest.fixture()
def synthetic_d1_d2_messages(tmp_path: Path):
    """Build a 2-session project that fires both D1 (un-keyed
    assistant usage) and D2 (Passthrough entry) triggers."""
    s1 = "session-one"
    s2 = "session-two"
    raw = [
        _user_entry(
            "a", None, "hello s1", session_id=s1, timestamp="2025-07-01T10:00:00Z"
        ),
        _assistant_entry(
            "b",
            "a",
            "hi",
            session_id=s1,
            timestamp="2025-07-01T10:01:00Z",
            request_id="req-1",  # KEYED, counted
            input_tokens=10,
            output_tokens=5,
            cache_creation=2,
            cache_read=3,
        ),
        # D1 trigger: un-keyed assistant usage. Cache path drops it;
        # fallback counts it.
        _assistant_entry(
            "c",
            "b",
            "follow-up no requestId",
            session_id=s1,
            timestamp="2025-07-01T10:02:00Z",
            request_id=None,
            input_tokens=100,
            output_tokens=50,
            cache_creation=20,
            cache_read=30,
        ),
        # D2 trigger: PassthroughTranscriptEntry. Cache path counts
        # it in message_count; fallback excludes it.
        _passthrough_entry(
            "d",
            "c",
            session_id=s1,
            timestamp="2025-07-01T10:03:00Z",
        ),
        # Session 2 — totally separate (no agent re-parenting).
        _user_entry(
            "e", None, "hello s2", session_id=s2, timestamp="2025-07-01T11:00:00Z"
        ),
        _assistant_entry(
            "f",
            "e",
            "s2 reply",
            session_id=s2,
            timestamp="2025-07-01T11:01:00Z",
            request_id="req-2",
            input_tokens=7,
            output_tokens=3,
            cache_creation=0,
            cache_read=0,
        ),
    ]
    project_dir = tmp_path / "synthetic-project"
    project_dir.mkdir()
    _write_jsonl(project_dir / f"{s1}.jsonl", raw[:4])
    _write_jsonl(project_dir / f"{s2}.jsonl", raw[4:])
    # Parse via load_transcript so the parse path is realistic. Note:
    # load_transcript with no cache returns a single file's entries;
    # for cross-file projects we just concatenate (no agent splicing
    # in this fixture).
    messages_s1 = load_transcript(project_dir / f"{s1}.jsonl")
    messages_s2 = load_transcript(project_dir / f"{s2}.jsonl")
    return project_dir, [*messages_s1, *messages_s2], s1, s2


@pytest.fixture()
def synthetic_typical_messages(tmp_path: Path):
    """A boring well-formed project — no D1/D2 triggers — to pin the
    common-case totals as the baseline."""
    sid = "typical-session"
    raw = [
        _user_entry("u1", None, "hi", session_id=sid, timestamp="2025-08-01T10:00:00Z"),
        _assistant_entry(
            "a1",
            "u1",
            "ack",
            session_id=sid,
            timestamp="2025-08-01T10:01:00Z",
            request_id="r1",
            input_tokens=4,
            output_tokens=2,
            cache_creation=1,
            cache_read=1,
        ),
        _user_entry(
            "u2", "a1", "more", session_id=sid, timestamp="2025-08-01T10:02:00Z"
        ),
        _assistant_entry(
            "a2",
            "u2",
            "ack again",
            session_id=sid,
            timestamp="2025-08-01T10:03:00Z",
            request_id="r2",
            input_tokens=8,
            output_tokens=4,
            cache_creation=0,
            cache_read=2,
        ),
    ]
    project_dir = tmp_path / "typical-project"
    project_dir.mkdir()
    _write_jsonl(project_dir / f"{sid}.jsonl", raw)
    return project_dir, load_transcript(project_dir / f"{sid}.jsonl"), sid


# ----- characterization: _build_session_data_from_messages -----------------


class TestBuildSessionDataCharacterization:
    """Pin the outputs of the pagination cache-miss fallback
    (`_build_session_data_from_messages`). Post-C9b: routes through
    the shared `compute_session_data` helper. The D2 delta (count
    Passthrough in `message_count`) is the visible flip on this
    side; D1 was already opp-1 behavior here (count un-keyed usage),
    so it's unchanged.
    """

    def test_d1_d2_fixture_pinned(self, synthetic_d1_d2_messages):
        _project_dir, messages, s1, s2 = synthetic_d1_d2_messages
        out = _build_session_data_from_messages(messages)

        # Session set: only the two real sessions appear, no
        # synthetic agent ids. (Passthrough doesn't manifest as its
        # own session.)
        assert set(out.keys()) == {s1, s2}

        # === Session 1: includes un-keyed assistant (D1 counted —
        # was already opp-1 behavior here) AND now includes the
        # Passthrough entry in message_count (D2 flip).
        s1_data = out[s1]
        # 1 user + 2 assistants + 1 Passthrough = 4 (D2 flip:
        # was 3 pre-C9b; Passthrough now counted everywhere).
        assert s1_data.message_count == 4, (
            "post-C9b: PassthroughTranscriptEntry counts in "
            f"message_count on the fallback path too; got {s1_data.message_count}"
        )
        # Both assistant usages counted: 10+100, 5+50, 2+20, 3+30.
        assert s1_data.total_input_tokens == 110
        assert s1_data.total_output_tokens == 55
        assert s1_data.total_cache_creation_tokens == 22
        assert s1_data.total_cache_read_tokens == 33

        # === Session 2: single user + assistant.
        s2_data = out[s2]
        assert s2_data.message_count == 2
        assert s2_data.total_input_tokens == 7
        assert s2_data.total_output_tokens == 3
        assert s2_data.total_cache_creation_tokens == 0
        assert s2_data.total_cache_read_tokens == 0

    def test_typical_fixture_pinned(self, synthetic_typical_messages):
        _project_dir, messages, sid = synthetic_typical_messages
        out = _build_session_data_from_messages(messages)

        assert set(out.keys()) == {sid}
        data = out[sid]
        assert data.message_count == 4  # 2u + 2a
        assert data.total_input_tokens == 12
        assert data.total_output_tokens == 6
        assert data.total_cache_creation_tokens == 1
        assert data.total_cache_read_tokens == 3


# ----- characterization: _update_cache_with_session_data -------------------


class TestUpdateCacheCharacterization:
    """Pin the outputs of the canonical cache path
    (`_update_cache_with_session_data`). Post-C9b: thin wrapper over
    `compute_session_data` + `compute_project_aggregates`. The D1
    delta (count un-keyed usage) is the visible flip on this side;
    D2 was already cache-path behavior here (count Passthrough),
    so it's unchanged.
    """

    def _cache(self, project_dir: Path, tmp_path: Path) -> CacheManager:
        return CacheManager(
            project_dir,
            library_version=get_library_version(),
            db_path=tmp_path / "characterization.db",
        )

    def test_d1_d2_fixture_pinned(self, tmp_path, synthetic_d1_d2_messages):
        project_dir, messages, s1, s2 = synthetic_d1_d2_messages
        cm = self._cache(project_dir, tmp_path)

        _update_cache_with_session_data(cm, messages)
        cached = cm.get_cached_project_data()
        assert cached is not None
        sessions = cached.sessions

        assert set(sessions.keys()) == {s1, s2}

        # === Session 1: cache path counts Passthrough in
        # message_count (unchanged from pre-C9b — was always cache
        # behavior) AND NOW counts the un-keyed assistant's usage
        # (D1 flip: pre-C9b's truthy guard silently dropped it; the
        # unified rule counts it).
        s1_data = sessions[s1]
        # 1 user + 2 assistants + 1 Passthrough = 4.
        assert s1_data.message_count == 4
        # Both assistant usages counted (D1 flip): 10+100, 5+50,
        # 2+20, 3+30. Pre-C9b cache path returned 10/5/2/3.
        assert s1_data.total_input_tokens == 110, (
            "post-C9b: cache path now counts un-keyed assistant usage; "
            f"got {s1_data.total_input_tokens}"
        )
        assert s1_data.total_output_tokens == 55
        assert s1_data.total_cache_creation_tokens == 22
        assert s1_data.total_cache_read_tokens == 33

        # === Session 2: identical shape to fallback (no D1/D2
        # entries in this session).
        s2_data = sessions[s2]
        assert s2_data.message_count == 2
        assert s2_data.total_input_tokens == 7
        assert s2_data.total_output_tokens == 3

    def test_typical_fixture_matches_fallback(
        self, tmp_path, synthetic_typical_messages
    ):
        """On a fixture with NO D1/D2 entries, the cache path and
        the fallback path produce IDENTICAL `SessionCacheData`
        contents. C9b must preserve this property — divergences on
        the typical case would be a regression.
        """
        project_dir, messages, sid = synthetic_typical_messages
        cm = self._cache(project_dir, tmp_path)

        _update_cache_with_session_data(cm, messages)
        cached = cm.get_cached_project_data()
        assert cached is not None
        cache_data = cached.sessions[sid]

        fallback = _build_session_data_from_messages(messages)[sid]

        # Pin every comparable field is equal between the two paths
        # on the typical fixture.
        assert cache_data.message_count == fallback.message_count == 4
        assert cache_data.total_input_tokens == fallback.total_input_tokens == 12
        assert cache_data.total_output_tokens == fallback.total_output_tokens == 6
        assert (
            cache_data.total_cache_creation_tokens
            == fallback.total_cache_creation_tokens
            == 1
        )
        assert (
            cache_data.total_cache_read_tokens == fallback.total_cache_read_tokens == 3
        )

    def test_d1_d2_fixture_cache_equals_fallback(
        self, tmp_path, synthetic_d1_d2_messages
    ):
        """Post-C9b: the cache path and fallback path produce
        IDENTICAL ``SessionCacheData`` on the D1/D2 fixture too —
        because the unified ``compute_session_data`` helper applies
        the same D1/D2 rules at both call sites. The pre-C9b
        cache-vs-fallback divergence on this fixture (cache
        message_count=4 + tokens=10; fallback message_count=3 +
        tokens=110) is exactly the gap C9b closes.
        """
        project_dir, messages, s1, s2 = synthetic_d1_d2_messages
        cm = self._cache(project_dir, tmp_path)

        _update_cache_with_session_data(cm, messages)
        cached = cm.get_cached_project_data()
        assert cached is not None
        fallback = _build_session_data_from_messages(messages)

        # Same session set.
        assert set(cached.sessions.keys()) == set(fallback.keys()) == {s1, s2}

        # Same per-session totals on s1 (the D1/D2 trigger session).
        c1, f1 = cached.sessions[s1], fallback[s1]
        assert c1.message_count == f1.message_count == 4  # incl. Passthrough
        assert c1.total_input_tokens == f1.total_input_tokens == 110  # incl. un-keyed
        assert c1.total_output_tokens == f1.total_output_tokens == 55
        assert c1.total_cache_creation_tokens == f1.total_cache_creation_tokens == 22
        assert c1.total_cache_read_tokens == f1.total_cache_read_tokens == 33

        # And on s2 (no D1/D2 triggers, sanity).
        c2, f2 = cached.sessions[s2], fallback[s2]
        assert c2.message_count == f2.message_count == 2
        assert c2.total_input_tokens == f2.total_input_tokens == 7
        assert c2.total_output_tokens == f2.total_output_tokens == 3


# ----- characterization: index inline-aggregate loop -----------------------


@pytest.mark.skipif(
    not _SYMLINKS_SUPPORTED,
    reason="symlink creation requires privilege/Developer Mode on Windows",
)
class TestIndexInlineAggregateLoopCharacterization:
    """Pin the project-aggregate totals produced by the inline loop
    inside `process_projects_hierarchy` (the cache-unavailable
    fallback path, ~converter.py:2761-2812). C9b will replace this
    loop with a call to the new `compute_project_aggregates` helper;
    the totals it produces here must continue to match (with the D1
    delta surfacing as a deliberate change once the shared helper
    adopts the count-un-keyed-usage rule).

    Strategy: drive `process_projects_hierarchy` end-to-end with the
    cache forcibly unavailable (so the fallback path runs), then
    inspect the rendered project card in `index.html` for the
    aggregate totals. The card's text is the only externally
    observable output of the inline loop.
    """

    def _drive_with_cache_disabled(self, projects_root: Path, output_dir: Path) -> str:
        """Run `process_projects_hierarchy` with the cache mocked to
        return None, so the inline-aggregate loop runs. Returns the
        rendered `index.html` text.
        """
        from unittest.mock import patch

        # Patch get_cached_project_data on the class so EVERY
        # per-project instance returns None — forces the fallback
        # for every project encountered.
        with patch.object(
            CacheManager,
            "get_cached_project_data",
            return_value=None,
        ):
            process_projects_hierarchy(
                projects_root,
                output_format="html",
                output_dir=output_dir,
                silent=True,
            )

        index_html = output_dir / "index.html"
        assert index_html.exists(), "index.html should be generated"
        return index_html.read_text(encoding="utf-8")

    # Stable rendered seam in the project card: a single label line
    # of the form
    #   ``Input: N | Output: N | Cache Creation: N | Cache Read: N``.
    # Match the whole line in one shot so we anchor on all four
    # numbers together — robust against ``<input>`` HTML elements
    # elsewhere in the page (the search UI uses them).
    _TOKEN_LINE_RE = re.compile(
        r"Input:\s*([\d,]+)\s*\|\s*Output:\s*([\d,]+)"
        r"\s*\|\s*Cache Creation:\s*([\d,]+)"
        r"\s*\|\s*Cache Read:\s*([\d,]+)"
    )

    def _extract_token_totals(self, html: str) -> tuple[int, int, int, int]:
        match = self._TOKEN_LINE_RE.search(html)
        assert match is not None, (
            "could not find rendered token-line in index.html "
            "(format: 'Input: N | Output: N | Cache Creation: N | "
            f"Cache Read: N'); first 500 chars:\n{html[:500]}"
        )
        return tuple(int(g.replace(",", "")) for g in match.groups())  # type: ignore[return-value]

    def test_d1_d2_fixture_aggregates(self, tmp_path, synthetic_d1_d2_messages):
        project_dir, _messages, _s1, _s2 = synthetic_d1_d2_messages
        # process_projects_hierarchy expects a directory of projects.
        # Move our single project under a wrapper dir to match that.
        projects_root = tmp_path / "projects-root"
        projects_root.mkdir()
        # Symlink the synthetic project into the projects root so the
        # converter discovers it.
        (projects_root / project_dir.name).symlink_to(project_dir)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        html = self._drive_with_cache_disabled(projects_root, output_dir)

        # Post-C9b: the index path now routes through the shared
        # ``compute_project_aggregates`` helper, which applies the
        # unified D1 rule: count un-keyed assistant usage, dedup
        # repeats of a present requestId. So s1's three assistant
        # entries all contribute (10 keyed + 100 un-keyed) and s2's
        # one contributes (7 keyed): 117, 58, 22, 33.
        # PassthroughTranscriptEntry carries no usage, so it doesn't
        # affect token totals (D2 affects message_count, pinned at
        # the cache + fallback layers; for the index-layer message_
        # count pinning see ``test_d1_d2_fixture_message_count_via_cache``
        # below).
        input_total, output_total, cache_create, cache_read = (
            self._extract_token_totals(html)
        )
        assert input_total == 117, (
            "post-C9b: index inline path counts un-keyed assistant "
            f"usage via compute_project_aggregates; got input_total={input_total}"
        )
        assert output_total == 58
        assert cache_create == 22
        assert cache_read == 33

    def test_typical_fixture_aggregates(self, tmp_path, synthetic_typical_messages):
        project_dir, _messages, _sid = synthetic_typical_messages
        projects_root = tmp_path / "projects-root"
        projects_root.mkdir()
        (projects_root / project_dir.name).symlink_to(project_dir)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        html = self._drive_with_cache_disabled(projects_root, output_dir)
        input_total, output_total, cache_create, cache_read = (
            self._extract_token_totals(html)
        )
        # 4 + 8 = 12, 2 + 4 = 6 (both keyed; the typical case has
        # no un-keyed assistant, so the inline loop produces the
        # obvious total — no D1 effect to mask).
        assert input_total == 12
        assert output_total == 6
        assert cache_create == 1
        assert cache_read == 3

    def test_d1_d2_fixture_message_count_via_cache(
        self, tmp_path, synthetic_d1_d2_messages
    ):
        """Pin D2 at the index layer too.

        The rendered project card surfaces token totals (token line),
        but ``message_count`` is exposed via the cached
        ``SessionCacheData`` rather than a stable text seam on the
        card. During the index-fallback path,
        ``process_projects_hierarchy`` calls
        ``_update_cache_with_session_data`` upfront (so the cache is
        rebuilt from the same `messages` the inline loop then
        aggregates). Reading the cache post-run lets us assert the
        D2 rule (Passthrough counted in message_count) holds at the
        index layer, closing the gap monk flagged in #3446 —
        message_count would otherwise be silently unpinned on this
        site.
        """
        project_dir, _messages, s1, s2 = synthetic_d1_d2_messages
        projects_root = tmp_path / "projects-root"
        projects_root.mkdir()
        (projects_root / project_dir.name).symlink_to(project_dir)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Drive the index fallback so the cache write inside it runs
        # on this fixture's messages.
        self._drive_with_cache_disabled(projects_root, output_dir)

        # Read back from the actual cache that `process_projects_
        # hierarchy` populated. The default cache DB path is
        # ``<project_parent>/claude-code-log-cache.db``; since the
        # converter walked through ``projects_root/<project_name>``
        # (the symlink path), its CacheManager keyed off
        # ``projects_root``. Reuse that same parent so we hit the
        # right DB.
        cache = CacheManager(
            projects_root / project_dir.name,
            library_version=get_library_version(),
        )
        cached = cache.get_cached_project_data()
        assert cached is not None
        # D2 at the index layer: Passthrough counts.
        assert cached.sessions[s1].message_count == 4
        assert cached.sessions[s2].message_count == 2
