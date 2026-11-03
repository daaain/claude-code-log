"""Tests for scripts/release_notes.py, which turns one CHANGELOG.md section
into the GitHub Release's notes (in the release workflow) and previews them
locally (`just release-preview`)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "release_notes.py"

_CHANGELOG = """# Changelog

All notable changes to claude-code-log will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [1.10.0] - 2026-10-04

### Changed

- **Newest change (#2)**
- **Another change**

## [1.9.0] - 2026-09-01

### Changed

- **Older change (#1)**

## Previous Versions

Earlier versions focused on basic JSONL to HTML conversion.
"""


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load_script()


def test_extracts_the_section_and_links_the_previous_version():
    assert release_notes.notes(_CHANGELOG, "1.10.0") == (
        "### Changed\n"
        "\n"
        "- **Newest change (#2)**\n"
        "- **Another change**\n"
        "\n"
        "**Full Changelog**: "
        "https://github.com/daaain/claude-code-log/compare/1.9.0...1.10.0\n"
    )


def test_oldest_versioned_section_stops_at_any_heading_and_has_no_link():
    assert release_notes.notes(_CHANGELOG, "1.9.0") == (
        "### Changed\n\n- **Older change (#1)**\n"
    )


def test_version_is_matched_exactly_not_as_a_prefix():
    # "1.1.0" is a substring of "[1.10.0]" once the bracket is dropped.
    with pytest.raises(release_notes.MissingSection):
        release_notes.notes(_CHANGELOG, "1.1.0")


def test_cli_prints_notes_and_fails_loudly_without_a_section(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_CHANGELOG)

    ok = subprocess.run(
        [sys.executable, str(_SCRIPT), "1.9.0", str(changelog)],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0
    assert ok.stdout == "### Changed\n\n- **Older change (#1)**\n"

    missing = subprocess.run(
        [sys.executable, str(_SCRIPT), "2.0.0", str(changelog)],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert "## [2.0.0]" in missing.stderr
