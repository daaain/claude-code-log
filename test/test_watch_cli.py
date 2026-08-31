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
    def test_it_converts_once_before_watching(
        self, tmp_path: Path, monkeypatch
    ) -> None:
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

        calls: list[int] = []

        def run_once(engine, stop=None):
            calls.append(1)
            engine.tick()

        monkeypatch.setattr(watch_mod.WatchEngine, "run", run_once)
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

    def test_a_project_dir_with_all_projects_fails_with_a_diagnosis(
        self, tmp_path: Path
    ) -> None:
        """`INPUT_PATH --all-projects` means "this is the archive root" --
        the same as it does for `convert`. Point it at a single project,
        whose JSONL files sit directly inside it rather than one level
        down, and there is nothing to convert.

        That is the user's mistake, but it used to surface as a raw
        traceback out of the up-front conversion, because only the
        *per-tick* failures had a handler. Watching on regardless would be
        worse: every tick would fail the same way, forever.
        """
        projects = tmp_path / "projects"
        proj = projects / "-tmp-demo"
        proj.mkdir(parents=True)
        sid = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        (proj / f"{sid}.jsonl").write_text(_entry("u0", "hello", sid), encoding="utf-8")

        result = CliRunner().invoke(
            main,
            ["watch", str(proj), "--projects-dir", str(projects), "--all-projects"],
        )

        assert result.exit_code == 1
        assert "No project directories with JSONL files found" in result.output
        assert "Traceback" not in result.output

    def test_the_archive_root_with_all_projects_still_converts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The other half of the pair: an explicit path *is* honoured under
        `--all-projects` when it really is a hierarchy, so the guard above
        must not have turned the combination into a blanket rejection."""
        projects = tmp_path / "projects"
        proj = projects / "-tmp-demo"
        proj.mkdir(parents=True)
        sid = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
        (proj / f"{sid}.jsonl").write_text(_entry("u0", "hello", sid), encoding="utf-8")

        import claude_code_log.watch as watch_mod

        monkeypatch.setattr(
            watch_mod.WatchEngine, "run", lambda engine, stop=None: None
        )
        result = CliRunner().invoke(
            main,
            ["watch", str(projects), "--projects-dir", str(projects), "--all-projects"],
        )

        assert result.exit_code == 0, result.output
        assert (proj / f"session-{sid}.html").exists(), result.output


class TestServeWatch:
    """`serve --watch` runs the same engine beside the HTTP server.

    The server never renders: it re-runs the ordinary conversion and lets
    the files on disk stay canonical, so a page served over http and the
    same file opened from file:// can never disagree.
    """

    def _project(self, tmp_path: Path) -> tuple[Path, Path, str]:
        projects = tmp_path / "projects"
        proj = projects / "-tmp-demo"
        proj.mkdir(parents=True)
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (proj / f"{sid}.jsonl").write_text(_entry("u0", "hello", sid), encoding="utf-8")
        return projects, proj, sid

    def _invoke(self, projects: Path, args: list[str], monkeypatch):
        """Run `serve`, returning as soon as the server would block.

        The stub calls `start()` rather than doing nothing:
        `BaseServer.shutdown()` waits on an event that only
        `serve_forever` sets, so a no-op stub makes the command's own
        `server.stop()` hang forever.
        """
        import claude_code_log.server as server_mod

        def start_instead(server):
            server_mod.ArchiveServer.start(server)

        monkeypatch.setattr(server_mod.ArchiveServer, "serve_forever", start_instead)
        return CliRunner().invoke(
            main,
            ["serve", "--projects-dir", str(projects), "--port", "0", "--no-index"]
            + args,
        )

    def test_watch_starts_and_stops_the_engine(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        projects, _proj, _sid = self._project(tmp_path)
        import claude_code_log.watch as watch_mod

        started: list[object] = []
        real_run_in_thread = watch_mod.WatchEngine.run_in_thread

        def spy(engine, stop):
            started.append(engine)
            return real_run_in_thread(engine, stop)

        monkeypatch.setattr(watch_mod.WatchEngine, "run_in_thread", spy)
        result = self._invoke(projects, ["--watch"], monkeypatch)

        assert result.exit_code == 0, result.output
        assert started, "the watch engine was never started"
        assert "watching for changes" in result.output
        # The engine's thread is a daemon and was asked to stop in the
        # finally block; nothing should still be running.
        import threading

        assert not any(
            t.name == "claude-code-log-watch" and t.is_alive()
            for t in threading.enumerate()
        )

    def test_without_the_flag_no_engine_runs(self, tmp_path: Path, monkeypatch) -> None:
        projects, _proj, _sid = self._project(tmp_path)
        import claude_code_log.watch as watch_mod

        started: list[object] = []

        def spy(engine, stop):
            started.append(engine)

        monkeypatch.setattr(watch_mod.WatchEngine, "run_in_thread", spy)
        result = self._invoke(projects, [], monkeypatch)

        assert result.exit_code == 0, result.output
        assert not started
        assert "watching for changes" not in result.output
