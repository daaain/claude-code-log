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
import os
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


# --------------------------------------------------------------------------
# Migration: stale FULL-era no-suffix outputs + --detail high ≡ default
# --------------------------------------------------------------------------
class TestDefaultChangeMigration:
    def test_full_era_no_suffix_output_regenerates_at_new_default(
        self, tmp_path: Path
    ) -> None:
        """Migration guard (#159): a pre-#159 no-suffix output (rendered as
        FULL, embedding an OLD library version) must NOT be served stale
        after the default flips to HIGH — the version gate
        (renderer.is_outdated) forces regeneration even when the source is
        untouched, so the version bump shipping this change re-renders every
        no-suffix file at the new default.

        Isolates the version gate: the output is left NEWER than the source
        (source_is_newer is False), so only is_outdated can trigger regen.
        """
        src = _write(
            tmp_path,
            _entry("user", "u1", None, "the user prompt"),
            _entry("system", "sys1", "u1", "SYSTEM_NOISE_STALE"),
            _entry("assistant", "a1", "sys1", "reply"),
        )
        # First run establishes the real no-suffix output path.
        r1 = CliRunner().invoke(main, [str(src)])
        assert r1.exit_code == 0, r1.output
        outputs = [p for p in tmp_path.glob("*.html")]
        assert len(outputs) == 1, outputs
        out = outputs[0]
        assert not out.name.endswith((".hook.html", ".agent.html")), out.name

        # Simulate a FULL-era artifact at that path: OLD embedded version +
        # content FULL would have shown (the system noise) + a unique marker.
        out.write_text(
            "<!-- Generated by claude-code-log v0.0.1 -->\n"
            "<html><body>STALE_FULL_ERA_BODY SYSTEM_NOISE_STALE</body></html>",
            encoding="utf-8",
        )
        # Keep the output strictly newer than the source so mtime-freshness
        # would (wrongly) call it current — only is_outdated should fire.
        src_mtime = src.stat().st_mtime
        os.utime(out, (src_mtime + 5, src_mtime + 5))

        r2 = CliRunner().invoke(main, [str(src)])
        assert r2.exit_code == 0, r2.output
        regenerated = out.read_text(encoding="utf-8")
        # Regenerated, not served stale: the stale body is gone …
        assert "STALE_FULL_ERA_BODY" not in regenerated
        # … it's the NEW default (HIGH), which filters system noise …
        assert "SYSTEM_NOISE_STALE" not in regenerated
        # … and the real conversation is present with the current version.
        assert "the user prompt" in regenerated
        assert "<!-- Generated by claude-code-log v0.0.1 -->" not in regenerated

    def test_detail_high_is_byte_identical_to_default_tool(
        self, tmp_path: Path
    ) -> None:
        """Retiring the `.high` suffix must not fork the two paths: legacy
        `--detail high` and the default (`--depth tool`) resolve to the SAME
        level, the SAME (empty) suffix, and byte-identical output."""
        entries = (
            _entry("user", "u1", None, "hello"),
            _entry("assistant", "a1", "u1", "world"),
        )
        dir_default = tmp_path / "default"
        dir_high = tmp_path / "high"
        dir_default.mkdir()
        dir_high.mkdir()
        src_default = _write(dir_default, *entries)
        src_high = _write(dir_high, *entries)

        r1 = CliRunner().invoke(main, [str(src_default)])
        assert r1.exit_code == 0, r1.output
        r2 = CliRunner().invoke(main, [str(src_high), "--detail", "high"])
        assert r2.exit_code == 0, r2.output

        out_default = next(dir_default.glob("*.html"))
        out_high = next(dir_high.glob("*.html"))
        # Same (no) suffix.
        assert out_default.name == out_high.name
        # Byte-identical content (the only difference is the deprecation
        # warning on stderr, not in the file).
        assert out_default.read_bytes() == out_high.read_bytes()

    def test_full_era_combined_regenerates_with_valid_cache(
        self, tmp_path: Path
    ) -> None:
        """Cached directory path (#159 migration): a FULL-era
        combined_transcripts.html (OLD embedded version) is regenerated even
        with a populated cache and an untouched source, because the cached
        path's should_regenerate ORs in is_outdated (and a release ALSO
        invalidates the whole cache via cache_version). Pins the cache-row
        interaction main flagged as untested.
        """
        _write(
            tmp_path,
            _entry("user", "u1", None, "the user prompt"),
            _entry("system", "sys1", "u1", "SYSTEM_NOISE_STALE"),
            _entry("assistant", "a1", "sys1", "reply"),
        )
        # First run: directory mode builds the cache + combined output.
        r1 = CliRunner().invoke(main, [str(tmp_path)])
        assert r1.exit_code == 0, r1.output
        combined = tmp_path / "combined_transcripts.html"
        assert combined.exists()

        # Simulate a FULL-era combined file: old version + stale body/noise,
        # left newer than the source so mtime-freshness alone would skip it.
        combined.write_text(
            "<!-- Generated by claude-code-log v0.0.1 -->\n"
            "<html><body>STALE_COMBINED_BODY SYSTEM_NOISE_STALE</body></html>",
            encoding="utf-8",
        )
        src_mtime = max(f.stat().st_mtime for f in tmp_path.glob("*.jsonl"))
        os.utime(combined, (src_mtime + 5, src_mtime + 5))

        r2 = CliRunner().invoke(main, [str(tmp_path)])
        assert r2.exit_code == 0, r2.output
        txt = combined.read_text(encoding="utf-8")
        assert "STALE_COMBINED_BODY" not in txt  # regenerated, not served stale
        assert "SYSTEM_NOISE_STALE" not in txt  # new default (HIGH) filters it
        assert "the user prompt" in txt
