#!/usr/bin/env python3
"""Tests for the ``--depth`` option and the new default level (#159).

``--depth session|user|assistant|agent|tool|hook`` (default ``tool``)
replaces the deprecated ``--detail full|high|low|minimal|user-only`` with
message-hierarchy names. Coverage:

- the depth↔DetailLevel bijection and the new DEFAULT_DETAIL_LEVEL;
- CLI resolution: default is tool/HIGH, --depth maps, --detail still works
  (with a deprecation warning), the two are mutually exclusive;
- the filename suffix is the --depth name of the level (default = no suffix);
- the new ``session`` level renders session structure only (headers), with
  user + assistant + everything-else dropped.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.models import (
    DEFAULT_DETAIL_LEVEL,
    DEPTH_TO_DETAIL,
    DETAIL_TO_DEPTH,
    DetailLevel,
)
from claude_code_log.utils import variant_suffix


# --------------------------------------------------------------------------
# Mapping + default
# --------------------------------------------------------------------------
class TestDepthMapping:
    def test_default_level_is_high(self) -> None:
        # #159: the default output level is HIGH == --depth tool.
        assert DEFAULT_DETAIL_LEVEL is DetailLevel.HIGH

    def test_depth_maps_every_level_bijectively(self) -> None:
        # All six --depth names present and mapping onto distinct DetailLevels.
        assert set(DEPTH_TO_DETAIL) == {
            "session",
            "user",
            "assistant",
            "agent",
            "tool",
            "hook",
        }
        assert set(DEPTH_TO_DETAIL.values()) == set(DetailLevel)
        # Round-trips: DEPTH_TO_DETAIL and DETAIL_TO_DEPTH are inverses.
        for name, level in DEPTH_TO_DETAIL.items():
            assert DETAIL_TO_DEPTH[level] == name

    def test_specific_mappings(self) -> None:
        assert DEPTH_TO_DETAIL["tool"] is DetailLevel.HIGH
        assert DEPTH_TO_DETAIL["hook"] is DetailLevel.FULL
        assert DEPTH_TO_DETAIL["agent"] is DetailLevel.LOW
        assert DEPTH_TO_DETAIL["assistant"] is DetailLevel.MINIMAL
        assert DEPTH_TO_DETAIL["user"] is DetailLevel.USER_ONLY
        assert DEPTH_TO_DETAIL["session"] is DetailLevel.SESSION

    def test_session_is_least_verbose(self) -> None:
        # SESSION drops everything USER_ONLY drops AND more (it is below it).
        assert DetailLevel.SESSION.includes(DetailLevel.SESSION)
        assert not DetailLevel.SESSION.includes(DetailLevel.USER_ONLY)
        # FULL still includes everything, including the new SESSION.
        assert DetailLevel.FULL.includes(DetailLevel.SESSION)


# --------------------------------------------------------------------------
# Suffix: default level is suffix-less; others use the --depth name
# --------------------------------------------------------------------------
class TestDepthSuffix:
    def test_default_level_has_no_suffix(self) -> None:
        assert variant_suffix(DetailLevel.HIGH) == ""

    def test_non_default_levels_use_depth_names(self) -> None:
        assert variant_suffix(DetailLevel.FULL) == ".hook"
        assert variant_suffix(DetailLevel.LOW) == ".agent"
        assert variant_suffix(DetailLevel.MINIMAL) == ".assistant"
        assert variant_suffix(DetailLevel.USER_ONLY) == ".user"
        assert variant_suffix(DetailLevel.SESSION) == ".session"

    def test_deprecated_detail_names_produce_depth_suffix(self) -> None:
        # A level selected via legacy --detail gets the SAME (depth) suffix as
        # via --depth — one canonical name per level.
        assert variant_suffix("low") == variant_suffix(DetailLevel.LOW) == ".agent"
        assert variant_suffix("full") == ".hook"


# --------------------------------------------------------------------------
# Fixtures + helpers for CLI integration
# --------------------------------------------------------------------------
def _entry(kind: str, uuid: str, parent, text: str, level: str = "info") -> dict:
    base = {
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.0",
        "timestamp": "2026-01-01T10:00:00.000Z",
    }
    if kind == "user":
        return {
            **base,
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
    if kind == "assistant":
        return {
            **base,
            "type": "assistant",
            "message": {
                "id": "m" + uuid,
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
    if kind == "system":
        return {**base, "type": "system", "level": level, "content": text}
    raise ValueError(kind)


def _write(tmp: Path, *entries: dict) -> Path:
    p = tmp / "session.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# CLI resolution
# --------------------------------------------------------------------------
class TestDepthCli:
    def _run(self, args):
        return CliRunner().invoke(main, args)

    def test_default_is_tool_no_suffix_and_filters_system(self, tmp_path: Path) -> None:
        src = _write(
            tmp_path,
            _entry("user", "u1", None, "the user prompt"),
            _entry("system", "sys1", "u1", "SYSTEM_NOISE_XYZ"),
            _entry("assistant", "a1", "sys1", "the assistant reply"),
        )
        out = tmp_path / "o.html"
        r = self._run([str(src), "-o", str(out)])
        assert r.exit_code == 0, r.output
        html = out.read_text(encoding="utf-8")
        # Default == tool/HIGH: system noise filtered, user + assistant kept.
        assert "SYSTEM_NOISE_XYZ" not in html
        assert "the user prompt" in html
        assert "the assistant reply" in html

    def test_depth_hook_shows_system(self, tmp_path: Path) -> None:
        src = _write(
            tmp_path,
            _entry("user", "u1", None, "prompt"),
            _entry("system", "sys1", "u1", "SYSTEM_NOISE_XYZ"),
            _entry("assistant", "a1", "sys1", "reply"),
        )
        out = tmp_path / "o.hook.html"
        r = self._run([str(src), "--depth", "hook", "-o", str(out)])
        assert r.exit_code == 0, r.output
        assert "SYSTEM_NOISE_XYZ" in out.read_text(encoding="utf-8")

    def test_depth_and_detail_mutually_exclusive(self, tmp_path: Path) -> None:
        src = _write(tmp_path, _entry("user", "u1", None, "hi"))
        r = self._run([str(src), "--depth", "tool", "--detail", "high"])
        assert r.exit_code != 0
        assert "mutually exclusive" in r.output

    def test_detail_emits_deprecation_warning(self, tmp_path: Path) -> None:
        src = _write(tmp_path, _entry("user", "u1", None, "hi"))
        r = self._run([str(src), "--detail", "low"])
        assert r.exit_code == 0, r.output
        assert "deprecated" in r.output
        # Its output uses the depth-named suffix (.agent), not .low.
        assert list(tmp_path.glob("*.agent.html"))
        assert not list(tmp_path.glob("*.low.html"))

    def test_depth_suffix_on_filename(self, tmp_path: Path) -> None:
        src = _write(tmp_path, _entry("user", "u1", None, "hi"))
        r = self._run([str(src), "--depth", "agent"])
        assert r.exit_code == 0, r.output
        assert list(tmp_path.glob("*.agent.html"))

    def test_invalid_depth_rejected(self, tmp_path: Path) -> None:
        src = _write(tmp_path, _entry("user", "u1", None, "hi"))
        # 'full' is a --detail name, not a valid --depth value.
        r = self._run([str(src), "--depth", "full"])
        assert r.exit_code != 0


# --------------------------------------------------------------------------
# The new SESSION level
# --------------------------------------------------------------------------
class TestSessionDepth:
    def test_session_drops_user_and_assistant(self, tmp_path: Path) -> None:
        src = _write(
            tmp_path,
            _entry("user", "u1", None, "USER_PROMPT_MARKER"),
            _entry("assistant", "a1", "u1", "ASSISTANT_REPLY_MARKER"),
        )
        out = tmp_path / "o.session.html"
        r = CliRunner().invoke(main, [str(src), "--depth", "session", "-o", str(out)])
        assert r.exit_code == 0, r.output
        html = out.read_text(encoding="utf-8")
        # 'session structure only' — even user prompts are dropped (this is the
        # level below user-only). Mutation-check: reverting the SESSION branch
        # in _ghost_template_by_detail makes these markers reappear.
        assert "USER_PROMPT_MARKER" not in html
        assert "ASSISTANT_REPLY_MARKER" not in html

    def test_user_depth_keeps_user_drops_assistant(self, tmp_path: Path) -> None:
        # Contrast: --depth user (== USER_ONLY) keeps user prompts but drops
        # assistant — confirms session is strictly below user.
        src = _write(
            tmp_path,
            _entry("user", "u1", None, "USER_PROMPT_MARKER"),
            _entry("assistant", "a1", "u1", "ASSISTANT_REPLY_MARKER"),
        )
        out = tmp_path / "o.user.html"
        r = CliRunner().invoke(main, [str(src), "--depth", "user", "-o", str(out)])
        assert r.exit_code == 0, r.output
        html = out.read_text(encoding="utf-8")
        assert "USER_PROMPT_MARKER" in html
        assert "ASSISTANT_REPLY_MARKER" not in html
