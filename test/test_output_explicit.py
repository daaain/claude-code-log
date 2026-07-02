"""Tests for explicit --output correctness (PR 1 of the #220-223 set).

Covers:
- #221: an explicit ``-o`` always regenerates (no stale content kept when
  a different source is written to the same path).
- #222: output format is inferred from the ``-o`` file suffix when ``-f``
  is omitted, and an explicit conflict is an error.
- #223-part1: the ``is_outdated`` version sniff no longer opens a
  non-regular destination (e.g. a FIFO / ``/dev/stdout``), which deadlocked.
"""

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.html.renderer import check_html_version
from claude_code_log.json.renderer import JsonRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer


_requires_mkfifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only (not on Windows)"
)


def _user_entry(text: str, session_id: str = "s") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2025-01-01T10:00:00Z",
        "sessionId": session_id,
        "uuid": f"u-{uuid.uuid4().hex[:8]}",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _write_jsonl(path: Path, text: str) -> Path:
    path.write_text(json.dumps(_user_entry(text)) + "\n", encoding="utf-8")
    return path


class TestExplicitOutputAlwaysRegenerates:
    """#221: same-version file at a user path must not keep stale content."""

    def test_second_source_overwrites_first(self, tmp_path: Path):
        a = _write_jsonl(tmp_path / "a.jsonl", "AAA_alpha_source")
        b = _write_jsonl(tmp_path / "b.jsonl", "BBB_beta_source")
        out = tmp_path / "out.md"
        runner = CliRunner()

        r1 = runner.invoke(main, [str(a), "-f", "markdown", "-o", str(out)])
        assert r1.exit_code == 0, r1.output
        first = out.read_text(encoding="utf-8")
        assert "AAA_alpha_source" in first

        r2 = runner.invoke(main, [str(b), "-f", "markdown", "-o", str(out)])
        assert r2.exit_code == 0, r2.output
        second = out.read_text(encoding="utf-8")
        # Regenerated from B — not the stale A content.
        assert "BBB_beta_source" in second
        assert "AAA_alpha_source" not in second
        # And it did not announce a skip.
        assert "skipping regeneration" not in r2.output

    def test_directory_input_to_file_output_cross_source(self, tmp_path: Path):
        """A *directory* input exported to an explicit `-o` *file* also fixes
        #221: `combined.html` is a file destination, so force_regenerate is
        set and a second, different source produces fresh content rather than
        the stale first export. Complements the file-input case above and
        exercises the directory-mode regeneration branch."""
        dir_a = tmp_path / "proj_a"
        dir_a.mkdir()
        _write_jsonl(dir_a / "a.jsonl", "ALPHA_dir_source")
        dir_b = tmp_path / "proj_b"
        dir_b.mkdir()
        _write_jsonl(dir_b / "b.jsonl", "BETA_dir_source")
        out = tmp_path / "combined.html"
        runner = CliRunner()

        r1 = runner.invoke(main, [str(dir_a), "-o", str(out)])
        assert r1.exit_code == 0, r1.output
        assert "ALPHA_dir_source" in out.read_text(encoding="utf-8")

        r2 = runner.invoke(main, [str(dir_b), "-o", str(out)])
        assert r2.exit_code == 0, r2.output
        after = out.read_text(encoding="utf-8")
        assert "BETA_dir_source" in after
        assert "ALPHA_dir_source" not in after


class TestFormatInferenceFromSuffix:
    """#222: infer -f from the -o suffix; error on explicit conflict."""

    def setup_method(self):
        self.runner = CliRunner()

    def _src(self, tmp_path: Path) -> Path:
        return _write_jsonl(tmp_path / "s.jsonl", "hello_inference")

    def test_md_suffix_infers_markdown(self, tmp_path: Path):
        out = tmp_path / "o.md"
        r = self.runner.invoke(main, [str(self._src(tmp_path)), "-o", str(out)])
        assert r.exit_code == 0, r.output
        assert "<!DOCTYPE html>" not in out.read_text(encoding="utf-8")

    def test_html_suffix_stays_html(self, tmp_path: Path):
        out = tmp_path / "o.html"
        r = self.runner.invoke(main, [str(self._src(tmp_path)), "-o", str(out)])
        assert r.exit_code == 0, r.output
        assert "<!DOCTYPE html>" in out.read_text(encoding="utf-8")

    def test_json_suffix_infers_json(self, tmp_path: Path):
        out = tmp_path / "o.json"
        r = self.runner.invoke(main, [str(self._src(tmp_path)), "-o", str(out)])
        assert r.exit_code == 0, r.output
        json.loads(out.read_text(encoding="utf-8"))  # parses as JSON

    def test_explicit_format_matching_suffix_ok(self, tmp_path: Path):
        out = tmp_path / "o.md"
        # `md` is canonically the same as the `.md` suffix's `markdown`.
        r = self.runner.invoke(
            main, [str(self._src(tmp_path)), "-o", str(out), "-f", "md"]
        )
        assert r.exit_code == 0, r.output

    def test_conflicting_format_and_suffix_errors(self, tmp_path: Path):
        out = tmp_path / "o.md"
        r = self.runner.invoke(
            main, [str(self._src(tmp_path)), "-o", str(out), "-f", "html"]
        )
        assert r.exit_code != 0
        assert "conflicts" in r.output
        # Nothing should have been written.
        assert not out.exists()


class TestIsOutdatedNonRegularFileGuard:
    """#223-part1: version sniff must not open a non-regular destination."""

    def _call_with_timeout(
        self, fn: Callable[..., Any], *args: Any, timeout: float = 3.0
    ) -> Any:
        result: dict[str, Any] = {}

        def run() -> None:
            result["value"] = fn(*args)

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout)
        assert not t.is_alive(), "is_outdated blocked on a non-regular file (hang)"
        return result["value"]

    # FIFO pins the actual deadlock scenario (opening a pipe read-only blocks
    # on readline); os.mkfifo is POSIX-only, so these skip on Windows. The
    # directory-based tests below cover the is_file() guard cross-platform.
    @_requires_mkfifo
    def test_markdown_is_outdated_on_fifo_returns_true(self, tmp_path: Path):
        fifo = tmp_path / "f"
        os.mkfifo(fifo)
        assert self._call_with_timeout(MarkdownRenderer().is_outdated, fifo) is True

    @_requires_mkfifo
    def test_json_is_outdated_on_fifo_returns_true(self, tmp_path: Path):
        fifo = tmp_path / "f"
        os.mkfifo(fifo)
        assert self._call_with_timeout(JsonRenderer().is_outdated, fifo) is True

    @_requires_mkfifo
    def test_check_html_version_on_fifo_returns_none(self, tmp_path: Path):
        fifo = tmp_path / "f"
        os.mkfifo(fifo)
        assert self._call_with_timeout(check_html_version, fifo) is None

    def test_markdown_is_outdated_on_directory_returns_true(self, tmp_path: Path):
        """Cross-platform: a directory is not a regular file → outdated,
        without opening it (the is_file() guard, sans POSIX FIFO)."""
        d = tmp_path / "subdir"
        d.mkdir()
        assert MarkdownRenderer().is_outdated(d) is True

    def test_json_is_outdated_on_directory_returns_true(self, tmp_path: Path):
        d = tmp_path / "subdir"
        d.mkdir()
        assert JsonRenderer().is_outdated(d) is True

    def test_check_html_version_on_directory_returns_none(self, tmp_path: Path):
        d = tmp_path / "subdir"
        d.mkdir()
        assert check_html_version(d) is None

    def test_nonexistent_path_is_outdated(self, tmp_path: Path):
        missing = tmp_path / "nope.md"
        assert MarkdownRenderer().is_outdated(missing) is True


class TestSingleFileStaleness:
    """#221 follow-up: the *default* single-file output (no ``-o``, detail-
    suffixed naming) rides the version-only skip, so a source that grows
    between runs was wrongly kept stale. And a genuine skip printed BOTH the
    converter's "is current, skipping" line and the CLI's "Successfully
    converted" line. These pin the fixes for the exact CLI repro:
    ``claude-code-log <file>.jsonl --detail low`` run, grow source, re-run."""

    def test_grown_source_regenerates(self, tmp_path: Path):
        src = _write_jsonl(tmp_path / "session.jsonl", "FIRST_turn_marker")
        runner = CliRunner()

        r1 = runner.invoke(main, [str(src), "--detail", "low"])
        assert r1.exit_code == 0, r1.output
        out = next(tmp_path.glob("*.low.html"))
        assert "FIRST_turn_marker" in out.read_text(encoding="utf-8")
        out_mtime = out.stat().st_mtime

        # Source progresses (a real append, not a synthetic mtime bump), then
        # forced strictly newer than the output so the check is robust on
        # coarse-granularity filesystems.
        with open(src, "a", encoding="utf-8") as f:
            f.write(json.dumps(_user_entry("SECOND_turn_marker")) + "\n")
        os.utime(src, (out_mtime + 2, out_mtime + 2))

        r2 = runner.invoke(main, [str(src), "--detail", "low"])
        assert r2.exit_code == 0, r2.output
        assert "skipping regeneration" not in r2.output
        second = out.read_text(encoding="utf-8")
        assert "SECOND_turn_marker" in second, "stale HTML served after source grew"
        assert out.stat().st_mtime > out_mtime

    def test_unchanged_source_skips_without_success_line(self, tmp_path: Path):
        src = _write_jsonl(tmp_path / "session.jsonl", "ONLY_turn_marker")
        runner = CliRunner()

        r1 = runner.invoke(main, [str(src), "--detail", "low"])
        assert r1.exit_code == 0, r1.output
        assert "Successfully converted" in r1.output

        # Re-run with the source untouched: skip announced exactly once, and
        # NOT also "Successfully converted" (the misleading double message).
        r2 = runner.invoke(main, [str(src), "--detail", "low"])
        assert r2.exit_code == 0, r2.output
        assert "skipping regeneration" in r2.output
        assert "Successfully converted" not in r2.output

    def test_directory_skip_suppresses_success_line(self, tmp_path: Path):
        """Directory symmetry: a no-op directory re-run early-exits with a
        skip line and must NOT also print 'Successfully combined ...' (the
        same double-message fix applied to the directory branch)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_jsonl(proj / "s.jsonl", "DIR_turn_marker")
        runner = CliRunner()

        r1 = runner.invoke(main, [str(proj)])
        assert r1.exit_code == 0, r1.output
        assert "Successfully combined" in r1.output

        r2 = runner.invoke(main, [str(proj)])
        assert r2.exit_code == 0, r2.output
        assert "skipping regeneration" in r2.output
        assert "Successfully combined" not in r2.output

    def test_combined_no_confirms_session_generation(self, tmp_path: Path):
        """`--combined no` writes only per-session files (no combined). The
        success line is gated on regeneration, and that gate must count
        per-session output too — otherwise the run falls completely silent
        despite writing every session file. Regression guard: the first
        version of the skip-suppression tracked only the combined write."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_jsonl(proj / "aaaaaaaa.jsonl", "SESSION_A_marker")
        runner = CliRunner()

        r1 = runner.invoke(main, [str(proj), "--combined", "no"])
        assert r1.exit_code == 0, r1.output
        # Per-session files were produced...
        assert list(proj.glob("session-*.html")), "no session files written"
        # ...and the run confirms that work rather than printing nothing.
        assert "individual session files" in r1.output, (
            f"--combined no produced no confirmation; output was:\n{r1.output}"
        )
        # ...and it does NOT claim to have "combined" anything (no combined
        # file is written under --combined no); it reports "processed" instead.
        assert "Successfully processed" in r1.output
        assert "Successfully combined" not in r1.output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
