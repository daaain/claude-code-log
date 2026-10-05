"""The demo archive the videos are recorded from (``record.py``).

Synthetic, so anyone can re-record the videos, and written to read like real
work rather than test data: one project, ``claude-code-log``, whose sessions
between them show what the minimal theme renders —

* the theme work (``test/dag_demo_fixture.write_dag_demo``): background,
  synchronous and nested agents, and a rewound prompt that forks;
* a Windows-only CI failure tracked down (``_ci_session``): thinking, a todo
  list, a red pytest run and a green one, a highlighted ``Read``, diffs and a
  markdown write-up;
* a ``watch`` command planned, then built (``_watch_session``): a web search,
  a question for the user, an approved plan, a new file, and a ``/compact``.

The committed real sample projects (``test/test_data/real_projects``) are
copied in beside it, so the index and the archive search have an archive's
breadth.
"""

from __future__ import annotations

import json
import shutil
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
PROJECT = "-home-dev-claude-code-log"
CWD = "/home/dev/claude-code-log"
REAL_PROJECTS = REPO / "test" / "test_data" / "real_projects"

CI_SESSION = "c1c1c1c1-0000-4000-8000-000000000001"
WATCH_SESSION = "c1c1c1c1-0000-4000-8000-000000000002"

# Plain, as pytest writes when its output is not a terminal (which also
# keeps the collapsed previews free of escape codes).
RED = GREEN = BOLD = RESET = ""


class Session:
    """Appends entries to one session, each parented on the previous one."""

    def __init__(self, session_id: str, start: datetime, branch: str) -> None:
        self.session_id = session_id
        self.start = start
        self.branch = branch
        self.entries: list[dict[str, Any]] = []
        self.last: Optional[str] = None
        self._count = 0
        self._tools = 0

    def _uuid(self) -> str:
        self._count += 1
        return str(
            uuid_lib.uuid5(uuid_lib.NAMESPACE_URL, f"{self.session_id}/{self._count}")
        )

    def _base(self, kind: str, at: float, **extra: Any) -> dict[str, Any]:
        stamp = self.start + timedelta(seconds=at)
        entry: dict[str, Any] = {
            "parentUuid": self.last,
            "isSidechain": False,
            "userType": "external",
            "cwd": CWD,
            "sessionId": self.session_id,
            "version": "2.1.180",
            "gitBranch": self.branch,
            "type": kind,
            "uuid": self._uuid(),
            "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{stamp.microsecond // 1000:03d}Z",
        }
        entry.update(extra)
        self.entries.append(entry)
        self.last = entry["uuid"]
        return entry

    def user(self, at: float, content: Any, **extra: Any) -> dict[str, Any]:
        return self._base(
            "user", at, message={"role": "user", "content": content}, **extra
        )

    def assistant(
        self, at: float, content: list[dict[str, Any]], out_tokens: int = 240
    ) -> dict[str, Any]:
        entry = self._base(
            "assistant",
            at,
            message={
                "id": f"msg_{self._count:04d}{self.session_id[:8]}",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "stop_reason": None,
                "content": content,
                "usage": {
                    "input_tokens": 6,
                    "cache_creation_input_tokens": 2100,
                    "cache_read_input_tokens": 31800 + 900 * self._count,
                    "output_tokens": out_tokens,
                },
            },
            requestId=f"req_{self._count:04d}{self.session_id[:8]}",
        )
        return entry

    def say(self, at: float, text: str) -> None:
        self.assistant(at, [{"type": "text", "text": text}])

    def think(self, at: float, text: str) -> None:
        self.assistant(
            at, [{"type": "thinking", "thinking": text, "signature": "demo"}]
        )

    def tool(
        self,
        at: float,
        name: str,
        inp: dict[str, Any],
        content: Any,
        result: Any = None,
        *,
        is_error: bool = False,
        took: float = 2,
    ) -> None:
        """A tool call and its result ``took`` seconds later."""
        self._tools += 1
        tool_id = f"toolu_01{self.session_id[:6]}{self._tools:04d}demo"
        self.assistant(
            at, [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}], 90
        )
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        extra = {"toolUseResult": result} if result is not None else {}
        self.user(at + took, [block], **extra)

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.session_id}.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.entries), encoding="utf-8"
        )


def _numbered(text: str, first: int) -> str:
    return "\n".join(
        f"{first + i:>6}→{line}" for i, line in enumerate(text.splitlines())
    )


def _read(s: Session, at: float, path: str, text: str, first: int) -> None:
    lines = text.splitlines()
    s.tool(
        at,
        "Read",
        {"file_path": f"{CWD}/{path}", "offset": first, "limit": len(lines)},
        _numbered(text, first),
        {
            "type": "text",
            "file": {
                "filePath": f"{CWD}/{path}",
                "content": text,
                "numLines": len(lines),
                "startLine": first,
                "totalLines": 2400,
            },
        },
    )


def _edit(s: Session, at: float, path: str, old: str, new: str) -> None:
    s.tool(
        at,
        "Edit",
        {"file_path": f"{CWD}/{path}", "old_string": old, "new_string": new},
        f"The file {CWD}/{path} has been updated successfully.",
        {
            "filePath": f"{CWD}/{path}",
            "oldString": old,
            "newString": new,
            "replaceAll": False,
            "userModified": False,
        },
    )


def _bash(
    s: Session,
    at: float,
    command: str,
    description: str,
    out: str,
    *,
    code: int = 0,
    took: float = 4,
) -> None:
    if code:
        s.tool(
            at,
            "Bash",
            {"command": command, "description": description},
            f"Exit code {code}\n{out}",
            f"Error: Exit code {code}\n{out}",
            is_error=True,
            took=took,
        )
    else:
        s.tool(
            at,
            "Bash",
            {"command": command, "description": description},
            out,
            {"stdout": out, "stderr": "", "interrupted": False, "isImage": False},
            took=took,
        )


def _todos(s: Session, at: float, items: list[tuple[str, str, str]]) -> None:
    todos = [
        {"content": content, "status": status, "activeForm": active}
        for content, status, active in items
    ]
    s.tool(
        at,
        "TodoWrite",
        {"todos": todos},
        "Todos have been modified successfully. Ensure that you continue to "
        "use the todo list to track your progress.",
        {"oldTodos": [], "newTodos": todos},
        took=0.3,
    )


CACHE_KEY_BEFORE = '''\
def _file_key(self, jsonl_path: Path) -> str:
    """The cache's key for a transcript: its path below the projects dir."""
    relative = str(jsonl_path).removeprefix(str(self.projects_dir))
    return relative.lstrip("/")
'''

PYTEST_RED = f"""\
{BOLD}============================= test session starts =============================={RESET}
collected 48 items

test/test_cache.py {GREEN}.............................................{RESET}{RED}F{RESET}{GREEN}..{RESET}  [100%]

=================================== FAILURES ===================================
{RED}{BOLD}_______________ TestCacheKeys.test_key_is_stable_across_runs ________________{RESET}

    def test_key_is_stable_across_runs(self, tmp_path):
        cache = CacheManager(tmp_path, "1.7.0")
        session = tmp_path / "-repo" / "abc.jsonl"
>       assert cache._file_key(session) == "-repo/abc.jsonl"
{RED}E       AssertionError: assert '-repo\\\\abc.jsonl' == '-repo/abc.jsonl'{RESET}
{RED}E         - -repo/abc.jsonl{RESET}
{RED}E         + -repo\\\\abc.jsonl{RESET}

{RED}FAILED{RESET} test/test_cache.py::TestCacheKeys::test_key_is_stable_across_runs
{RED}========================= {BOLD}1 failed{RESET}{RED}, 47 passed in 3.12s ========================={RESET}"""

PYTEST_GREEN = f"""\
{BOLD}============================= test session starts =============================={RESET}
collected 49 items

test/test_cache.py {GREEN}.................................................{RESET}  [100%]

{GREEN}============================== {BOLD}49 passed{RESET}{GREEN} in 3.08s =============================={RESET}"""


def _ci_session(directory: Path, start: datetime) -> None:
    s = Session(CI_SESSION, start, "fix/windows-cache-keys")
    s.user(
        0,
        "CI is red on the Windows runner again: `test_cache.py` fails there but "
        "passes on macOS and Linux. Can you dig in? The log says something "
        "about the cache key.",
    )
    s.think(
        6,
        "A failure that only shows up on Windows, in the cache tests, around "
        "the cache key. The usual suspects are path separators and "
        "case-insensitive paths. The key is derived from the transcript's "
        "path, so if it is built with str() it will carry backslashes on "
        "Windows, and keys written by one OS won't match another's. I should "
        "reproduce it before guessing: the test can simulate a Windows path.",
    )
    s.say(9, "Let me reproduce it first, then find where the key is built.")
    _todos(
        s,
        11,
        [
            (
                "Reproduce the Windows failure",
                "in_progress",
                "Reproducing the Windows failure",
            ),
            ("Find where the cache key is built", "pending", "Finding the cache key"),
            ("Fix it and add a regression test", "pending", "Fixing the cache key"),
        ],
    )
    _bash(
        s,
        14,
        "uv run pytest test/test_cache.py -q -p no:randomly",
        "Run the cache tests",
        PYTEST_RED,
        code=1,
        took=6,
    )
    s.tool(
        24,
        "Grep",
        {"pattern": "def _file_key|_file_key\\(", "path": f"{CWD}/claude_code_log"},
        "Found 2 files\nclaude_code_log/cache.py\nclaude_code_log/converter.py",
        {
            "mode": "files_with_matches",
            "numFiles": 2,
            "filenames": ["claude_code_log/cache.py", "claude_code_log/converter.py"],
        },
        took=1,
    )
    _read(s, 27, "claude_code_log/cache.py", CACHE_KEY_BEFORE, 412)
    s.say(
        33,
        "Found it. `_file_key` builds the key with `str(path)`, so on Windows it "
        "carries backslashes: a cache written there never matches one written "
        "anywhere else, and the test's expected key fails. `Path.relative_to` "
        "plus `as_posix()` gives the same key on every OS.",
    )
    _edit(
        s,
        38,
        "claude_code_log/cache.py",
        "    relative = str(jsonl_path).removeprefix(str(self.projects_dir))\n"
        '    return relative.lstrip("/")',
        "    relative = jsonl_path.relative_to(self.projects_dir)\n"
        "    return relative.as_posix()",
    )
    _edit(
        s,
        47,
        "test/test_cache.py",
        "    def test_key_is_stable_across_runs(self, tmp_path):",
        "    def test_key_uses_forward_slashes(self, tmp_path):\n"
        '        cache = CacheManager(tmp_path, "1.7.0")\n'
        '        windows = PureWindowsPath(tmp_path) / "-repo" / "abc.jsonl"\n'
        '        assert "\\\\" not in cache._file_key(windows)\n'
        "\n"
        "    def test_key_is_stable_across_runs(self, tmp_path):",
    )
    _bash(
        s,
        58,
        "uv run pytest test/test_cache.py -q -p no:randomly",
        "Run the cache tests again",
        PYTEST_GREEN,
        took=6,
    )
    _todos(
        s,
        66,
        [
            (
                "Reproduce the Windows failure",
                "completed",
                "Reproducing the Windows failure",
            ),
            ("Find where the cache key is built", "completed", "Finding the cache key"),
            ("Fix it and add a regression test", "completed", "Fixing the cache key"),
        ],
    )
    s.say(
        72,
        "## Fixed: cache keys are now the same on every OS\n\n"
        "**Root cause.** `CacheManager._file_key` turned the transcript path into "
        "a string and stripped the projects dir as a prefix. On Windows that "
        "string uses `\\`, so the key differed from the one written on macOS "
        "or Linux, and the test pinning the key format failed.\n\n"
        "**Fix.** Build the key with `relative_to(...).as_posix()`:\n\n"
        "```python\n"
        "def _file_key(self, jsonl_path: Path) -> str:\n"
        "    relative = jsonl_path.relative_to(self.projects_dir)\n"
        "    return relative.as_posix()\n"
        "```\n\n"
        "| | Before | After |\n"
        "|---|---|---|\n"
        "| macOS / Linux key | `-repo/abc.jsonl` | `-repo/abc.jsonl` |\n"
        "| Windows key | `-repo\\abc.jsonl` | `-repo/abc.jsonl` |\n"
        "| `test_cache.py` | 47 passed, 1 failed | 49 passed |\n\n"
        "The new `test_key_uses_forward_slashes` builds a `PureWindowsPath`, so "
        "it guards the fix on every runner, not only on Windows. Existing "
        "Windows caches re-key once, on their next run.",
    )
    s.write(directory)


WATCH_PLAN = """\
## Plan: `claude-code-log watch`

Regenerate the HTML whenever a transcript changes, so an open page stays current
while a session runs.

### Approach

1. **Poll, don't subscribe.** Stat every `*.jsonl` under the project once a
   second. No new dependency, and it behaves the same on every OS and on network
   drives, where file events are unreliable.
2. **Debounce.** Claude Code appends in bursts; wait until a file has been quiet
   for `--debounce` seconds (default 0.5) before converting.
3. **Convert incrementally.** Reuse the cache, so a tick re-parses only the
   files that changed.
4. **Report each tick** with its duration, and keep going on errors.

### Files

| File | Change |
|---|---|
| `claude_code_log/watch.py` | new: the polling engine |
| `claude_code_log/cli.py` | new `watch` command |
| `test/test_watch_engine.py` | new: ticks, debounce, errors |

### Out of scope

Live reload in the browser; that can come next, once the pages poll.
"""

WATCH_ENGINE = '''\
"""Poll a project for changed transcripts and re-convert after a quiet period."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

Stamp = tuple[int, int]  # (size, mtime_ns)


def scan(root: Path) -> dict[Path, Stamp]:
    """Every transcript under ``root`` with its size and mtime."""
    stamps: dict[Path, Stamp] = {}
    for path in root.rglob("*.jsonl"):
        st = path.stat()
        stamps[path] = (st.st_size, st.st_mtime_ns)
    return stamps


class WatchEngine:
    def __init__(
        self,
        root: Path,
        convert: Callable[[set[Path]], None],
        debounce: float = 0.5,
    ) -> None:
        self.root = root
        self.convert = convert
        self.debounce = debounce
        self._stamps = scan(root)

    def tick(self) -> set[Path]:
        """Changed files since the last tick, once they have gone quiet."""
        current = scan(self.root)
        changed = {p for p, s in current.items() if self._stamps.get(p) != s}
        if changed:
            time.sleep(self.debounce)
            if scan(self.root) != current:
                return set()  # still being written: next tick
            self.convert(changed)
        self._stamps = current
        return changed
'''


def _watch_session(directory: Path, start: datetime) -> None:
    s = Session(WATCH_SESSION, start, "feat/watch")
    s.user(
        0,
        "I'd like a `watch` command that regenerates the HTML whenever a "
        "transcript changes, so I can keep a session page open while Claude "
        "works. Plan it first, don't write code yet.",
    )
    s.tool(
        5,
        "WebSearch",
        {
            "query": "python watch directory for file changes polling vs inotify cross-platform"
        },
        'Web search results for query: "python watch directory for file '
        'changes polling vs inotify cross-platform"',
        {
            "query": "python watch directory for file changes polling vs inotify cross-platform",
            "results": [
                {
                    "tool_use_id": "srvtoolu_01demo",
                    "content": [
                        {
                            "title": "watchdog: Python API to monitor file system events",
                            "url": "https://pypi.org/project/watchdog/",
                        },
                        {
                            "title": "watchfiles: simple, modern file watching (Rust notify)",
                            "url": "https://pypi.org/project/watchfiles/",
                        },
                        {
                            "title": "inotify(7): monitoring filesystem events",
                            "url": "https://man7.org/linux/man-pages/man7/inotify.7.html",
                        },
                    ],
                },
                "Native file events (inotify, FSEvents, ReadDirectoryChangesW) are "
                "efficient but differ per OS and miss changes on network drives; "
                "watchdog and watchfiles wrap them and fall back to polling. For a "
                "few hundred files, polling stat() once a second is cheap and "
                "portable.",
            ],
            "durationSeconds": 6.1,
        },
        took=6,
    )
    question = (
        "Native file events need a new dependency (watchfiles); polling needs "
        "none and works on network drives. Which should `watch` use?"
    )
    s.tool(
        16,
        "AskUserQuestion",
        {
            "questions": [
                {
                    "question": question,
                    "header": "Watching",
                    "options": [
                        {
                            "label": "Poll every second",
                            "description": "No dependency, same on every OS; a few hundred stat() calls a tick",
                        },
                        {
                            "label": "watchfiles",
                            "description": "Instant native events, one more dependency",
                        },
                    ],
                    "multiSelect": False,
                }
            ]
        },
        f'User has answered your questions: "{question}"="Poll every second". '
        "You can now continue with the user's answers in mind.",
        {
            "questions": [
                {
                    "question": question,
                    "header": "Watching",
                    "options": [],
                    "multiSelect": False,
                }
            ],
            "answers": {question: "Poll every second"},
        },
        took=22,
    )
    s.tool(
        42,
        "ExitPlanMode",
        {"plan": WATCH_PLAN},
        "User has approved your plan. You can now start coding.",
        {"plan": WATCH_PLAN, "isAgent": False},
        took=35,
    )
    s.tool(
        80,
        "Write",
        {"file_path": f"{CWD}/claude_code_log/watch.py", "content": WATCH_ENGINE},
        f"File created successfully at: {CWD}/claude_code_log/watch.py",
        {
            "type": "create",
            "filePath": f"{CWD}/claude_code_log/watch.py",
            "content": WATCH_ENGINE,
            "structuredPatch": [],
        },
    )
    _bash(
        s,
        95,
        "uv run pytest test/test_watch_engine.py -q",
        "Run the new watch tests",
        f"{GREEN}............{RESET}  [100%]\n{GREEN}12 passed in 0.84s{RESET}",
    )
    s.say(
        102,
        "`WatchEngine` is in `claude_code_log/watch.py` with 12 tests. Next I'll "
        "wire it into the CLI as `claude-code-log watch`.",
    )
    # The context fills up here: /compact, then the work continues.
    s._base(
        "system",
        1500,
        subtype="compact_boundary",
        content="Conversation compacted",
        isMeta=False,
        level="info",
        compactMetadata={"trigger": "manual", "preTokens": 148210},
    )
    s.user(
        1501,
        "This session is being continued from a previous conversation that ran "
        "out of context. The conversation is summarized below:\n\n"
        "1. The user asked for a `watch` command that regenerates HTML when a "
        "transcript changes, planned before coding.\n"
        "2. Polling once a second was chosen over native events (no new "
        "dependency, works on network drives).\n"
        "3. `claude_code_log/watch.py` now has `scan()` and `WatchEngine` with a "
        "debounce; `test/test_watch_engine.py` has 12 passing tests.\n"
        "4. Next: the `watch` CLI command.",
        isCompactSummary=True,
    )
    s.user(1530, "Also make the debounce configurable from the command line, please.")
    _edit(
        s,
        1536,
        "claude_code_log/cli.py",
        '@click.command()\n@click.argument("path", type=click.Path(exists=True))',
        "@click.command()\n"
        '@click.option(\n    "--debounce",\n    default=0.5,\n    show_default=True,\n'
        '    help="Seconds a file must stay quiet before converting.",\n)\n'
        '@click.argument("path", type=click.Path(exists=True))',
    )
    _bash(
        s,
        1550,
        "uv run claude-code-log watch ~/.claude/projects/-home-dev-claude-code-log --debounce 1",
        "Try the watch command",
        "Watching -home-dev-claude-code-log (1 project, 3 sessions)\n"
        "  14:31:07  converted in 0.41s\n"
        "  14:31:22  converted in 0.18s",
        took=20,
    )
    s.say(
        1575,
        "Done: `claude-code-log watch PATH --debounce 1` re-converts within a "
        "second of a transcript going quiet, and each tick reports how long it "
        "took.",
    )
    s.write(directory)


def write_archive(projects: Path) -> Path:
    """Write the demo archive into ``projects``; returns the demo project dir."""
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from test.dag_demo_fixture import write_dag_demo

    project = projects / PROJECT
    write_dag_demo(project)
    for path in project.rglob("*.jsonl"):  # the fixture's own cwd
        path.write_text(
            path.read_text(encoding="utf-8").replace("/home/synthetic/theme", CWD),
            encoding="utf-8",
        )
    now = datetime(2026, 10, 3, 14, 2, 11, tzinfo=timezone.utc)
    _ci_session(project, now - timedelta(days=1, hours=3))
    _watch_session(project, now - timedelta(days=2, hours=5))
    for real in sorted(REAL_PROJECTS.iterdir()):
        if real.is_dir():
            shutil.copytree(real, projects / real.name)
    return project
