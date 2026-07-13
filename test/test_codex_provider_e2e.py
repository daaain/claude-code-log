"""Real Codex provider-to-registry-to-renderer export contract."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main


FIXTURES = Path(__file__).parent / "test_data" / "codex"
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize(("format_name", "suffix"), [("html", "html"), ("md", "md")])
def test_real_codex_provider_exports_semantic_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
    suffix: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(FIXTURES))
    output = tmp_path / f"codex.{suffix}"

    result = CliRunner().invoke(
        main,
        [
            "--provider",
            "codex",
            "--session-id",
            SESSION_ID,
            "--format",
            format_name,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    document = output.read_text()
    expected_in_order = [
        "List the synthetic files.",
        "I will inspect the synthetic directory.",
        "The synthetic files are alpha.txt and beta.txt.",
    ]
    positions = [document.index(text) for text in expected_in_order]
    assert positions == sorted(positions)
    assert "find . -maxdepth 1 -type f" in document
    assert "./alpha.txt" in document and "./beta.txt" in document
    assert "apply_patch" in document
    assert "mcp__synthetic__lookup" in document
    assert "tests running" in document and "2 passed" in document
    assert "write_stdin" not in document
    assert "call-wait-001" not in document
    assert "call-write-001" not in document
    assert "Script running with cell ID" not in document
    assert "SYNTHETIC_ENCRYPTED_CONTENT_MUST_NOT_RENDER" not in document
