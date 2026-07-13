"""Privacy and completeness checks for the documented Codex schema corpus."""

import json
from pathlib import Path


CORPUS = Path(__file__).parents[1] / "dev-docs" / "messages" / "codex"
EXPECTED_FAMILIES = {
    "session metadata",
    "turn context",
    "visible message pairs",
    "environment context",
    "user shell command",
    "reasoning summary",
    "structured tool call",
    "exec wrapper",
    "async command",
    "web run",
    "thread spawn",
    "collaboration tools",
}


def test_codex_schema_manifest_is_complete_valid_and_sanitized() -> None:
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    families = manifest["families"]
    assert set(families) == EXPECTED_FAMILIES

    forbidden = (
        "/home/",
        "/Users/",
        "github.com/",
        "Bearer ",
        "api_key",
        "access_token",
        "refresh_token",
        "gAAAAA",
    )
    for relative_path in families.values():
        path = CORPUS / relative_path
        assert path.is_file(), relative_path
        value = json.loads(path.read_text())
        serialized = json.dumps(value)
        assert not any(fragment in serialized for fragment in forbidden)
