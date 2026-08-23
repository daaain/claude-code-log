"""Regression tests for the subcommand-capable CLI.

`claude-code-log` was a single flat Click command for its whole life. It is
now a group with a `convert` default, so every historical invocation shape
has to keep resolving to `convert` — that is what these tests pin. Without
them, a change to `DefaultCommandGroup.parse_args` can silently break
`claude-code-log --from-date yesterday` while every other test still passes.
"""

from pathlib import Path
from typing import Any, Optional

import click
import pytest
from click.testing import CliRunner

from claude_code_log.cli import DefaultCommandGroup, main


def _record_group() -> tuple[click.Group, dict[str, Any]]:
    """A miniature clone of the real group that records what it resolved to.

    Invoking the real `convert` would run a full conversion, so the routing
    is verified against a stand-in built on the same group class.
    """
    seen: dict[str, Any] = {}

    @click.group(cls=DefaultCommandGroup)
    def cli() -> None:
        pass

    @cli.command(name="convert")
    @click.argument("input_path", required=False)
    @click.option("--from-date")
    @click.option("--all-projects", is_flag=True)
    def _convert(
        input_path: Optional[str], from_date: Optional[str], all_projects: bool
    ) -> None:
        seen.update(
            cmd="convert",
            input_path=input_path,
            from_date=from_date,
            all_projects=all_projects,
        )

    @cli.command(name="serve")
    @click.option("--port", type=int, default=8010)
    def _serve(port: int) -> None:
        seen.update(cmd="serve", port=port)

    return cli, seen


@pytest.mark.parametrize(
    "args,expected",
    [
        pytest.param([], {"cmd": "convert", "input_path": None}, id="bare"),
        pytest.param(
            ["some/file.jsonl"],
            {"cmd": "convert", "input_path": "some/file.jsonl"},
            id="positional-path",
        ),
        pytest.param(
            ["--from-date", "yesterday"],
            {"cmd": "convert", "from_date": "yesterday"},
            id="option-first",
        ),
        pytest.param(
            ["--all-projects"],
            {"cmd": "convert", "all_projects": True},
            id="flag-first",
        ),
        pytest.param(
            ["convert", "some/file.jsonl"],
            {"cmd": "convert", "input_path": "some/file.jsonl"},
            id="explicit-convert",
        ),
        pytest.param(["serve"], {"cmd": "serve", "port": 8010}, id="subcommand"),
        pytest.param(
            ["serve", "--port", "9999"], {"cmd": "serve", "port": 9999}, id="sub-opts"
        ),
        pytest.param(
            ["--", "serve"],
            {"cmd": "convert", "input_path": "serve"},
            id="dash-dash-escape",
        ),
    ],
)
def test_invocation_routes_to_expected_command(
    args: list[str], expected: dict[str, Any]
) -> None:
    cli, seen = _record_group()
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    for key, value in expected.items():
        assert seen[key] == value, f"{key}: {seen.get(key)!r} != {value!r}"


def test_dash_dash_escape_reaches_a_path_named_like_a_subcommand() -> None:
    """A project directory called `serve` must still be convertible."""
    cli, seen = _record_group()
    result = CliRunner().invoke(cli, ["--", "serve"])
    assert result.exit_code == 0, result.output
    assert seen["cmd"] == "convert"
    assert seen["input_path"] == "serve"


def test_help_shows_convert_options_not_bare_group_help() -> None:
    """`--help` must keep showing the full option list, as it always did."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for expected_option in ("--from-date", "--all-projects", "--output", "--debug"):
        assert expected_option in result.output, expected_option


def test_help_advertises_registered_subcommands() -> None:
    """The group's own help is unreachable, so subcommands must surface here.

    The epilog is derived from the registered commands, so this also pins
    that it never advertises something that doesn't exist.
    """
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    extras = [name for name in main.commands if name != "convert"]
    if extras:
        assert "Subcommands:" in result.output
        for name in extras:
            assert name in result.output
    else:
        assert "Subcommands:" not in result.output


def test_version_still_works_without_a_subcommand() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "claude-code-log, version" in result.output


def test_convert_is_the_default_command() -> None:
    assert "convert" in main.commands
    assert DefaultCommandGroup.default_command == "convert"


def test_unknown_option_still_errors_rather_than_being_swallowed() -> None:
    """Rewriting args must not turn a typo into a silent no-op."""
    result = CliRunner().invoke(main, ["--definitely-not-an-option"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_convert_help_is_reachable_explicitly(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["convert", "--help"])
    assert result.exit_code == 0
    assert "INPUT_PATH" in result.output
