"""Codex user-shell command expansion."""

from claude_code_log.factories.user_factory import (
    create_bash_input_message,
    create_bash_output_message,
)
from claude_code_log.models import MessageMeta
from claude_code_log.providers.codex_messages import parse_codex_user_shell_command


SHELL_MESSAGE = """<user_shell_command>
<command>
ls work/
</command>
<result>
Exit code: 0
Duration: 0.0167 seconds
Output:
codex-provider.md
codex-tools.md

</result>
</user_shell_command>"""


def test_user_shell_envelope_decodes_command_and_output() -> None:
    shell = parse_codex_user_shell_command(SHELL_MESSAGE)

    assert shell is not None
    assert shell.command == "ls work/"
    assert shell.output == "codex-provider.md\ncodex-tools.md"
    assert shell.exit_code == 0
    assert shell.duration == "0.0167 seconds"


def test_decoded_values_select_existing_user_bash_models() -> None:
    shell = parse_codex_user_shell_command(SHELL_MESSAGE)
    assert shell is not None
    meta = MessageMeta.empty()

    bash_input = create_bash_input_message(
        meta, f"<bash-input>{shell.command}</bash-input>"
    )
    bash_output = create_bash_output_message(
        meta, f"<bash-stdout>{shell.output}</bash-stdout>"
    )

    assert bash_input is not None and bash_input.command == "ls work/"
    assert bash_output is not None
    assert bash_output.stdout == "codex-provider.md\ncodex-tools.md"


def test_malformed_or_unsafe_shell_envelopes_are_not_decoded() -> None:
    assert parse_codex_user_shell_command("<user_shell_command>") is None
    unsafe = SHELL_MESSAGE.replace("ls work/", "echo </bash-input>")
    assert parse_codex_user_shell_command(unsafe) is None
