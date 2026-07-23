"""INPUT_PATH auto-detection for the Codex provider (modalities M1).

The worst gap the modalities work closes: a Codex rollout handed to the CLI as
an INPUT_PATH used to fall through to the Claude parser, which skips every
record and emits a near-empty page. ``CodexProvider.detect_path`` is the cheap
sniff that routes such a path to the Codex pipeline instead. These tests pin the
sniff itself (filename pattern, first-line ``session_meta``, legacy header,
directory containment) on synthetic fixtures — no real transcript content.
"""

import json
from pathlib import Path

from claude_code_log.providers.base import BaseProvider
from claude_code_log.providers.codex import CodexProvider

_SESSION_META = {
    "type": "session_meta",
    "payload": {"id": "0123abcd-0000-4000-8000-000000000001", "cwd": "/workspace"},
}


def _write(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_detects_rollout_by_filename(tmp_path: Path) -> None:
    # Filename pattern alone is a positive signal (body not consulted).
    path = _write(tmp_path / "rollout-2026-07-24T10-00-00-abcd.jsonl", [{"x": 1}])
    assert CodexProvider().detect_path(path) is True


def test_detects_rollout_by_session_meta_first_line(tmp_path: Path) -> None:
    # A non-rollout filename still detects via the first-line session_meta sniff.
    path = _write(tmp_path / "export.jsonl", [_SESSION_META, {"type": "message"}])
    assert CodexProvider().detect_path(path) is True


def test_detects_legacy_flat_header(tmp_path: Path) -> None:
    # Legacy rollouts: no ``type`` but a top-level ``id`` header.
    path = _write(tmp_path / "old.jsonl", [{"id": "abc", "cwd": "/w"}, {"x": 1}])
    assert CodexProvider().detect_path(path) is True


def test_does_not_detect_claude_transcript(tmp_path: Path) -> None:
    # A Claude-style transcript (no rollout name, no session_meta) is NOT a
    # rollout — it must keep flowing to the Claude parser, not be hijacked.
    path = _write(
        tmp_path / "session.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "hi"}}],
    )
    assert CodexProvider().detect_path(path) is False


def test_does_not_detect_garbage_or_empty(tmp_path: Path) -> None:
    garbage = tmp_path / "notes.jsonl"
    garbage.write_text("this is not json\n", encoding="utf-8")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert CodexProvider().detect_path(garbage) is False
    assert CodexProvider().detect_path(empty) is False


def test_detects_directory_containing_a_rollout(tmp_path: Path) -> None:
    nested = tmp_path / "sessions" / "2026" / "07"
    nested.mkdir(parents=True)
    _write(nested / "rollout-abcd.jsonl", [_SESSION_META])
    assert CodexProvider().detect_path(tmp_path) is True


def test_does_not_detect_directory_without_rollouts(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "plain.jsonl", [{"type": "user"}])
    assert CodexProvider().detect_path(tmp_path) is False


def test_directory_discovery_keeps_symlink_containment(tmp_path: Path) -> None:
    # A rollout reachable only via a symlink escaping the root must not count —
    # mirrors the loader's containment rule so an INPUT_PATH dir can't pull in
    # outside files.
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "rollout-escape.jsonl", [_SESSION_META])
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # platform without symlink support — skip the containment check
    assert CodexProvider().detect_path(root) is False


def test_base_provider_default_is_no_detection(tmp_path: Path) -> None:
    # The base contract opts out — only providers that override detect_path
    # participate in auto-detection.
    class _Dummy(BaseProvider):
        def get_provider_name(self) -> str:
            return "dummy"

        def get_session_format(self) -> str:
            return "dummy"

        def get_data_dir(self) -> Path | None:
            return None

        def discover_sessions(self):  # type: ignore[no-untyped-def]
            return iter(())

        def load_session(self, session_id, max_messages=None):  # type: ignore[no-untyped-def]
            return iter(())

    path = _write(tmp_path / "rollout-x.jsonl", [_SESSION_META])
    assert _Dummy().detect_path(path) is False
