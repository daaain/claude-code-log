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
from typing import Iterator, Optional

import pytest

from claude_code_log.models import TranscriptEntry
from claude_code_log.providers.base import BaseProvider, SessionInfo
from claude_code_log.providers.codex import CodexProvider
from claude_code_log.providers.registry import ProviderRegistry

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


# --------------------------------------------------------------------------
# Registry routing + standalone-path load.
# --------------------------------------------------------------------------
class _AlwaysDetects(BaseProvider):
    """A stub provider that claims every path (to force an ambiguous match)."""

    def get_provider_name(self) -> str:
        return "stub"

    def get_session_format(self) -> str:
        return "stub"

    def get_data_dir(self) -> Optional[Path]:
        return None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        return iter(())

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        return iter(())

    def detect_path(self, path: Path) -> bool:
        return True


def _registry(*providers: BaseProvider) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def test_registry_detects_codex_for_rollout(tmp_path: Path) -> None:
    registry = _registry(CodexProvider())
    path = _write(tmp_path / "rollout-x.jsonl", [_SESSION_META])
    assert registry.detect_provider_for_path(path) == "codex"


def test_registry_returns_none_for_claude_transcript(tmp_path: Path) -> None:
    # Detection is independent of is_available; a non-rollout still yields None
    # (falls through to the Claude default path).
    registry = _registry(CodexProvider())
    path = _write(tmp_path / "session.jsonl", [{"type": "user"}])
    assert registry.detect_provider_for_path(path) is None


def test_registry_raises_on_ambiguous_match(tmp_path: Path) -> None:
    registry = _registry(CodexProvider(), _AlwaysDetects())
    path = _write(tmp_path / "rollout-x.jsonl", [_SESSION_META])
    with pytest.raises(ValueError, match="multiple providers"):
        registry.detect_provider_for_path(path)


def test_load_session_from_path_loads_standalone_rollout() -> None:
    fixture = Path(
        "test/test_data/codex/sessions/2026/01/02/"
        "rollout-2026-01-02T03-04-05-11111111-1111-4111-8111-111111111111.jsonl"
    )
    entries = list(CodexProvider().load_session_from_path(fixture))
    assert entries, "standalone rollout should yield entries, not an empty page"
    roles = {getattr(getattr(e, "message", None), "role", None) for e in entries}
    assert "user" in roles and "assistant" in roles


def test_load_session_from_path_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(CodexProvider().load_session_from_path(tmp_path / "nope.jsonl"))


# --------------------------------------------------------------------------
# CLI dispatch: a rollout INPUT_PATH renders via the provider, never empty.
# --------------------------------------------------------------------------
_FIXTURE_ROLLOUT = Path(
    "test/test_data/codex/sessions/2026/01/02/"
    "rollout-2026-01-02T03-04-05-11111111-1111-4111-8111-111111111111.jsonl"
)


@pytest.mark.parametrize(
    ("fmt", "ext"), [("html", "html"), ("md", "md"), ("json", "json")]
)
def test_rollout_input_path_renders_via_codex(
    tmp_path: Path, fmt: str, ext: str
) -> None:
    """A bare rollout INPUT_PATH (no --provider) auto-detects to codex and
    renders the actual conversation — asserting CONTENT, not just a non-empty
    file, because a wrong render key silently produced a full-size-but-empty
    page (regression pin)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    out = tmp_path / f"rollout.{ext}"
    result = CliRunner().invoke(
        main, [str(_FIXTURE_ROLLOUT), "-o", str(out), "--format", fmt]
    )
    assert result.exit_code == 0, result.output
    assert "synthetic files" in out.read_text(encoding="utf-8")


def test_rollout_that_yields_no_messages_errors_loudly(tmp_path: Path) -> None:
    """A detected rollout that produces no renderable messages must fail LOUDLY,
    never fall through to a near-empty page (the worst gap)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    rollout = _write(tmp_path / "rollout-empty.jsonl", [_SESSION_META])
    result = CliRunner().invoke(main, [str(rollout), "-o", str(tmp_path / "e.html")])
    assert result.exit_code != 0
    assert "no renderable messages" in result.output


def test_explicit_provider_renders_rollout_file(tmp_path: Path) -> None:
    """`--provider codex <rollout>` renders that file directly (fence relaxed
    from the old blanket INPUT_PATH rejection), asserting real content."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    out = tmp_path / "explicit.html"
    result = CliRunner().invoke(
        main, ["--provider", "codex", str(_FIXTURE_ROLLOUT), "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "synthetic files" in out.read_text(encoding="utf-8")


def test_rollout_directory_errors_loudly_until_walker_lands(tmp_path: Path) -> None:
    """Interim guard: a directory of rollouts must NOT fall through to the empty
    Claude parse before the wholesale walker lands. Both auto-detect and
    explicit --provider error loudly (the matrix must not silently lie)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    nested = tmp_path / "sessions" / "2026" / "01"
    nested.mkdir(parents=True)
    _write(nested / "rollout-abcd.jsonl", [_SESSION_META, {"type": "message"}])

    auto = CliRunner().invoke(main, [str(tmp_path), "-o", str(tmp_path / "a.html")])
    assert auto.exit_code != 0
    assert "sessions directory" in auto.output and "wholesale walker" in auto.output

    explicit = CliRunner().invoke(
        main, ["--provider", "codex", str(tmp_path), "-o", str(tmp_path / "e.html")]
    )
    assert explicit.exit_code != 0
    assert "sessions directory" in explicit.output


def test_non_rollout_file_is_left_to_the_claude_path(tmp_path: Path) -> None:
    """A non-rollout .jsonl is NOT hijacked by auto-detection — it must reach
    the Claude parser unchanged (byte-stability of the default path)."""
    from click.testing import CliRunner

    from claude_code_log.cli import main

    claude = _write(
        tmp_path / "session.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "s1",
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello claude"},
            }
        ],
    )
    result = CliRunner().invoke(main, [str(claude), "-o", str(tmp_path / "c.html")])
    # It reaches the Claude path (no provider hijack, no loud provider error).
    assert "no renderable messages" not in result.output
