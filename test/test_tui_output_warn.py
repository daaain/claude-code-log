"""PR 3 of the #220-223 set (issue #220): warn that --output / --format
are no-ops under --tui.

The TUI ignores --output/--format (run_session_browser never receives them;
its export actions write to a fixed per-session path). Rather than silently
ignoring, the CLI now warns — mirroring the --expand-paths/--filter-path
ignored-flag warnings. These tests drive the warn via an empty projects
dir so the TUI returns early ("No projects ...") without launching Textual.
"""

from pathlib import Path

from click.testing import CliRunner

from claude_code_log.cli import main


def _empty_projects(tmp_path: Path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


class TestTuiOutputWarn:
    def setup_method(self):
        self.runner = CliRunner()

    def test_warns_on_output_under_tui(self, tmp_path: Path):
        r = self.runner.invoke(
            main,
            ["--tui", "--projects-dir", str(_empty_projects(tmp_path)), "-o", "x.md"],
        )
        assert r.exit_code == 0, r.output
        assert "ignored in --tui mode" in r.stderr

    def test_warns_on_explicit_format_under_tui(self, tmp_path: Path):
        r = self.runner.invoke(
            main,
            [
                "--tui",
                "--projects-dir",
                str(_empty_projects(tmp_path)),
                "-f",
                "markdown",
            ],
        )
        assert r.exit_code == 0, r.output
        assert "ignored in --tui mode" in r.stderr

    def test_no_warn_under_tui_without_output_or_format(self, tmp_path: Path):
        r = self.runner.invoke(
            main, ["--tui", "--projects-dir", str(_empty_projects(tmp_path))]
        )
        assert r.exit_code == 0, r.output
        assert "ignored in --tui mode" not in r.stderr

    def test_warn_goes_to_stderr_not_stdout(self, tmp_path: Path):
        r = self.runner.invoke(
            main,
            ["--tui", "--projects-dir", str(_empty_projects(tmp_path)), "-o", "x.md"],
        )
        assert "ignored in --tui mode" not in r.stdout
        assert "ignored in --tui mode" in r.stderr

    def test_conflicting_output_format_under_tui_warns_not_errors(self, tmp_path: Path):
        """A `-o x.md -f html` conflict (#222) must NOT hard-error under --tui:
        both flags are ignored there, so we warn and let the TUI proceed
        rather than blocking on a conflict the user can't act on (#220)."""
        r = self.runner.invoke(
            main,
            [
                "--tui",
                "--projects-dir",
                str(_empty_projects(tmp_path)),
                "-o",
                "x.md",
                "-f",
                "html",
            ],
        )
        assert r.exit_code == 0, r.output
        assert "ignored in --tui mode" in r.stderr
        assert "conflicts" not in r.output

    def test_no_warn_when_not_tui(self, tmp_path: Path):
        """A normal (non-TUI) `-o` conversion must not emit the TUI warning."""
        src = tmp_path / "s.jsonl"
        src.write_text("", encoding="utf-8")  # empty → no messages, still converts
        out = tmp_path / "out.md"
        r = self.runner.invoke(main, [str(src), "-o", str(out)])
        assert "ignored in --tui mode" not in r.output
