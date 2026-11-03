"""Codex Bash result transport normalization."""

from typing import Any

from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.models import BashOutput, ToolResultContent
from claude_code_log.providers.codex import CodexProvider


class TestProvider(CodexProvider):
    __test__ = False

    def normalize_tool_output(
        self, value: object, tool_name: str
    ) -> str | list[dict[str, Any]]:
        return self._tool_output(value, tool_name=tool_name)


def _direct_result(output: str) -> list[dict[str, str]]:
    return [
        {
            "type": "input_text",
            "text": "Script completed\nWall time: 0.2 seconds\nOutput:\n",
        },
        {"type": "input_text", "text": output},
    ]


def test_direct_text_command_result_becomes_bash_output() -> None:
    provider = TestProvider()
    source = "# not markdown\nprint('literal output')\n"

    normalized = provider.normalize_tool_output(_direct_result(source), "Bash")
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-bash",
        content=normalized,
    )
    parsed = create_tool_output("Bash", raw)

    assert normalized == source
    assert isinstance(parsed, BashOutput)
    assert parsed.content == source


def test_multiple_direct_text_chunks_preserve_order() -> None:
    provider = TestProvider()
    items = _direct_result("first")
    items.append({"type": "input_text", "text": "second\n"})

    assert provider.normalize_tool_output(items, "Bash") == "first\nsecond\n"


def test_legacy_status_without_wall_time_colon_is_supported() -> None:
    provider = TestProvider()
    items = _direct_result("plain output")
    items[0]["text"] = "Script completed\nWall time 0.2 seconds\nOutput:\n"

    assert provider.normalize_tool_output(items, "Bash") == "plain output"


def test_same_transport_is_not_unwrapped_for_non_bash_tools() -> None:
    provider = TestProvider()
    items = _direct_result("{}")

    assert provider.normalize_tool_output(items, "TodoWrite") == items


def test_unrecognized_bash_structure_keeps_generic_content() -> None:
    provider = TestProvider()
    items = [{"type": "input_text", "text": "A future transport shape"}]

    assert provider.normalize_tool_output(items, "Bash") == items
