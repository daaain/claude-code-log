"""The test run never sees the host's claude-code-log settings.

A developer's shell may export ``CLAUDE_CODE_LOG_THEME``, a cache path,
a render-pool size... and every one of them changes what the code under
test does: with them set to unusual values, 252 unit tests failed. So
``conftest.py`` removes them (`test.host_env`) before anything imports
the package — some settings are read at import time — and tests that
want one set it themselves with ``monkeypatch``.

Generic host state (``TZ``, ``HOME``, colour and terminal width) was
measured too and changes no result, so it is left alone.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from test.host_env import is_host_setting

PACKAGE = Path(__file__).resolve().parent.parent / "claude_code_log"


def test_no_host_setting_is_visible() -> None:
    assert [name for name in os.environ if is_host_setting(name)] == []


def test_settings_read_at_import_time_saw_a_clean_environment() -> None:
    from claude_code_log import renderer_timings

    assert renderer_timings.DEBUG_TIMING is False


def test_a_poisoned_host_environment_does_not_reach_the_tests() -> None:
    """The checks above, in a child run whose environment carries host
    settings — so the scrub itself is exercised, not just a clean CI box."""
    poisoned = {
        **os.environ,
        "CLAUDE_CODE_LOG_THEME": "minimal",
        "CLAUDE_CODE_LOG_DEBUG_TIMING": "1",
        "CLAUDE_CODE_LOG_A_SETTING_ADDED_LATER": "1",
        "CODEX_HOME": "/nonexistent/codex-home",
    }
    here = Path(__file__)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n0",
            "-p",
            "no:cacheprovider",
            f"{here}::test_no_host_setting_is_visible",
            f"{here}::test_settings_read_at_import_time_saw_a_clean_environment",
        ],
        cwd=here.parent.parent,
        env=poisoned,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Every setting the package reads is one the scrub removes
# ---------------------------------------------------------------------------


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments."""
    found: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = value.value
    return found


def _is_environ(node: ast.expr) -> bool:
    """``os.environ`` or a bare ``environ``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _env_name_nodes(tree: ast.Module) -> list[ast.expr]:
    """The expressions naming an environment variable the code reads:
    ``os.environ.get(X)``, ``os.environ[X]``, ``os.getenv(X)``, and a
    Click option's ``envvar=X``."""
    nodes: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if node.args and (
                (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("get", "pop", "setdefault")
                    and _is_environ(func.value)
                )
                or (isinstance(func, ast.Attribute) and func.attr == "getenv")
                or (isinstance(func, ast.Name) and func.id == "getenv")
            ):
                nodes.append(node.args[0])
            nodes.extend(kw.value for kw in node.keywords if kw.arg == "envvar")
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            nodes.append(node.slice)
    return nodes


def _settings_the_package_reads() -> set[str]:
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in PACKAGE.rglob("*.py")
    }
    # Names are resolved package-wide: a constant defined in one module
    # (search.ENV_SEARCH_FIELDS) is often read through an import in another.
    constants: dict[str, str] = {}
    for tree in trees.values():
        constants.update(_string_constants(tree))
    names: set[str] = set()
    for path, tree in trees.items():
        for node in _env_name_nodes(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            elif isinstance(node, ast.Name) and node.id in constants:
                names.add(constants[node.id])
            else:
                raise AssertionError(
                    f"{path.relative_to(PACKAGE.parent)}:{node.lineno}: an "
                    f"environment variable named by {ast.unparse(node)!r}, "
                    "which this scan can't resolve to a string: name it with "
                    "a literal or a module-level constant"
                )
    return names


def test_the_scan_finds_the_settings_it_should() -> None:
    """Guards the guard: a scan that silently found nothing would pass."""
    names = _settings_the_package_reads()
    assert {
        "CLAUDE_CODE_LOG_THEME",  # module constant, read in another module
        "CLAUDE_CODE_LOG_DEBUG_TIMING",  # literal, read at import
        "CLAUDE_CODE_LOG_GIT_LINK",  # a Click option's envvar=
        "CODEX_HOME",  # outside the prefix
    } <= names


def test_every_setting_the_package_reads_is_scrubbed() -> None:
    unscrubbed = sorted(
        n for n in _settings_the_package_reads() if not is_host_setting(n)
    )
    assert unscrubbed == [], (
        "The package reads these environment variables, but the test run "
        "doesn't remove them from the host environment: add them to "
        "test/host_env.py"
    )
