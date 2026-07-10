"""Regression tests for per-session output freshness across all formats.

The html_cache incremental-skip bookkeeping historically only worked for
the default HTML, in-place layout. These tests pin the four fixes on
`dev/cache-all-formats` so the perpetual-slow-path bug can't return:

- cache rows are written (and looked up) keyed by the variant-specific
  filename ``session-{id}{variant}.{ext}`` — not the hard-coded
  ``session-{id}.html`` — for *every* format;
- ``CacheManager.is_transcript_stale`` resolves the artifact against the
  real output directory (``--output`` destination), not the source
  project dir;
- ``--combined no`` (``write_combined=False``) does not veto the phase-1b
  early exit via the intentionally-absent combined file;
- the legacy default-HTML in-place behavior is unchanged.

All assertions use in-process ``convert_jsonl_to`` calls and a
``RegenerationReport`` out-parameter (or a direct cache query) rather than
stdout scraping — the CLI can serve stale output from an existing cache
after a code edit, and stdout is a weaker signal than the actual
regenerated counts.
"""

import json
import uuid
from pathlib import Path
from typing import Any

from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import RegenerationReport, convert_jsonl_to
from claude_code_log.models import DetailLevel


def _entry(text: str, session_id: str, parent: str | None) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2025-01-01T10:00:00Z",
        "sessionId": session_id,
        "uuid": f"u-{uuid.uuid4().hex[:8]}",
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _make_project(tmp_path: Path, session_id: str = "sess-1") -> Path:
    """Create a project dir with a single two-message session."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    first = _entry("first message", session_id, None)
    second = _entry("second message", session_id, first["uuid"])
    (project_dir / "transcript.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
    )
    return project_dir


def test_markdown_output_dir_writes_variant_keyed_cache_rows(tmp_path: Path):
    """A Markdown ``--output`` run records html_cache rows keyed by the
    variant-specific filename in the *destination* directory.

    This is the pin against the perpetual-slow-path bug: the previous code
    only wrote rows under ``format == "html"`` and keyed lookups on the
    default ``session-{id}.html`` name / source dir, so a Markdown variant
    export left the cache empty and re-rendered every session forever.
    """
    session_id = "abc123"
    project_dir = _make_project(tmp_path, session_id)
    dest = tmp_path / "out"

    convert_jsonl_to(
        "markdown",
        project_dir,
        output_root=dest,
        detail=DetailLevel.LOW,
        compact=True,
        generate_individual_sessions=True,
        silent=True,
    )

    # Variant-specific filenames actually written to the destination.
    session_name = f"session-{session_id}.low.compact.md"
    combined_name = "combined_transcripts.low.compact.md"
    assert (dest / session_name).exists()
    assert (dest / combined_name).exists()

    # And the html_cache has rows keyed by exactly those names.
    cache = CacheManager(project_dir, get_library_version())
    assert cache.get_html_cache(session_name) is not None, (
        "per-session Markdown output must record a variant-keyed cache row"
    )
    assert cache.get_html_cache(combined_name) is not None, (
        "combined Markdown output must record a variant-keyed cache row"
    )


def test_second_identical_run_regenerates_nothing(tmp_path: Path):
    """A quiescent second run takes the fast path: nothing is regenerated.

    Asserted via the ``RegenerationReport`` (regenerated counts), not
    stdout — the whole point of the fix is that the early exit fires.
    """
    project_dir = _make_project(tmp_path)
    dest = tmp_path / "out"

    def run() -> RegenerationReport:
        report = RegenerationReport()
        convert_jsonl_to(
            "markdown",
            project_dir,
            output_root=dest,
            generate_individual_sessions=True,
            silent=True,
            report=report,
        )
        return report

    first = run()
    # First run actually renders (cache freshly populated → full path).
    assert first.sessions_regenerated > 0
    assert first.combined_regenerated

    second = run()
    # Second, unchanged run regenerates nothing.
    assert second.sessions_regenerated == 0
    assert not second.combined_regenerated


def test_combined_no_does_not_veto_early_exit(tmp_path: Path):
    """``--combined no`` (``write_combined=False``) must not block the
    phase-1b early exit: the (intentionally absent) combined file's
    "staleness" would otherwise force a full reload every run."""
    project_dir = _make_project(tmp_path)
    dest = tmp_path / "out"

    def run() -> RegenerationReport:
        report = RegenerationReport()
        convert_jsonl_to(
            "markdown",
            project_dir,
            output_root=dest,
            write_combined=False,
            generate_individual_sessions=True,
            silent=True,
            report=report,
        )
        return report

    first = run()
    assert first.sessions_regenerated > 0
    # No combined file is produced under --combined no.
    assert not first.combined_regenerated
    assert not (dest / "combined_transcripts.md").exists()

    second = run()
    # The absent combined file does not veto the early exit.
    assert second.sessions_regenerated == 0
    assert not second.combined_regenerated


def test_is_transcript_stale_honors_output_dir(tmp_path: Path):
    """``is_transcript_stale`` resolves the file against the given
    ``output_dir``; against the source project dir the artifact is missing.
    """
    session_id = "xyz789"
    project_dir = _make_project(tmp_path, session_id)
    dest = tmp_path / "out"

    convert_jsonl_to(
        "markdown",
        project_dir,
        output_root=dest,
        generate_individual_sessions=True,
        silent=True,
    )

    cache = CacheManager(project_dir, get_library_version())
    session_name = f"session-{session_id}.md"

    # Rendered file lives in `dest`, so resolving there is up to date …
    is_stale, reason = cache.is_transcript_stale(
        session_name, session_id, output_dir=dest
    )
    assert not is_stale, reason
    assert reason == "up_to_date"

    # … but resolving against the source project dir (the legacy default)
    # can't find it — the exact bug that made every --output run re-render.
    is_stale_src, reason_src = cache.is_transcript_stale(session_name, session_id)
    assert is_stale_src
    assert reason_src == "file_missing"


def test_json_output_keeps_incremental_skip(tmp_path: Path):
    """JSON must keep its per-session incremental skip on a quiescent rerun.

    JSON carries its freshness in a top-level ``version`` field, not the
    ``<!-- Generated by claude-code-log v… -->`` comment that
    ``is_transcript_stale`` sniffs. Routing JSON through the marker-based
    cache path (dropping the format gate) reported every JSON file as
    "outdated" and re-rendered it on every run — this pins the gate that
    keeps JSON on its ``JsonRenderer.is_outdated`` fallback.
    """
    session_id = "json-1"
    project_dir = _make_project(tmp_path, session_id)

    def run() -> RegenerationReport:
        report = RegenerationReport()
        convert_jsonl_to(
            "json",
            project_dir,
            generate_individual_sessions=True,
            silent=True,
            report=report,
        )
        return report

    first = run()
    assert first.sessions_regenerated > 0
    assert (project_dir / f"session-{session_id}.json").exists()

    # Second, unchanged run must not re-render the session file.
    second = run()
    assert second.sessions_regenerated == 0, (
        "JSON must keep the renderer-based (version-field) incremental skip; "
        "routing it through the HTML version-comment sniff re-renders forever"
    )

    # And JSON does not occupy a marker-keyed html_cache row (which would
    # always read stale) — it tracks freshness via its own version field.
    cache = CacheManager(project_dir, get_library_version())
    assert cache.get_html_cache(f"session-{session_id}.json") is None


def test_legacy_html_inplace_behavior_unchanged(tmp_path: Path):
    """Default HTML, in-place layout (no output_root, no variant) still
    skips a quiescent second run — the fix must not disturb it."""
    session_id = "html-1"
    project_dir = _make_project(tmp_path, session_id)

    def run() -> RegenerationReport:
        report = RegenerationReport()
        convert_jsonl_to(
            "html",
            project_dir,
            generate_individual_sessions=True,
            silent=True,
            report=report,
        )
        return report

    first = run()
    assert first.sessions_regenerated > 0
    assert first.combined_regenerated
    # Default HTML writes in place, in the source project dir.
    assert (project_dir / f"session-{session_id}.html").exists()
    assert (project_dir / "combined_transcripts.html").exists()

    second = run()
    assert second.sessions_regenerated == 0
    assert not second.combined_regenerated
