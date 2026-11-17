"""Projects worked on only from temp dirs, and projects with nothing to open.

Test suites, scratchpad agents and scripted ``claude -p`` runs each leave a
project behind, named after a temp dir. The index folds those into one
"Temporary directories" group at the end (``--show-temp-projects`` lists them
with the rest), and leaves out projects whose rows would lead nowhere: no
message and no session. The recent page's side is in ``test_recent.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.renderer import has_nothing_to_open, prepare_projects_index
from claude_code_log.utils import (
    SHOW_TEMP_PROJECTS_ENV_VAR,
    is_temp_dir,
    is_temp_project,
    is_temp_session,
)

PYTEST_CWD = "/private/var/folders/5l/m76/T/pytest-of-u/pytest-1478/popen-gw6/repo"


@pytest.mark.parametrize(
    "path",
    [
        "/tmp",
        "/tmp/",
        "/tmp/claude/501/scratchpad/agents/clone",
        "/private/tmp/claude/501",
        "/var/tmp/x",
        "/private/var/tmp/x",
        "/var/folders/5l/m76/T/x",
        PYTEST_CWD,
        r"C:\Users\u\AppData\Local\Temp\pytest-of-u\repo",
        r"C:\Users\u\AppData\Local\Temp",
    ],
)
def test_temp_dirs(path: str) -> None:
    assert is_temp_dir(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/tmpfiles/project",  # a sibling of /tmp, not under it
        "/Users/u/tmp/notes",  # anchored: tmp/ elsewhere is a real project
        "/Users/u/workspace/claude-code-log",
        "/home/u/var/folders/x",
        r"C:\Users\u\code\Temperature",
        r"C:\Users\u\AppData\Local\Templates",
    ],
)
def test_real_dirs(path: str) -> None:
    assert not is_temp_dir(path)


def test_a_project_is_temp_when_every_working_dir_is() -> None:
    name = "-private-var-folders-5l-repo"
    assert is_temp_project(name, ["/tmp/a", "/private/var/folders/5l/repo"])
    # One real working dir is enough to keep it with the rest.
    assert not is_temp_project(name, ["/tmp/a", "/Users/u/code/repo"])


def test_without_working_dirs_the_folder_name_decides() -> None:
    # A transcript holding no message records no cwd: the folder Claude
    # Code named after it is all there is.
    assert is_temp_project("-private-var-folders-5l-m76-T-pytest-of-u-repo", [])
    assert is_temp_project("-private-tmp-claude-501--Users-u-scratchpad", [])
    assert is_temp_project("-tmp-x", None)
    assert is_temp_project("-tmp", None)
    assert is_temp_project("C--Users-u-AppData-Local-Temp-x", [])
    assert is_temp_project("C--Users-u-AppData-Local-Temp", [])
    # Bounded: a sibling of Temp is a real folder.
    assert not is_temp_project("C--Users-u-AppData-Local-Templates", [])
    assert not is_temp_project("C--Users-u-AppData-Local-Templates-x", [])
    assert not is_temp_project("-Users-u-tmp-notes", [])
    assert not is_temp_project("-tmpfiles-x", [])
    assert not is_temp_project("-Users-u-code-repo", [])


def test_a_session_is_temp_by_its_cwd_else_its_project_folder() -> None:
    assert is_temp_session(PYTEST_CWD, "/home/u/.claude/projects/-Users-u-x")
    assert not is_temp_session("/Users/u/x", "/h/.claude/projects/-tmp-x")
    assert is_temp_session(None, "/home/u/.claude/projects/-tmp-x")
    # Only the folder's own name counts, not where the archive lives.
    assert not is_temp_session(None, "/tmp/pytest-of-u/projects/-Users-u-x")
    assert not is_temp_session(None, None)
    assert not is_temp_session(
        None, "C:/a/.claude/projects/C--Users-u-AppData-Local-Templates"
    )


# -- the index ----------------------------------------------------------------


def _summary(
    name: str,
    working_dirs: list[str],
    *,
    messages: int = 3,
    sessions: bool = True,
    modified: float = 0.0,
) -> dict[str, Any]:
    return {
        "name": name,
        "path": Path("/archive") / name,
        "html_file": f"{name}/combined_transcripts.html",
        "jsonl_count": 1,
        "message_count": messages,
        "last_modified": modified,
        "working_directories": working_dirs,
        "sessions": [
            {
                "id": f"{name}-s1",
                "summary": f"{name} work",
                "timestamp_range": "",
                "message_count": messages,
                "first_user_message": "Go.",
                "file": f"{name}/session-{name}-s1.html",
            }
        ]
        if sessions
        else [],
    }


def _archive() -> list[dict[str, Any]]:
    return [
        _summary("-Users-u-code-alpha", ["/Users/u/code/alpha"], modified=1.0),
        # Newest of all, yet listed after the real ones.
        _summary(
            "-private-tmp-agents-clone", ["/private/tmp/agents/clone"], modified=9.0
        ),
        _summary("-Users-u-code-beta", ["/Users/u/code/beta"], modified=2.0),
        # A pytest run's project: one file, no message, nothing to open.
        _summary(
            "-private-var-folders-5l-T-pytest-of-u-repo",
            [],
            messages=0,
            sessions=False,
        ),
    ]


def test_projects_with_nothing_to_open_are_left_out() -> None:
    assert has_nothing_to_open(_summary("-x", [], messages=0, sessions=False))
    # Messages but no listable session: the combined page still opens.
    assert not has_nothing_to_open(_summary("-x", [], messages=4, sessions=False))
    # No message counted but a session listed: the session page opens.
    assert not has_nothing_to_open(_summary("-x", [], messages=0))

    projects, summary = prepare_projects_index(_archive())
    assert [p.name for p in projects] == [
        "-private-tmp-agents-clone",
        "-Users-u-code-beta",
        "-Users-u-code-alpha",
    ]
    # Out of the totals too.
    assert summary.total_projects == 3
    assert summary.total_jsonl == 3


def test_temp_projects_are_flagged_unless_shown() -> None:
    projects, _ = prepare_projects_index(_archive())
    assert {p.name: p.is_temporary for p in projects} == {
        "-private-tmp-agents-clone": True,
        "-Users-u-code-beta": False,
        "-Users-u-code-alpha": False,
    }
    projects, _ = prepare_projects_index(_archive(), show_temp_projects=True)
    assert not any(p.is_temporary for p in projects)


def _html_index(theme: str, **kwargs: Any) -> str:
    renderer = HtmlRenderer()
    renderer.theme = theme
    return renderer.generate_projects_index(_archive(), **kwargs)


def _split_at_group(html: str) -> tuple[str, str]:
    before, marker, after = html.partition("<details class='temp-projects")
    assert marker, "the index should fold the temp projects into a group"
    return before, after


@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_html_index_folds_temp_projects_into_one_group(theme: str) -> None:
    html = _html_index(theme)
    before, group = _split_at_group(html)
    assert "Temporary directories (1)" in group
    assert "-private-tmp-agents-clone/combined_transcripts.html" in group
    assert "-private-tmp-agents-clone" not in before
    for real in ("-Users-u-code-alpha", "-Users-u-code-beta"):
        assert f"{real}/combined_transcripts.html" in before
        assert real not in group
    # The empty project has no row at all.
    assert "pytest-of-u" not in html
    # Rows in the group are still project cards, so the page finder
    # (components/search.html) indexes them.
    assert "project-card" in group


@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_html_index_shows_temp_projects_on_request(theme: str) -> None:
    html = _html_index(theme, show_temp_projects=True)
    assert "temp-projects" not in html.split("</style>")[-1]
    assert "Temporary directories" not in html
    assert "-private-tmp-agents-clone/combined_transcripts.html" in html


@pytest.mark.parametrize("theme", ["classic", "minimal"])
def test_html_index_tree_folds_temp_projects_too(theme: str) -> None:
    html = _html_index(theme, expand_paths_tree=True)
    before, group = _split_at_group(html)
    assert "project-tree" in before
    assert "-private-tmp-agents-clone/combined_transcripts.html" in group


def test_markdown_index_lists_temp_projects_last() -> None:
    md = MarkdownRenderer().generate_projects_index(_archive())
    before, marker, after = md.partition("**Temporary directories (1)**")
    assert marker
    assert "## [beta]" in before and "## [alpha]" in before
    assert "-private-tmp-agents-clone/combined_transcripts.md" in after
    assert "pytest-of-u" not in md
    assert "**Total:** 3 projects" in md

    shown = MarkdownRenderer().generate_projects_index(
        _archive(), show_temp_projects=True
    )
    assert "Temporary directories" not in shown
    # Back in last-modified order: the newest first.
    assert shown.index("agents-clone") < shown.index("## [beta]")


def test_markdown_index_unchanged_without_temp_projects() -> None:
    real = [p for p in _archive() if p["name"].startswith("-Users")]
    assert MarkdownRenderer().generate_projects_index(
        real
    ) == MarkdownRenderer().generate_projects_index(real, show_temp_projects=True)


# -- the CLI ------------------------------------------------------------------


def _write_project(
    projects: Path, cwd: str, session_id: str, folder: str | None = None
) -> None:
    project = projects / (folder or cwd.replace("/", "-"))
    project.mkdir(parents=True)
    lines = [
        {
            "type": "user",
            "timestamp": "2026-11-16T10:00:00.000Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": cwd,
            "sessionId": session_id,
            "version": "2.0.0",
            "uuid": f"{session_id}-u",
            "message": {"role": "user", "content": "Do the thing."},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-16T10:00:05.000Z",
            "parentUuid": f"{session_id}-u",
            "isSidechain": False,
            "userType": "external",
            "cwd": cwd,
            "sessionId": session_id,
            "version": "2.0.0",
            "uuid": f"{session_id}-a",
            "requestId": f"req-{session_id}",
            "message": {
                "id": f"msg-{session_id}",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    (project / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    _write_project(root, "/Users/u/code/alpha", "aaaa1111")
    _write_project(root, "/private/tmp/agents/clone", "tttt2222")
    return root


def _convert(projects: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = CliRunner().invoke(
        main, [str(projects), "--all-projects", "--jobs", "1", *args], env=env
    )
    assert result.exit_code == 0, result.output
    return (projects / "index.html").read_text(encoding="utf-8")


def test_cli_folds_temp_projects_by_default(projects: Path) -> None:
    assert "Temporary directories (1)" in _convert(
        projects, env={SHOW_TEMP_PROJECTS_ENV_VAR: ""}
    )


def test_cli_flag_shows_them(projects: Path) -> None:
    assert "Temporary directories" not in _convert(projects, "--show-temp-projects")


def test_cli_env_var_shows_them(projects: Path) -> None:
    assert "Temporary directories" not in _convert(
        projects, env={SHOW_TEMP_PROJECTS_ENV_VAR: "1"}
    )


@pytest.mark.parametrize("command", ["serve", "watch"])
def test_serve_and_watch_take_the_flag(command: str) -> None:
    result = CliRunner().invoke(main, [command, "--help"])
    assert result.exit_code == 0
    assert "--show-temp-projects" in result.output


def test_without_a_cache_the_messages_cwds_decide(tmp_path: Path) -> None:
    """``--no-cache`` has no cached working dirs: the loaded messages' cwds
    stand in, so a temp-only project under a non-temp folder name is still
    folded, as the cached path folds it."""
    projects = tmp_path / "projects"
    _write_project(projects, "/Users/u/code/alpha", "aaaa1111")
    _write_project(projects, "/tmp/run", "tttt2222", folder="-home-u-work")
    before, group = _split_at_group(_convert(projects, "--no-cache"))
    assert "-home-u-work/combined_transcripts.html" in group
    assert "-home-u-work" not in before
