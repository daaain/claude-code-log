"""Tests for per-level output file naming and variant-aware caching.

Covers:

- `variant_suffix()` matrix across (detail, compact, format).
- `VARIANT_ENTRY_RE` regex acceptance/rejection.
- `_get_page_html_path(n, suffix)` composition with pagination.
- Converter integration: combined / session / --session-id paths all
  land at the variant-encoded filename; explicit `-o` honours the
  user's literal path.
- Cache coexistence: full and low variants cache independently; a
  second render of the same variant is a cache hit; variants do not
  delete each other's page files.
- Session → combined back-link points to the same variant.
- `_enumerate_project_variants` lists all entry points present.
- CLI --compact help text regression.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_code_log.cache import CacheManager
from claude_code_log.cli import main as cli_main
from claude_code_log.converter import (
    _enumerate_project_variants,
    _get_page_html_path,
    convert_jsonl_to,
    generate_single_session_file,
    process_projects_hierarchy,
)
from claude_code_log.models import DetailLevel
from claude_code_log.utils import VARIANT_ENTRY_RE, variant_suffix


# ---------------------------------------------------------------------------
# variant_suffix() matrix
# ---------------------------------------------------------------------------


class TestVariantSuffix:
    def test_default_is_empty(self) -> None:
        assert variant_suffix(DetailLevel.FULL, False, "html") == ""
        assert variant_suffix(DetailLevel.FULL, False, "md") == ""

    def test_detail_only(self) -> None:
        assert variant_suffix(DetailLevel.HIGH, False, "html") == ".high"
        assert variant_suffix(DetailLevel.LOW, False, "html") == ".low"
        assert variant_suffix(DetailLevel.MINIMAL, False, "md") == ".minimal"
        assert variant_suffix(DetailLevel.USER_ONLY, False, "html") == ".user-only"
        assert variant_suffix(DetailLevel.USER_ONLY, False, "md") == ".user-only"

    def test_compact_markdown_only(self) -> None:
        # Compact contributes for Markdown output.
        assert variant_suffix(DetailLevel.FULL, True, "md") == ".compact"
        assert variant_suffix(DetailLevel.FULL, True, "markdown") == ".compact"
        assert variant_suffix(DetailLevel.LOW, True, "md") == ".low.compact"
        # HTML silently drops the compact component.
        assert variant_suffix(DetailLevel.FULL, True, "html") == ""
        assert variant_suffix(DetailLevel.LOW, True, "html") == ".low"

    def test_string_detail_accepted(self) -> None:
        # The CLI passes the already-normalised enum, but convenience callers
        # may pass the string form.
        assert variant_suffix("low", False, "html") == ".low"


# ---------------------------------------------------------------------------
# VARIANT_ENTRY_RE
# ---------------------------------------------------------------------------


class TestVariantEntryRegex:
    @pytest.mark.parametrize(
        "name,expected_suffix",
        [
            ("combined_transcripts.html", ""),
            ("combined_transcripts.low.html", ".low"),
            ("combined_transcripts.high.html", ".high"),
            ("combined_transcripts.low.compact.html", ".low.compact"),
            ("combined_transcripts.minimal.html", ".minimal"),
            ("combined_transcripts.user-only.html", ".user-only"),
            ("combined_transcripts.user-only.compact.html", ".user-only.compact"),
        ],
    )
    def test_accepts_entry_points(self, name: str, expected_suffix: str) -> None:
        m = VARIANT_ENTRY_RE.match(name)
        assert m is not None, name
        assert m.group(1) == expected_suffix

    @pytest.mark.parametrize(
        "name",
        [
            "combined_transcripts_2.html",
            "combined_transcripts.low_2.html",
            "combined_transcripts..html",
            "other_file.html",
            "combined_transcripts.md",
        ],
    )
    def test_rejects_non_entry_points(self, name: str) -> None:
        assert VARIANT_ENTRY_RE.match(name) is None, name


# ---------------------------------------------------------------------------
# _get_page_html_path
# ---------------------------------------------------------------------------


class TestPageHtmlPath:
    def test_default_page_one(self) -> None:
        assert _get_page_html_path(1) == "combined_transcripts.html"

    def test_default_page_two(self) -> None:
        assert _get_page_html_path(2) == "combined_transcripts_2.html"

    def test_variant_page_one(self) -> None:
        assert _get_page_html_path(1, ".low") == "combined_transcripts.low.html"

    def test_variant_page_two(self) -> None:
        assert _get_page_html_path(2, ".low") == "combined_transcripts.low_2.html"

    def test_variant_with_compact_chain(self) -> None:
        assert (
            _get_page_html_path(3, ".low.compact")
            == "combined_transcripts.low.compact_3.html"
        )


# ---------------------------------------------------------------------------
# Fixtures for converter integration
# ---------------------------------------------------------------------------


def _make_user(
    uuid: str, session_id: str, ts: str, text: str, parent: str | None = None
) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_assistant(uuid: str, session_id: str, ts: str, parent: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "requestId": f"req_{uuid}",
        "message": {
            "id": uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": "reply"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _write_session(path: Path, session_id: str, num_messages: int = 4) -> None:
    """Write a small single-session JSONL file at `path`."""
    entries: list[dict] = []
    prev: str | None = None
    for i in range(num_messages):
        uid = f"{session_id}_{i:03d}"
        if i % 2 == 0:
            entries.append(
                _make_user(
                    uid,
                    session_id,
                    f"2026-01-01T10:{i:02d}:00Z",
                    f"hi {i}",
                    parent=prev,
                )
            )
        else:
            entries.append(
                _make_assistant(
                    uid, session_id, f"2026-01-01T10:{i:02d}:00Z", prev or ""
                )
            )
        prev = uid
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Converter integration: default path, variant paths, explicit -o
# ---------------------------------------------------------------------------


class TestConverterVariantPaths:
    def test_default_variant_uses_bare_filename(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        output_path = convert_jsonl_to("html", tmp_path, silent=True)
        assert output_path.name == "combined_transcripts.html"
        assert output_path.exists()

    def test_low_variant_encodes_suffix(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        output_path = convert_jsonl_to(
            "html", tmp_path, silent=True, detail=DetailLevel.LOW
        )
        assert output_path.name == "combined_transcripts.low.html"
        assert output_path.exists()

    def test_low_and_full_coexist(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        full = convert_jsonl_to("html", tmp_path, silent=True)
        low = convert_jsonl_to("html", tmp_path, silent=True, detail=DetailLevel.LOW)
        assert full.name == "combined_transcripts.html"
        assert low.name == "combined_transcripts.low.html"
        assert full.exists() and low.exists()
        assert full != low

    def test_md_compact_variant(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        path = convert_jsonl_to(
            "md",
            tmp_path,
            silent=True,
            detail=DetailLevel.LOW,
            compact=True,
        )
        assert path.name == "combined_transcripts.low.compact.md"
        assert path.exists()

    def test_individual_session_files_pick_up_suffix(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        convert_jsonl_to(
            "html",
            tmp_path,
            silent=True,
            detail=DetailLevel.LOW,
        )
        assert (tmp_path / "session-sess1.low.html").exists()
        assert not (tmp_path / "session-sess1.html").exists()

    def test_explicit_output_path_honoured(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        explicit = tmp_path / "custom.html"
        result = convert_jsonl_to(
            "html",
            tmp_path,
            explicit,
            silent=True,
            detail=DetailLevel.LOW,
        )
        # User's literal path wins: no suffix appended.
        assert result == explicit
        assert explicit.exists()

    def test_single_session_export_variant(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        # Build cache first.
        convert_jsonl_to("html", tmp_path, silent=True)
        out = generate_single_session_file(
            "html",
            tmp_path,
            "sess1",
            use_cache=True,
            detail=DetailLevel.LOW,
        )
        assert out.name == "session-sess1.low.html"

    def test_single_file_input_variant_suffix(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "sess1.jsonl"
        _write_session(jsonl, "sess1")
        out = convert_jsonl_to(
            "html",
            jsonl,
            silent=True,
            detail=DetailLevel.HIGH,
        )
        assert out.name == "sess1.high.html"
        assert out.exists()


# ---------------------------------------------------------------------------
# Cache coexistence
# ---------------------------------------------------------------------------


class TestCacheVariantCoexistence:
    def test_variant_cache_buckets_are_independent(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        # First: render FULL, populates cache for default variant.
        full1 = convert_jsonl_to("html", tmp_path, silent=True)
        # `st_mtime_ns` has nanosecond resolution on platforms that support
        # it (ext4/APFS/NTFS), avoiding the 1s/2s granularity wobble on
        # FAT32 or older HFS+. Pair with inode for extra robustness —
        # a rewrite-in-place on some filesystems preserves the inode, but
        # a delete+recreate flips it.
        full_stat = full1.stat()
        full_mtime_ns = full_stat.st_mtime_ns
        full_ino = full_stat.st_ino

        # Render LOW — must NOT touch the FULL file's cache row.
        low = convert_jsonl_to("html", tmp_path, silent=True, detail=DetailLevel.LOW)
        assert low.exists()
        assert full1.exists()

        # Second FULL render must hit cache (file untouched).
        full2 = convert_jsonl_to("html", tmp_path, silent=True)
        full2_stat = full2.stat()
        assert (
            full2_stat.st_mtime_ns == full_mtime_ns and full2_stat.st_ino == full_ino
        ), "Second FULL render should be a cache hit — file was rewritten"

    def test_low_render_does_not_delete_full_pages(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        full = convert_jsonl_to("html", tmp_path, silent=True)
        low = convert_jsonl_to("html", tmp_path, silent=True, detail=DetailLevel.LOW)
        # Both exist and are distinct files.
        assert full.exists() and low.exists() and full != low


# ---------------------------------------------------------------------------
# Session → combined back-link
# ---------------------------------------------------------------------------


class TestSessionBackLink:
    def test_low_session_links_to_low_combined(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        convert_jsonl_to(
            "html",
            tmp_path,
            silent=True,
            detail=DetailLevel.LOW,
        )
        session_file = tmp_path / "session-sess1.low.html"
        # Explicit UTF-8: the HTML contains emoji glyphs (🤷, 🤖, 📦);
        # Python on Windows otherwise defaults to cp1252 and crashes.
        html = session_file.read_text(encoding="utf-8")
        # Should link to the LOW combined file, not the bare default.
        assert "combined_transcripts.low.html" in html
        # The bare default may still occur as text elsewhere; ensure it is
        # not the back-link target on its own.
        assert 'href="combined_transcripts.html"' not in html


# ---------------------------------------------------------------------------
# _enumerate_project_variants
# ---------------------------------------------------------------------------


class TestEnumerateProjectVariants:
    def test_lists_all_variants_default_first(self, tmp_path: Path) -> None:
        # Create dummy variant files — include `user-only` with its hyphen
        # to guard against a regex regression that drops hyphenated variants.
        (tmp_path / "combined_transcripts.html").write_text("x")
        (tmp_path / "combined_transcripts.low.html").write_text("x")
        (tmp_path / "combined_transcripts.high.html").write_text("x")
        (tmp_path / "combined_transcripts.user-only.html").write_text("x")
        # Paginated trailers should be ignored.
        (tmp_path / "combined_transcripts_2.html").write_text("x")
        (tmp_path / "combined_transcripts.low_2.html").write_text("x")
        (tmp_path / "combined_transcripts.user-only_2.html").write_text("x")

        variants = _enumerate_project_variants(tmp_path, "project")
        suffixes = [v["suffix"] for v in variants]
        labels = [v["label"] for v in variants]
        # Default first.
        assert suffixes[0] == ""
        assert labels[0] == "Full"
        # All four entries present, no paginated trailers.
        assert sorted(suffixes) == ["", ".high", ".low", ".user-only"]
        # Hyphenated label renders with a space (UI polish).
        user_only_label = next(
            v["label"] for v in variants if v["suffix"] == ".user-only"
        )
        assert user_only_label == "User only"

    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert _enumerate_project_variants(tmp_path, "empty") == []


# ---------------------------------------------------------------------------
# CLI --compact help text regression
# ---------------------------------------------------------------------------


class TestCliHelpText:
    def test_compact_help_notes_markdown_only(self) -> None:
        # `CliRunner` invokes the click entry point in-process — no
        # dependency on `uv` or an installed `claude-code-log` console
        # script, so the test stays hermetic across CI matrices, editable
        # dev shells, and plain pip/venv setups.
        result = CliRunner().invoke(cli_main, ["--help"])
        assert result.exit_code == 0
        # Flatten whitespace since click wraps help lines.
        flat = re.sub(r"\s+", " ", result.output)
        assert "--compact" in flat
        assert "Markdown-only" in flat, (
            f"Expected 'Markdown-only' note in --compact help; got:\n{flat}"
        )


# ---------------------------------------------------------------------------
# Cache API: variant-aware pagination (low-level)
# ---------------------------------------------------------------------------


class TestPaginationCacheVariantApi:
    def test_page_cache_rows_are_variant_scoped(self, tmp_path: Path) -> None:
        _write_session(tmp_path / "sess1.jsonl", "sess1")
        # Trigger an initial render so the cache manager is primed.
        convert_jsonl_to("html", tmp_path, silent=True)

        cache = CacheManager(tmp_path, "0.0.1")

        # Manually insert a page row at the default variant and at .low.
        cache.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=2000,
            session_ids=["sess1"],
            message_count=10,
            first_timestamp="2026-01-01T10:00:00Z",
            last_timestamp="2026-01-01T10:10:00Z",
            total_input_tokens=0,
            total_output_tokens=0,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        cache.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.low.html",
            page_size_config=2000,
            session_ids=["sess1"],
            message_count=5,
            first_timestamp="2026-01-01T10:00:00Z",
            last_timestamp="2026-01-01T10:10:00Z",
            total_input_tokens=0,
            total_output_tokens=0,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
            variant_suffix=".low",
        )

        default = cache.get_page_data(1)
        low = cache.get_page_data(1, ".low")
        assert default is not None and low is not None
        assert default.html_path == "combined_transcripts.html"
        assert low.html_path == "combined_transcripts.low.html"
        # The rows are independent — different message_count.
        assert default.message_count != low.message_count
        # Page counts are variant-scoped.
        assert cache.get_page_count() == 1
        assert cache.get_page_count(".low") == 1


# ---------------------------------------------------------------------------
# Migration 004 upgrade path — preserve page_sessions across the
# table-recreate (PRAGMA foreign_keys toggle)
# ---------------------------------------------------------------------------


class TestMigration004UpgradePath:
    def test_page_sessions_survive_table_recreate(self, tmp_path: Path) -> None:
        """Pre-populating html_pages + page_sessions rows before applying
        migration 004 must NOT lose any page_sessions on upgrade. Without
        `PRAGMA foreign_keys = OFF`, the DROP TABLE html_pages cascades
        and wipes every page_sessions row."""
        import sqlite3
        from claude_code_log.migrations.runner import (
            apply_migration,
            _ensure_schema_version_table,
        )

        db = tmp_path / "cache.db"
        conn = sqlite3.connect(str(db), timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            _ensure_schema_version_table(conn)
            migrations_dir = (
                Path(__file__).parent.parent / "claude_code_log" / "migrations"
            )
            # Apply 001, 002, 003 to build the pre-upgrade schema.
            for i in (1, 2, 3):
                mig_file = next(migrations_dir.glob(f"{i:03d}_*.sql"))
                apply_migration(conn, i, mig_file)

            # Seed a minimal paginated state: one project, one page, one
            # page_sessions row.
            conn.execute(
                "INSERT INTO projects (project_path, version, cache_created, last_updated) "
                "VALUES (?, '0.0.1', datetime('now'), datetime('now'))",
                (str(tmp_path / "proj"),),
            )
            project_id = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
            conn.execute(
                "INSERT INTO html_pages "
                "(project_id, page_number, html_path, page_size_config, message_count, "
                " first_session_id, last_session_id, generated_at, library_version) "
                "VALUES (?, 1, 'combined_transcripts.html', 2000, 10, 's1', 's1', "
                " datetime('now'), '0.0.1')",
                (project_id,),
            )
            page_id = conn.execute("SELECT id FROM html_pages LIMIT 1").fetchone()[0]
            conn.execute(
                "INSERT INTO page_sessions (page_id, session_id, session_order) "
                "VALUES (?, 's1', 0)",
                (page_id,),
            )
            conn.commit()

            before_pages = conn.execute("SELECT COUNT(*) FROM html_pages").fetchone()[0]
            before_page_sessions = conn.execute(
                "SELECT COUNT(*) FROM page_sessions"
            ).fetchone()[0]
            assert before_pages == 1
            assert before_page_sessions == 1

            # Apply migration 004 — the blocker was that this silently
            # cascade-deleted every page_sessions row.
            mig_004 = next(migrations_dir.glob("004_*.sql"))
            apply_migration(conn, 4, mig_004)

            after_pages = conn.execute("SELECT COUNT(*) FROM html_pages").fetchone()[0]
            after_page_sessions = conn.execute(
                "SELECT COUNT(*) FROM page_sessions"
            ).fetchone()[0]

            assert after_pages == 1, "html_pages row must survive the table recreate"
            assert after_page_sessions == 1, (
                f"page_sessions row must survive migration 004 "
                f"(was {before_page_sessions}, now {after_page_sessions}) — "
                "PRAGMA foreign_keys toggle missing?"
            )

            # Confirm the new variant_suffix column is present and the
            # existing row has the default '' variant.
            row = conn.execute(
                "SELECT variant_suffix FROM html_pages LIMIT 1"
            ).fetchone()
            assert row[0] == ""
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Paginated × variant cache coexistence (converter integration)
# ---------------------------------------------------------------------------


class TestPaginatedVariantCoexistence:
    def test_full_and_low_pages_coexist_and_cache_hits(self, tmp_path: Path) -> None:
        """Force pagination with a tiny page_size, render FULL → LOW →
        FULL again. All page files for both variants must coexist; the
        second FULL render must be a cache hit; LOW must not delete any
        FULL page file."""
        # Two sessions of 6 messages each, `page_size=2` → pagination
        # is deterministic: `_assign_sessions_to_pages` sorts by
        # `first_timestamp` (Python's stable sort) and packs one session
        # per page while honouring the size cap, so we reliably get at
        # least `combined_transcripts.html` + `_2.html`.
        _write_session(tmp_path / "sessA.jsonl", "sessA", num_messages=6)
        _write_session(tmp_path / "sessB.jsonl", "sessB", num_messages=6)

        full_page1 = convert_jsonl_to("html", tmp_path, silent=True, page_size=2)
        assert full_page1.name == "combined_transcripts.html"
        full_page2 = tmp_path / "combined_transcripts_2.html"
        assert full_page2.exists(), (
            f"Expected pagination to produce page 2; "
            f"dir contents: {sorted(p.name for p in tmp_path.glob('*.html'))}"
        )
        # `st_mtime_ns` + inode is robust across filesystems: mtime
        # alone has 1s/2s granularity on FAT32 and older HFS+, so a
        # rewrite-within-same-second would be invisible to `st_mtime`.
        # `_ns` gives nanosecond resolution on ext4/APFS/NTFS; inode
        # shifts under delete+recreate even when mtime happens to match.
        full_page1_stat = full_page1.stat()
        full_page2_stat = full_page2.stat()
        full_page1_sig = (full_page1_stat.st_mtime_ns, full_page1_stat.st_ino)
        full_page2_sig = (full_page2_stat.st_mtime_ns, full_page2_stat.st_ino)

        # Render LOW — must produce its own variant files and leave
        # FULL's alone.
        low_page1 = convert_jsonl_to(
            "html",
            tmp_path,
            silent=True,
            page_size=2,
            detail=DetailLevel.LOW,
        )
        assert low_page1.name == "combined_transcripts.low.html"
        low_page2 = tmp_path / "combined_transcripts.low_2.html"
        assert low_page2.exists()
        # FULL's page files must be untouched.
        assert full_page1.exists()
        assert full_page2.exists()
        fp1 = full_page1.stat()
        fp2 = full_page2.stat()
        assert (fp1.st_mtime_ns, fp1.st_ino) == full_page1_sig
        assert (fp2.st_mtime_ns, fp2.st_ino) == full_page2_sig

        # Second FULL render must hit the cache — no page files rewritten.
        full_page1_again = convert_jsonl_to("html", tmp_path, silent=True, page_size=2)
        assert full_page1_again == full_page1
        fp1_again = full_page1.stat()
        fp2_again = full_page2.stat()
        assert (fp1_again.st_mtime_ns, fp1_again.st_ino) == full_page1_sig, (
            "Second FULL render should have been a cache hit (page 1)"
        )
        assert (fp2_again.st_mtime_ns, fp2_again.st_ino) == full_page2_sig, (
            "Second FULL render should have been a cache hit (page 2)"
        )


# ---------------------------------------------------------------------------
# Projects index regeneration when variant flags toggle
# ---------------------------------------------------------------------------


def _build_one_project_projects_dir(root: Path, encoded: str = "-p") -> Path:
    """Minimal `~/.claude/projects/` shape with one project + one session.

    Mirrors `test_obsidian_output._build_fake_projects_dir` but inlined
    to avoid a cross-module fixture dependency."""
    projects_dir = root / "projects"
    projects_dir.mkdir()
    proj = projects_dir / encoded
    proj.mkdir()
    entry = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/joe/p",
        "sessionId": "sess1",
        "version": "2.1.0",
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-05-10T10:00:00.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    (proj / "sess1.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return projects_dir


class TestProjectsIndexVariantRefresh:
    """Regression: toggling a Markdown variant flag (`--compact`,
    `--no-timestamps`, `--detail`) between runs produces new per-project
    output filenames (e.g. `combined_transcripts.compact.md`). The
    projects `index.md` must regenerate on that re-run so its links
    point at the variant-suffixed filenames — not the stale unsuffixed
    ones from the previous run.

    Pre-fix, the index was gated on a "source JSONL changed" flag, so
    flag-only re-runs (forward or backward through the variant matrix)
    left the index stale even when per-project files were rewritten
    or the user re-targeted a pre-existing variant. Always regenerating
    the index closes both directions; the cost is one template pass +
    file write over already-aggregated `project_summaries`."""

    def test_compact_toggle_refreshes_index_link(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Isolate the cache so we don't poison the user's real one and
        # so the run is fully deterministic.
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

        projects_dir = _build_one_project_projects_dir(tmp_path)
        index_path = projects_dir / "index.md"

        # First run: default (no variant flag).
        process_projects_hierarchy(projects_dir, output_format="md")
        assert (projects_dir / "-p" / "combined_transcripts.md").exists()
        assert index_path.exists()
        first_index = index_path.read_text(encoding="utf-8")
        assert "combined_transcripts.md" in first_index
        assert "combined_transcripts.compact.md" not in first_index

        # Second run: same sources, `--compact` toggled on. This is the
        # regression scenario — no source files changed, so the legacy
        # `any_cache_updated` flag stays False, but a new variant file
        # is produced and the index must follow.
        process_projects_hierarchy(projects_dir, output_format="md", compact=True)
        assert (projects_dir / "-p" / "combined_transcripts.compact.md").exists()
        refreshed_index = index_path.read_text(encoding="utf-8")
        assert "combined_transcripts.compact.md" in refreshed_index, (
            "Index should have been regenerated to point at the .compact "
            "variant; it still references the bare filename."
        )

    def test_expand_paths_combined_no_refreshes_index_link(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--expand-paths` mode uses `--combined no` (per-session
        files only, no `combined_transcripts*`). Toggling `--compact`
        must still refresh the index so the per-session bullet links
        carry the `.compact` suffix."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

        projects_dir = _build_one_project_projects_dir(tmp_path)
        out = tmp_path / "out"
        index_path = out / "index.md"

        process_projects_hierarchy(
            projects_dir,
            output_format="md",
            output_dir=out,
            expand_paths=True,
            write_combined=False,
        )
        assert index_path.exists()
        first_index = index_path.read_text(encoding="utf-8")
        # Per-session bullet should point at the bare session filename.
        assert "session-sess1.md" in first_index
        assert "session-sess1.compact.md" not in first_index

        process_projects_hierarchy(
            projects_dir,
            output_format="md",
            output_dir=out,
            expand_paths=True,
            write_combined=False,
            compact=True,
        )
        # Per-session variant file must exist on disk.
        assert (out / "home" / "joe" / "p" / "session-sess1.compact.md").exists()
        refreshed_index = index_path.read_text(encoding="utf-8")
        assert "session-sess1.compact.md" in refreshed_index, (
            "Index should refresh per-session links to the .compact "
            "variant when `--combined no` is in effect."
        )

    def test_toggle_back_to_default_refreshes_index_link(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symmetric case: after running with `--compact`, re-running
        WITHOUT the flag must point the index back at the unsuffixed
        filename — even though the bare file already exists from an
        earlier run (no source/cache changes, no slow-path entry for
        the project).

        Pre-"always regenerate" fix this stayed stale: the slow path
        wasn't entered for the project, so the dirty flag never
        flipped, and the index kept `.compact` links."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

        projects_dir = _build_one_project_projects_dir(tmp_path)
        index_path = projects_dir / "index.md"

        # Seed both variants on disk.
        process_projects_hierarchy(projects_dir, output_format="md")
        process_projects_hierarchy(projects_dir, output_format="md", compact=True)
        # Index now reflects the most recent (compact) run.
        assert "combined_transcripts.compact.md" in index_path.read_text(
            encoding="utf-8"
        )

        # Re-run default. No source change, the bare combined file
        # already exists → the per-project loop takes the fast path.
        # Index must still flip back to the unsuffixed link.
        process_projects_hierarchy(projects_dir, output_format="md")
        final_index = index_path.read_text(encoding="utf-8")
        assert "combined_transcripts.compact.md" not in final_index, (
            "Index should no longer reference the .compact variant "
            "after toggling back to the default."
        )
        assert "combined_transcripts.md" in final_index
