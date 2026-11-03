"""Codex user-shell command expansion."""

from claude_code_log.factories.user_factory import (
    create_bash_input_message,
    create_bash_output_message,
)
from claude_code_log.models import (
    AssistantTranscriptEntry,
    MessageMeta,
    TextContent,
    UserTranscriptEntry,
)
from claude_code_log.providers.codex import CodexProvider
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


class TestProvider(CodexProvider):
    __test__ = False

    def normalize_user_text(
        self, text: str
    ) -> list[UserTranscriptEntry | AssistantTranscriptEntry]:
        return self._normalize_user_text(
            "session", "entry", "2026-07-14T00:00:00Z", text
        )


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
    well_formed_breakout = SHELL_MESSAGE.replace(
        "ls work/", "printf '&lt;/bash-input&gt;'"
    )
    assert parse_codex_user_shell_command(well_formed_breakout) is None


def test_failed_user_shell_command_keeps_exit_and_duration_losslessly() -> None:
    failed = SHELL_MESSAGE.replace("Exit code: 0", "Exit code: 7")

    entries = TestProvider().normalize_user_text(failed)

    assert len(entries) == 1
    content = entries[0].message.content
    assert len(content) == 1 and isinstance(content[0], TextContent)
    assert content[0].text == failed
    assert "Exit code: 7" in content[0].text
    assert "Duration: 0.0167 seconds" in content[0].text
