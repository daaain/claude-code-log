"""Tests for the `watch` subcommand's own logic.

The loop is covered by `test_watch_engine.py`; what's specific here is
which directory a bare `claude-code-log watch` decides to watch, and that
the wiring actually converts.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from claude_code_log.cli import _resolve_watch_root, main


def _entry(uuid: str, text: str, session_id: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-08-30T18:00:00Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp/live/demo",
                "sessionId": session_id,
                "version": "1.0.0",
                "uuid": uuid,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


class TestResolveWatchRoot:
    def test_an_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "somewhere"
        explicit.mkdir()
        assert _resolve_watch_root(explicit, tmp_path / "projects", False) == explicit

    def test_all_projects_selects_the_hierarchy(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()
        assert _resolve_watch_root(None, projects, True) == projects

    def test_the_cwd_project_is_the_default(self, tmp_path: Path, monkeypatch) -> None:
        """Running this beside a live session should just work."""
        work = tmp_path / "myproject"
        work.mkdir()
        projects = tmp_path / "projects"
        encoded = projects / str(work).replace("/", "-")
        encoded.mkdir(parents=True)
        monkeypatch.chdir(work)

        assert _resolve_watch_root(None, projects, False) == encoded

    def test_an_unknown_cwd_reports_rather_than_guessing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        work = tmp_path / "not-a-claude-project"
        work.mkdir()
        projects = tmp_path / "projects"
        projects.mkdir()
        monkeypatch.chdir(work)

        assert _resolve_watch_root(None, projects, False) is None
        err = capsys.readouterr().err
        assert "No transcripts found" in err
        assert "--all-projects" in err, "the error should say what to do instead"


class TestWatchCommand:
    def test_it_converts_once_before_watching(self, tmp_path: Path) -> None:
        """A watch that only reacts leaves the output stale until the next
        message; the up-front conversion is what makes it usable at once."""
        projects = tmp_path / "projects"
        proj = projects / "-tmp-demo"
        proj.mkdir(parents=True)
        sid = "11111111-2222-3333-4444-555555555555"
        (proj / f"{sid}.jsonl").write_text(_entry("u0", "hello", sid), encoding="utf-8")
        out = tmp_path / "vault"

        # max_latency=0 with an immediate stop: run() ticks once, sees no
        # change (the up-front conversion primed after writing), and exits.
        import claude_code_log.watch as watch_mod

        real_run = watch_mod.WatchEngine.run
        calls: list[int] = []

        def run_once(self, stop=None):  # noqa: ANN001
            calls.append(1)
            self.tick()

        watch_mod.WatchEngine.run = run_once
        try:
            result = CliRunner().invoke(
                main,
                [
                    "watch",
                    str(proj),
                    "--projects-dir",
                    str(projects),
                    "-f",
                    "md",
                    "-o",
                    str(out),
                ],
            )
        finally:
            watch_mod.WatchEngine.run = real_run

        assert result.exit_code == 0, result.output
        assert calls, "the engine loop never ran"
        produced = list(out.rglob(f"session-{sid}.md"))
        assert produced, f"no session file was written: {result.output}"
        assert "hello" in produced[0].read_text(encoding="utf-8")
        assert "Watching" in result.output

    def test_an_unresolvable_target_exits_nonzero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        work = tmp_path / "elsewhere"
        work.mkdir()
        projects = tmp_path / "projects"
        projects.mkdir()
        monkeypatch.chdir(work)

        result = CliRunner().invoke(main, ["watch", "--projects-dir", str(projects)])
        assert result.exit_code == 1
