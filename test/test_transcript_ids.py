"""Transcript ids are untrusted: an invalid one never reaches a sink.

A transcript's ``sessionId``/``agentId`` become file names, a ``claude
--resume`` argument, links and Textual markup. Ingestion replaces an invalid
id with a quarantined stand-in (``transcript_ids.py``), and every file sink
checks again. Each test here reproduces a path that, before the fix, read,
wrote or deleted outside the directories the tool works in, or crashed the
conversion. The TUI sinks are covered in ``test_tui_transcript_ids.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.cache import CacheManager
from claude_code_log.converter import convert_jsonl_to, load_transcript
from claude_code_log.factories import create_transcript_entry
from claude_code_log.transcript_ids import (
    is_plain_id,
    is_session_id,
    quarantined_id,
    sanitize_entry_ids,
    session_file_name,
    session_path,
)
from claude_code_log.utils import project_destination

HOSTILE_IDS = [
    "../../victim",
    "x/../../../../tmp/escaped",
    "..\\..\\victim",
    '--settings={"apiKeyHelper":"touch /tmp/pwn"}',
    "-r",
    "[@click=app.notify('CLICKED')]PWN",
    "\x1b[2Jcleared",
    "has space",
    "",
    ".hidden",
    "a" * 200,
]


def _user_entry(session_id: Any, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "user",
        "uuid": "u1",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/x/p",
        "sessionId": session_id,
        "version": "1",
        "timestamp": "2025-01-01T00:00:00Z",
        "message": {"role": "user", "content": "QUARANTINED-CONTENT"},
    }
    entry.update(extra)
    return entry


def _write_jsonl(path: Path, *entries: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


class TestIdRules:
    @pytest.mark.parametrize(
        "value",
        [
            "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
            "a1b2c3d",
            "session-123",
            "test_session.v2",
        ],
    )
    def test_real_ids_pass(self, value: str) -> None:
        assert is_plain_id(value)
        assert is_session_id(value)

    def test_agent_stamped_session_ids_pass(self) -> None:
        # The loader's own `{trunk}#agent-{id}` lines come back from the cache.
        stamped = "29ccd257-68b1-427f-ae5f-6524b7cb6f20#agent-a1b2#agent-c3"
        assert is_session_id(stamped)
        assert not is_plain_id(stamped)

    @pytest.mark.parametrize("value", HOSTILE_IDS)
    def test_hostile_ids_fail(self, value: str) -> None:
        assert not is_plain_id(value)
        assert not is_session_id(value)

    @pytest.mark.parametrize("value", [None, 42, ["a"], {"a": 1}])
    def test_non_strings_fail(self, value: object) -> None:
        assert not is_plain_id(value)
        assert not is_session_id(value)

    def test_stand_in_is_stable_valid_and_distinct(self) -> None:
        a = quarantined_id("../../a")
        assert a == quarantined_id("../../a")
        assert a != quarantined_id("../../b")
        assert is_plain_id(a) and a.startswith("quarantined-")


class TestSanitizeEntry:
    @pytest.mark.parametrize("value", HOSTILE_IDS)
    def test_session_id_is_replaced_and_idempotent(self, value: str) -> None:
        entry = _user_entry(value)
        assert sanitize_entry_ids(entry) == ["sessionId"]
        assert entry["sessionId"] == quarantined_id(value)
        assert sanitize_entry_ids(entry) == []

    def test_every_agent_id_field_is_replaced(self) -> None:
        entry = _user_entry(
            "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
            agentId="../x",
            spawnedAgentId="../y",
            toolUseResult={"agentId": "../z", "status": "completed"},
        )
        assert sanitize_entry_ids(entry) == [
            "agentId",
            "spawnedAgentId",
            "toolUseResult.agentId",
        ]
        assert is_plain_id(entry["agentId"])
        assert is_plain_id(entry["spawnedAgentId"])
        assert is_plain_id(entry["toolUseResult"]["agentId"])
        assert entry["sessionId"] == "29ccd257-68b1-427f-ae5f-6524b7cb6f20"

    def test_factory_sanitizes_cache_rows_too(self) -> None:
        # Cache rows are rebuilt through create_transcript_entry, so a row
        # written before the fix comes back with the stand-in.
        entry = create_transcript_entry(_user_entry("../../victim"))
        assert entry.sessionId == quarantined_id("../../victim")


class TestSinkHelpers:
    @pytest.mark.parametrize("value", HOSTILE_IDS)
    def test_session_path_refuses(self, tmp_path: Path, value: str) -> None:
        assert session_path(tmp_path, value) is None

    def test_session_path_accepts(self, tmp_path: Path) -> None:
        assert session_path(tmp_path, "abc") == tmp_path / "abc.jsonl"

    @pytest.mark.parametrize("value", HOSTILE_IDS)
    def test_session_file_name_refuses(self, value: str) -> None:
        with pytest.raises(ValueError):
            session_file_name(value, ".html")


class TestLoading:
    def test_invalid_session_id_is_quarantined_with_a_safe_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = "\x1b]52;c;cHduZWQ=\x07../../victim"
        path = _write_jsonl(tmp_path / "p" / "a.jsonl", _user_entry(bad))
        entries = load_transcript(path)
        assert [e.sessionId for e in entries] == [quarantined_id(bad)]
        err = capsys.readouterr().err
        assert "invalid sessionId" in err and "a.jsonl" in err
        assert "\x1b" not in err and "victim" not in err

    def test_agent_file_outside_the_session_dir_is_not_read(
        self, tmp_path: Path
    ) -> None:
        # `parent/agent-x/../secret.jsonl` resolves wherever `agent-x` exists
        # (and anywhere on Windows, which collapses `..` before looking).
        project = tmp_path / "p"
        (project / "agent-x").mkdir(parents=True)
        _write_jsonl(
            project / "secret.jsonl",
            _user_entry(
                "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
                uuid="s1",
                isSidechain=True,
                message={"role": "user", "content": "SECRET-CONTENT"},
            ),
        )
        trunk = _write_jsonl(
            project / "29ccd257-68b1-427f-ae5f-6524b7cb6f20.jsonl",
            _user_entry(
                "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
                toolUseResult={"agentId": "x/../secret"},
            ),
        )
        entries = load_transcript(trunk, silent=True)
        dumped = json.dumps([e.model_dump() for e in entries])
        assert "SECRET-CONTENT" not in dumped


def test_quarantine_warning_shows_in_an_all_projects_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from claude_code_log.converter import process_projects_hierarchy

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    projects = tmp_path / "projects"
    _write_jsonl(projects / "-home-x-p" / "a.jsonl", _user_entry("../../victim"))
    process_projects_hierarchy(projects, silent=True)
    err = capfd.readouterr().err
    assert "invalid sessionId" in err and "a.jsonl" in err
    assert "victim" not in err


class TestConversion:
    @pytest.mark.parametrize("fmt", ["html", "md"])
    def test_hostile_session_renders_quarantined_and_stays_inside(
        self, tmp_path: Path, fmt: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Before: a `/` in the id crashed the whole conversion
        # (FileNotFoundError on `session-x/...`); on Windows `..` was
        # collapsed and the page written outside the output directory.
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
        project = tmp_path / "projects" / "p"
        _write_jsonl(project / "a.jsonl", _user_entry("x/../../../../escaped"))
        before = {p for p in tmp_path.rglob("*")}
        convert_jsonl_to(fmt, project, silent=True)
        created = {p for p in tmp_path.rglob("*")} - before
        assert created, "nothing rendered"
        outside = [
            p for p in created if project not in p.parents and p.name[:5] != "cache"
        ]
        assert not outside, outside
        quarantined = quarantined_id("x/../../../../escaped")
        assert (project / f"session-{quarantined}.{fmt}").is_file()
        assert "QUARANTINED-CONTENT" in (
            project / f"session-{quarantined}.{fmt}"
        ).read_text(encoding="utf-8")


def _inside(path: Path, root: Path) -> bool:
    """Lexically inside after normalising: `Path.parents` keeps `..` parts."""
    full, base = os.path.normpath(path), os.path.normpath(root)
    return full == base or full.startswith(base + os.sep)


class TestExpandPaths:
    @pytest.mark.parametrize(
        "cwd",
        [
            "/../../../../srv/ESCAPED",
            "/home/x/../../../../../srv/ESCAPED",
            "/home/..\\..\\..\\srv\\ESCAPED",
            "C:\\..\\..\\..\\ESCAPED",
        ],
    )
    def test_destination_stays_inside_output(self, tmp_path: Path, cwd: str) -> None:
        out = tmp_path / "out" / "deep"
        dest = project_destination(
            tmp_path / "projects" / "-whatever",
            output_dir=out,
            expand_paths=True,
            filter_path=None,
            cached_working_directories=[cwd],
        )
        assert dest is not None
        assert _inside(dest, out), dest

    def test_filter_prefix_destination_stays_inside_output(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        dest = project_destination(
            tmp_path / "projects" / "-whatever",
            output_dir=out,
            expand_paths=True,
            filter_path="/home/x",
            cached_working_directories=["/home/x/../../../../srv/ESCAPED"],
        )
        assert dest is None or _inside(dest, out), dest

    def test_ordinary_path_unchanged(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        dest = project_destination(
            tmp_path / "projects" / "-home-joe-proj",
            output_dir=out,
            expand_paths=True,
            filter_path=None,
            cached_working_directories=["/home/joe/proj"],
        )
        assert dest == out / "home" / "joe" / "proj"


def test_caches_from_1_7_0_are_rebuilt(tmp_path: Path) -> None:
    # Their derived rows (sessions, page records, search index) hold the
    # original ids, which no ingestion check will see again.
    manager = CacheManager(tmp_path, "1.7.1")
    assert manager._is_cache_version_compatible("1.7.0") is False
    assert manager._is_cache_version_compatible("1.7.1") is True


def test_codex_thread_id_is_quarantined(tmp_path: Path) -> None:
    from claude_code_log.providers.codex import CodexProvider

    rollout = tmp_path / "rollout-2026-01-01T00-00-00-x.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "../../escaped",
                    "parent_thread_id": "../../parent",
                    "cwd": "/proj",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    identity = CodexProvider()._read_identity(rollout)
    assert identity.thread_id == quarantined_id("../../escaped")
    assert identity.parent_thread_id is None


def test_workflow_agent_outside_run_dir_is_not_read(tmp_path: Path) -> None:
    from claude_code_log.workflow import parse_workflow_run

    run_dir = tmp_path / "s" / "subagents" / "workflows" / "wf_1"
    (run_dir / "agent-x").mkdir(parents=True)
    _write_jsonl(
        run_dir / "secret.jsonl",
        _user_entry("s", message={"role": "user", "content": "SECRET-CONTENT"}),
    )
    _write_jsonl(
        run_dir / "journal.jsonl",
        {"type": "started", "key": "k", "agentId": "x/../secret"},
    )
    run = parse_workflow_run(run_dir, None)
    agents = run.agents if run is not None else []
    assert all(a.agent_id != "x/../secret" for a in agents)
    assert "SECRET-CONTENT" not in repr(agents)


def test_cached_page_paths_outside_the_output_are_never_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The converter deletes what invalidate_all_pages returns when the page
    # size changes; a cache built elsewhere must not name other files.
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / "projects" / "p"
    project.mkdir(parents=True)
    manager = CacheManager(project, "1.7.1")
    for n, path in enumerate(
        [
            "combined_transcripts.html",
            "combined_transcripts_2.html",
            "../../outside/victim.txt",
            "/etc/victim",
            "combined_transcripts/../../x.html",
            "..\\..\\victim.html",
        ],
        start=1,
    ):
        manager.update_page_cache(n, path, 10, ["s"], 1, None, None, 0, 0, 0, 0)
    assert sorted(manager.invalidate_all_pages()) == [
        "combined_transcripts.html",
        "combined_transcripts_2.html",
    ]


def test_agent_lookup_skips_an_invalid_id_that_bypassed_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ids can reach the lookup without passing ingestion (a cache built
    # elsewhere, the subagent sidecar), so the sink checks on its own.
    import claude_code_log.converter as converter
    import claude_code_log.factories.transcript_factory as factory

    monkeypatch.setattr(converter, "sanitize_entry_ids", lambda _entry: [])
    monkeypatch.setattr(factory, "sanitize_entry_ids", lambda _entry: [])
    project = tmp_path / "p"
    (project / "agent-x").mkdir(parents=True)
    _write_jsonl(
        project / "secret.jsonl",
        _user_entry(
            "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
            uuid="s1",
            isSidechain=True,
            message={"role": "user", "content": "SECRET-CONTENT"},
        ),
    )
    trunk = _write_jsonl(
        project / "29ccd257-68b1-427f-ae5f-6524b7cb6f20.jsonl",
        _user_entry(
            "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
            toolUseResult={"agentId": "x/../secret"},
        ),
    )
    entries = load_transcript(trunk, silent=True)
    assert "SECRET-CONTENT" not in json.dumps([e.model_dump() for e in entries])


def test_join_inside_splits_both_separators(tmp_path: Path) -> None:
    from claude_code_log.utils import _join_inside  # pyright: ignore[reportPrivateUsage]

    out = tmp_path / "out"
    assert _join_inside(out, ["..\\..\\srv", "x"]) == out / "srv" / "x"
    assert _join_inside(out, ["a/../../b"]) == out / "a" / "b"
    assert _join_inside(out, ["", ".", ".."]) == out


def test_codex_fallback_id_is_quarantined(tmp_path: Path) -> None:
    # No session_meta: the id comes from the file name instead.
    from claude_code_log.providers.codex import CodexProvider

    rollout = tmp_path / "legacy copy (2).jsonl"
    rollout.write_text("", encoding="utf-8")
    identity = CodexProvider()._read_identity(rollout)
    assert is_plain_id(identity.thread_id)
    assert identity.thread_id.startswith("quarantined-")


def test_dag_warnings_escape_terminal_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # parentUuid and uuid come from the transcript; the orphan-node warning
    # echoes them to stderr.
    evil = "p\x1b]0;PWNED\x07\x1b[2J"
    path = _write_jsonl(tmp_path / "p" / "a.jsonl", _user_entry("s1", parentUuid=evil))
    from claude_code_log.dag import build_dag_from_entries

    with caplog.at_level("WARNING", logger="claude_code_log.dag"):
        build_dag_from_entries(load_transcript(path, silent=True))
    messages = [r.getMessage() for r in caplog.records if r.name.endswith("dag")]
    assert any("Orphan node" in m for m in messages), messages
    assert not any("\x1b" in m or "\x07" in m for m in messages), messages
