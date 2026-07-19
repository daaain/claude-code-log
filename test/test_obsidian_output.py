"""End-to-end tests for the Obsidian-friendly output flags (issue #151).

Drives the converter through ``process_projects_hierarchy`` with each
flag combination from the matrix and asserts the produced directory
tree. **Markdown-scoped per Q1 resolution** — the flag mechanics live
in ``converter.py``/``utils.py``, not the renderers, so HTML/JSON parity
is asserted by code inspection rather than by re-running the matrix
per format.
"""

import json
import shutil
from pathlib import Path

import pytest

from claude_code_log.converter import process_projects_hierarchy

_REAL_PROJECTS_DIR = Path(__file__).parent / "test_data" / "real_projects"


def _build_fake_projects_dir(
    root: Path,
    projects: list[tuple[str, str]],
) -> Path:
    """Create a fake `~/.claude/projects/`-shaped directory.

    Args:
        root: tmp_path-style scratch directory.
        projects: list of (encoded_name, real_cwd) pairs.
    Returns:
        The projects-dir path.
    """
    projects_dir = root / "projects"
    projects_dir.mkdir()
    for encoded, cwd in projects:
        proj = projects_dir / encoded
        proj.mkdir()
        # Minimal session JSONL — enough for the loader to find one
        # session and produce one combined transcript.
        entry = {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": cwd,
            "sessionId": f"session-{encoded.lstrip('-')[:32]}",
            "version": "2.1.0",
            "type": "user",
            "uuid": f"uuid-{encoded.lstrip('-')[:32]}",
            "timestamp": "2026-05-10T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"hi from {encoded}"}],
            },
        }
        (proj / "session.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return projects_dir


@pytest.fixture
def fake_projects(tmp_path: Path) -> Path:
    """Three encoded projects with realistic absolute cwds (which is
    what the JSONL-peek tier of `project_dir_to_real_path` will pick up).
    """
    return _build_fake_projects_dir(
        tmp_path,
        projects=[
            ("-home-joe-project-A", "/home/joe/project/A"),
            ("-home-joe-project-B", "/home/joe/project/B"),
            ("-home-jane-project-C", "/home/jane/project/C"),
        ],
    )


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Steer the cache to tmp so the test doesn't pollute / depend on
    the user's real `~/.claude/projects/` cache."""
    cache_path = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(cache_path))
    return cache_path


# Keep usage explicit so the fixture clearly applies even when its
# return value isn't read directly in the test body.
_ = isolated_cache


class TestObsidianOutputMatrix:
    """The matrix from work/obsidian-friendly-output.md, end-to-end.
    Each test asserts the produced directory shape under the relevant
    flag combination."""

    def test_legacy_no_output(self, fake_projects: Path, isolated_cache: Path):
        """Legacy: `--output` unset → outputs land inside each
        source project_dir under the projects tree (current behaviour
        from before #151)."""
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
        )

        # Each project gets a combined_transcripts.md under its source.
        for encoded in [
            "-home-joe-project-A",
            "-home-joe-project-B",
            "-home-jane-project-C",
        ]:
            assert (fake_projects / encoded / "combined_transcripts.md").exists()
        # Index at the projects-dir root.
        assert (fake_projects / "index.md").exists()

    def test_output_only_flat_copy(
        self,
        fake_projects: Path,
        isolated_cache: Path,
        tmp_path: Path,
    ):
        """`--output` alone → flat copy of each project under
        <output>/<encoded>/. Closes the implicit gap (`--output` was
        previously silently ignored for `--all-projects`)."""
        out = tmp_path / "out-flat"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
        )
        assert (out / "-home-joe-project-A" / "combined_transcripts.md").exists()
        assert (out / "-home-joe-project-B" / "combined_transcripts.md").exists()
        assert (out / "-home-jane-project-C" / "combined_transcripts.md").exists()
        assert (out / "index.md").exists()

    def test_expand_paths_full_tree(
        self,
        fake_projects: Path,
        isolated_cache: Path,
        tmp_path: Path,
    ):
        """`--output --expand-paths` → expanded real-path tree under
        <output>/. Encoded names are resolved via JSONL peek (the
        fixture's cwd field)."""
        out = tmp_path / "out-expanded"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            expand_paths=True,
        )
        assert (out / "home/joe/project/A/combined_transcripts.md").exists()
        assert (out / "home/joe/project/B/combined_transcripts.md").exists()
        assert (out / "home/jane/project/C/combined_transcripts.md").exists()
        assert (out / "index.md").exists()
        # The encoded-name flat directories must NOT exist — we
        # expanded, didn't both expand and copy.
        assert not (out / "-home-joe-project-A").exists()

    def test_expand_paths_filter_match_truncates(
        self,
        fake_projects: Path,
        isolated_cache: Path,
        tmp_path: Path,
    ):
        """`--filter-path /home/joe --expand-paths`: filter against
        real path; truncate the prefix; matching projects land at
        <output>/<rel-to-prefix>/."""
        out = tmp_path / "out-filtered"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            expand_paths=True,
            filter_path="/home/joe",
        )
        # Projects under /home/joe matched, prefix truncated.
        assert (out / "project/A/combined_transcripts.md").exists()
        assert (out / "project/B/combined_transcripts.md").exists()
        # Project under /home/jane filtered out — no output produced.
        assert not (out / "project/C").exists()
        assert not (out / "home").exists()  # would only exist if /home/joe survived
        assert (out / "index.md").exists()

    def test_filter_flat_no_expand(
        self,
        fake_projects: Path,
        isolated_cache: Path,
        tmp_path: Path,
    ):
        """`--filter-path -home-joe`without `--expand-paths`: filter
        against the encoded dir name; no truncation; matching
        projects land at <output>/<encoded>/."""
        out = tmp_path / "out-flat-filtered"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            expand_paths=False,
            filter_path="-home-joe",
        )
        # Two `-home-joe-...` projects matched; flat name preserved.
        assert (out / "-home-joe-project-A" / "combined_transcripts.md").exists()
        assert (out / "-home-joe-project-B" / "combined_transcripts.md").exists()
        # `-home-jane-...` doesn't start with `-home-joe`.
        assert not (out / "-home-jane-project-C").exists()


# -----------------------------------------------------------------------------
# CLI validation guards (#151 footgun fixes from monk's review)
# -----------------------------------------------------------------------------


class TestCliValidationGuards:
    """The CLI rejects relative `--filter-path` when paired with
    `--expand-paths` (would otherwise silently exclude every project),
    and warns when the new flags are passed in no-op contexts."""

    def test_relative_filter_path_with_expand_is_rejected(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """Loud rejection — without this, `--filter-path home/joe`
        (forgetting the leading `/`) would match no projects silently
        because `Path.relative_to` raises for relative paths."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--filter-path",
                "home/joe",  # relative — should be rejected
            ],
        )
        assert result.exit_code != 0
        assert "must be an absolute path" in result.output
        # No projects rendered.
        assert not out.exists() or not any(
            (out / p).exists() for p in ["home", "project", "-home-joe-project-A"]
        )

    def test_absolute_filter_path_with_expand_is_accepted(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """Counterpart: absolute `--filter-path` passes the guard."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--filter-path",
                "/home/joe",
                "--format",
                "md",
            ],
        )
        assert result.exit_code == 0, result.output
        # `--expand-paths` defaults `--combined` to `no` (Obsidian
        # mode), so the combined file is suppressed; check for the
        # per-session output instead.
        sessions = list((out / "project/A").glob("session-*.md"))
        assert sessions, "expected per-session output under the expanded tree"

    def test_warns_when_flags_used_without_all_projects(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """`--expand-paths` against a single-file/single-project
        target (without `--all-projects`) is a no-op; user gets a
        warning rather than silent ignore."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        # Point at a single project_dir with --expand-paths but no
        # --all-projects (and explicitly no `output` to make the
        # control flow predictable). Should warn.
        single_project = fake_projects / "-home-joe-project-A"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(single_project),
                "--expand-paths",
                "--format",
                "md",
            ],
        )
        # The exact stderr-output ordering is implementation-dependent,
        # but the warning text must surface somewhere — and the
        # invocation must still succeed (warning, not error).
        assert result.exit_code == 0, result.output
        assert "require --all-projects" in result.output

    def test_warns_when_expand_paths_with_file_output(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """`--output some-file.md --expand-paths` is a no-op (file
        output goes through the single-file path); warn."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(tmp_path / "out.md"),  # file-suffixed
                "--expand-paths",
                "--format",
                "md",
            ],
        )
        # Warning, not error — single-file path still runs successfully.
        assert result.exit_code == 0, result.output
        assert "require --output to be a directory" in result.output


# -----------------------------------------------------------------------------
# --combined yes/no/only flag (#151 follow-up)
# -----------------------------------------------------------------------------


class TestCombinedFlag:
    """The `--combined` flag controls whether the combined-transcript
    and per-session files are emitted. Default is `yes` except when
    `--expand-paths` is set, in which case it switches to `no`
    (Obsidian-vault-friendly default — combined is dead weight when
    each session has its own .md file)."""

    def test_combined_yes_emits_both(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        out = tmp_path / "out-both"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            write_combined=True,
            generate_individual_sessions=True,
        )
        assert (out / "-home-joe-project-A" / "combined_transcripts.md").exists()
        # Per-session file too. Filename is session-{session_id}.md.
        sessions = list((out / "-home-joe-project-A").glob("session-*.md"))
        assert sessions, "expected at least one per-session file"

    def test_combined_no_skips_combined(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        out = tmp_path / "out-none"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            write_combined=False,
            generate_individual_sessions=True,
        )
        # Combined file MUST NOT exist.
        assert not (out / "-home-joe-project-A" / "combined_transcripts.md").exists()
        # Per-session files SHOULD exist.
        sessions = list((out / "-home-joe-project-A").glob("session-*.md"))
        assert sessions

    def test_combined_only_skips_per_session(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        out = tmp_path / "out-only"
        process_projects_hierarchy(
            fake_projects,
            output_format="md",
            output_dir=out,
            write_combined=True,
            generate_individual_sessions=False,
        )
        assert (out / "-home-joe-project-A" / "combined_transcripts.md").exists()
        # Per-session files SHOULD NOT exist.
        sessions = list((out / "-home-joe-project-A").glob("session-*.md"))
        assert not sessions, "per-session files leaked through --combined only"

    def test_cli_expand_paths_default_is_combined_no(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """The default for `--combined` when `--expand-paths` is set
        should be `no` — Obsidian users want per-session files only."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-default"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--format",
                "md",
            ],
        )
        assert result.exit_code == 0, result.output
        # Combined files should NOT have been emitted under the
        # expanded tree.
        combined_files = list(out.rglob("combined_transcripts*.md"))
        assert not combined_files, (
            f"--combined no should be the default with --expand-paths, "
            f"but {len(combined_files)} combined files were written"
        )
        # Per-session files SHOULD be present.
        session_files = list(out.rglob("session-*.md"))
        assert session_files

    def test_cli_expand_paths_yields_bullet_tree_index(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """Markdown index under `--expand-paths` renders as a nested
        bullet-list directory tree (each path component a bullet,
        sessions as leaf bullets)."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-tree"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--format",
                "md",
            ],
        )
        assert result.exit_code == 0, result.output
        index_md = (out / "index.md").read_text(encoding="utf-8")
        # Directory bullets — bold, trailing slash.
        assert "- **home/**" in index_md
        assert "- **joe/**" in index_md
        # Leaf session links (markdown link syntax pointing into the
        # expanded tree).
        assert "(home/joe/project/A/session-" in index_md
        # The traditional flat `## [project](combined.md)` heading
        # shape must NOT appear in tree mode.
        assert "## [home/joe/project/A]" not in index_md

    def test_cli_expand_paths_yields_html_tree_index(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """HTML index under `--expand-paths` renders as a nested
        folder tree (`<ul class='project-tree'>` with `project-tree-dir`
        and `project-tree-leaf` items), and project-name links to the
        non-existent combined file are suppressed."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-html-tree"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                # HTML is the default format; left explicit for clarity.
                "--format",
                "html",
            ],
        )
        assert result.exit_code == 0, result.output
        index_html = (out / "index.html").read_text(encoding="utf-8")
        # Folder hierarchy markers — directory <li>s with bold names
        # carrying a trailing slash.
        assert "project-tree-dir" in index_html
        assert "<strong>home/</strong>" in index_html
        assert "<strong>joe/</strong>" in index_html
        # At least one leaf project card is wrapped in the tree.
        assert "project-tree-leaf" in index_html
        # Under `--combined no` (the default with `--expand-paths`),
        # the index must NOT render a hyperlink to combined files that
        # were never written, nor the "open combined transcript" hint.
        assert "(← open combined transcript)" not in index_html
        assert "combined_transcripts.html" not in index_html

    @pytest.mark.parametrize(
        "fmt,depth",
        [
            # Non-default --depth levels (the default `tool` gets no suffix,
            # so pick levels that DO earn one). The suffix == the --depth name.
            ("md", "agent"),
            ("md", "hook"),
            ("html", "agent"),
            ("html", "user"),
        ],
    )
    def test_index_session_links_carry_detail_variant_suffix(
        self,
        fmt: str,
        depth: str,
        fake_projects: Path,
        isolated_cache: Path,
        tmp_path: Path,
    ):
        """Under `--expand-paths --depth <non-default>`, the index session
        links MUST carry the `.{depth}.{ext}` infix that matches the
        on-disk filenames — otherwise every link in the bullet-tree /
        HTML folder tree 404s."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / f"out-depth-{fmt}-{depth}"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--format",
                fmt,
                "--depth",
                depth,
            ],
        )
        assert result.exit_code == 0, result.output

        ext = "md" if fmt == "md" else "html"
        index_path = out / f"index.{ext}"
        index_text = index_path.read_text(encoding="utf-8")

        # Walk every session file written on disk and assert the index
        # contains a link to its exact filename (relative path).
        session_files = list(out.rglob(f"session-*.{depth}.{ext}"))
        assert session_files, f"expected per-session files with .{depth}.{ext} suffix"
        for sf in session_files:
            rel = sf.relative_to(out).as_posix()
            # Confirm on-disk filename carries the depth infix.
            assert sf.name.endswith(f".{depth}.{ext}"), sf
            # And the index points at that same rel-path.
            assert rel in index_text, (
                f"index missing link to {rel!r}; "
                f"variant suffix .{depth} likely dropped from index URLs"
            )

    def test_index_excludes_synthetic_agent_sessions(
        self, isolated_cache: Path, tmp_path: Path
    ):
        """Agent sessions (`{sid}#agent-{aid}` synthetic IDs from
        `_integrate_agent_entries`) are inlined into the parent's
        transcript and skipped by `_generate_individual_session_files`
        — so they must NOT appear as standalone bullets in the index.
        Uses the real-projects fixture which contains `agent-*.jsonl`
        files that exercise the cache write path."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        if not _REAL_PROJECTS_DIR.exists():
            pytest.skip("real_projects test data not found")

        # Copy to a temp dir so we don't pollute the source tree with
        # generated cache.db / images.
        src_copy = tmp_path / "real_projects_copy"
        shutil.copytree(_REAL_PROJECTS_DIR, src_copy)

        out = tmp_path / "out-no-agents"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(src_copy),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--format",
                "md",
                "--detail",
                "low",
            ],
        )
        assert result.exit_code == 0, result.output
        index_md = (out / "index.md").read_text(encoding="utf-8")
        # No agent-session leaf bullets in the index — they're inlined
        # into the parent session's transcript by the renderer.
        assert "#agent-" not in index_md, (
            f"index.md contains {index_md.count('#agent-')} synthetic "
            f"agent-session links; they should be filtered by the "
            f"converter's per-session list builder"
        )

    def test_per_session_files_omit_combined_back_link_under_combined_no(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """Under `--combined no` (the implicit default with
        `--expand-paths`), per-session Markdown files must NOT carry
        a `[← Back to combined transcript](combined_transcripts.md)`
        line — the target was never written, so the link would 404."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-backlink"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--expand-paths",
                "--format",
                "md",
            ],
        )
        assert result.exit_code == 0, result.output
        session_files = list(out.rglob("session-*.md"))
        assert session_files, "expected per-session files under --combined no"
        for session_file in session_files:
            content = session_file.read_text(encoding="utf-8")
            assert "Back to combined transcript" not in content, (
                f"{session_file} still carries a back-link to the "
                f"(non-existent) combined transcript"
            )
            assert "combined_transcripts.md" not in content, (
                f"{session_file} still references combined_transcripts.md"
            )

    def test_bullet_tree_normalises_backslash_separators(self):
        """Regression: on Windows, `str(Path("home/joe"))` is
        `home\\joe`, so any leaked native-separator URL would land
        in the bullet-tree as a single un-split leaf line. The
        builder must fold backslashes to `/` before splitting."""
        from types import SimpleNamespace

        from claude_code_log.markdown.renderer import _render_expand_paths_tree

        project_backslash = SimpleNamespace(
            combined_suppressed=True,
            html_file="ignored.html",
            display_name="proj",
            formatted_time_range="2026-05-10 10:00:00",
            sessions=[
                {
                    "id": "abcdef1234",
                    "summary": "S1",
                    "timestamp_range": "2026-05-10 10:00:00",
                    "file": r"home\joe\project\B\session-abcdef1234.md",
                }
            ],
        )
        project_forward = SimpleNamespace(
            combined_suppressed=True,
            html_file="ignored.html",
            display_name="proj",
            formatted_time_range="2026-05-10 10:00:00",
            sessions=[
                {
                    "id": "deadbeef99",
                    "summary": "S2",
                    "timestamp_range": "2026-05-10 11:00:00",
                    "file": "home/joe/project/A/session-deadbeef99.md",
                }
            ],
        )
        lines = _render_expand_paths_tree([project_backslash, project_forward])
        joined = "\n".join(lines)
        assert "- **home/**" in joined
        assert "- **joe/**" in joined
        assert "- **project/**" in joined
        # Both leaf links must be present, with forward slashes only.
        assert "(home/joe/project/B/session-abcdef1234.md)" in joined
        assert "(home/joe/project/A/session-deadbeef99.md)" in joined
        assert "\\" not in joined

    def test_cli_combined_only_alias_with_no_individual_sessions(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """`--no-individual-sessions` is the back-compat alias for
        `--combined only`."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-noindividual"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--no-individual-sessions",
                "--format",
                "md",
            ],
        )
        assert result.exit_code == 0, result.output
        # Combined files present, per-session files absent.
        assert list(out.rglob("combined_transcripts*.md"))
        assert not list(out.rglob("session-*.md"))

    def test_cli_conflicting_combined_no_and_no_individual_sessions_rejected(
        self, fake_projects: Path, isolated_cache: Path, tmp_path: Path
    ):
        """`--combined no` + `--no-individual-sessions` is a conflict
        (both attempt to skip per-session files, but --no-individual-sessions
        implies combined-only). Should be rejected."""
        from click.testing import CliRunner

        from claude_code_log.cli import main

        out = tmp_path / "out-conflict"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(fake_projects),
                "--all-projects",
                "--output",
                str(out),
                "--no-individual-sessions",
                "--combined",
                "no",
                "--format",
                "md",
            ],
        )
        assert result.exit_code != 0
        assert "conflicts" in result.output.lower() or "no-individual" in result.output
