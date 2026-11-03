"""Browser tests that change their browser *context* must not share it.

``page`` lives in a persistent context shared by every browser test on an
xdist worker (conftest.py). Playwright's clock belongs to the context, so
``page.clock.install()`` there leaks a fake clock into the tests that
follow; ``isolated_page`` gives a context of the test's own. This checks
statically that every test touching the clock asks for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

TEST_DIR = Path(__file__).parent


def _tests_using_the_clock() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(TEST_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith(
                "test"
            ):
                continue
            uses_clock = any(
                isinstance(sub, ast.Attribute) and sub.attr == "clock"
                for sub in ast.walk(node)
            )
            if uses_clock:
                found.append((path.name, node))
    return found


def test_clock_users_take_an_isolated_page() -> None:
    offenders = [
        f"{name}::{node.name}"
        for name, node in _tests_using_the_clock()
        if "isolated_page" not in {arg.arg for arg in node.args.args}
    ]
    assert not offenders, (
        "these install a Playwright clock on the shared context; "
        f"take `isolated_page` instead of `page`: {offenders}"
    )


def test_the_check_finds_the_clock_users() -> None:
    """The check above is only as good as its search."""
    names = {node.name for _, node in _tests_using_the_clock()}
    assert "test_reload_picks_up_new_sessions_in_place" in names
