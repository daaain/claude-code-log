#!/usr/bin/env python3
"""CLI interface for claude-code-log."""

import faulthandler
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import click
from git import Repo, InvalidGitRepositoryError

from .converter import (
    convert_jsonl_to,
    convert_jsonl_to_html,
    ensure_fresh_cache,
    generate_single_session_file,
    get_file_extension,
    get_index_filename,
    process_projects_hierarchy,
)
from .cache import (
    CacheManager,
    find_session_in_cache,
    get_all_cached_projects,
    get_cache_db_path,
    get_library_version,
)


def _install_stack_dump_signal() -> None:
    """Make ``kill -USR1 <pid>`` print the live Python stack to stderr.

    Useful for diagnosing apparent hangs without killing the process —
    py-spy needs root on macOS, but this only needs the signal. SIGUSR1
    is POSIX-only; on Windows we silently skip.
    """
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return
    try:
        faulthandler.register(sigusr1, all_threads=True, chain=False)
    except (RuntimeError, ValueError, OSError):
        # E.g. signal already taken, no-tty environments, or platforms
        # where faulthandler.register raises. Diagnostics shouldn't
        # break the CLI — silently skip.
        pass


def get_default_projects_dir() -> Path:
    """Get the default Claude projects directory path."""
    return Path.home() / ".claude" / "projects"


def _discover_projects(
    projects_dir: Path,
) -> tuple[list[Path], set[Path]]:
    """Discover active and archived projects in the projects directory.

    Returns:
        Tuple of (all_project_dirs, archived_projects_set)
    """
    # Find active projects (directories with JSONL files)
    project_dirs = [
        d for d in projects_dir.iterdir() if d.is_dir() and list(d.glob("*.jsonl"))
    ]

    # Find archived projects (in cache but without JSONL files)
    archived_projects: set[Path] = set()
    cached_projects = get_all_cached_projects(projects_dir)
    active_project_paths = {str(p) for p in project_dirs}
    for project_path_str, is_archived in cached_projects:
        if is_archived and project_path_str not in active_project_paths:
            archived_path = Path(project_path_str)
            archived_projects.add(archived_path)
            project_dirs.append(archived_path)

    return project_dirs, archived_projects


def _launch_tui_with_cache_check(
    project_path: Path, is_archived: bool = False
) -> Optional[str]:
    """Launch TUI with proper cache checking and user feedback."""
    click.echo("Checking cache and loading session data...")

    # Check if we need to rebuild cache
    cache_manager = CacheManager(project_path, get_library_version())
    project_cache = cache_manager.get_cached_project_data()

    if is_archived:
        # Archived projects have no JSONL files, just load from cache
        if project_cache and project_cache.sessions:
            click.echo(
                f"[ARCHIVED] Found {len(project_cache.sessions)} sessions in cache. Launching TUI..."
            )
        else:
            click.echo("Error: No cached sessions found for archived project", err=True)
            return None
    else:
        jsonl_files = list(project_path.glob("*.jsonl"))
        modified_files = cache_manager.get_modified_files(jsonl_files)

        if not (project_cache and project_cache.sessions and not modified_files):
            # Need to rebuild cache
            if modified_files:
                click.echo(
                    f"Found {len(modified_files)} modified files, rebuilding cache..."
                )
            else:
                click.echo("Building session cache...")

            # Pre-build the cache before launching TUI (no HTML generation)
            try:
                ensure_fresh_cache(project_path, cache_manager, silent=True)
                click.echo("Cache ready! Launching TUI...")
            except Exception as e:
                click.echo(f"Error building cache: {e}", err=True)
                return None
        else:
            click.echo(
                f"Cache up to date. Found {len(project_cache.sessions)} sessions. Launching TUI..."
            )

    # Small delay to let user see the message before TUI clears screen
    import time

    time.sleep(0.5)

    from .tui import run_session_browser

    result = run_session_browser(project_path, is_archived=is_archived)
    return result


def convert_project_path_to_claude_dir(
    input_path: Path, base_projects_dir: Optional[Path] = None
) -> Path:
    """Convert a project path to the corresponding directory in ~/.claude/projects/.

    Args:
        input_path: The project path to convert
        base_projects_dir: Optional base directory for Claude projects.
                          Defaults to ~/.claude/projects/
    """
    # Get the real path to resolve any symlinks
    real_path = input_path.resolve()

    # Convert the path to the expected format: replace slashes with hyphens
    path_parts = list(real_path.parts)

    # Handle platform-specific root components
    if path_parts[0] == "/":
        # Unix: Remove leading slash, then prepend with dash
        # e.g., ['/', 'Users', 'test'] -> ['Users', 'test'] -> '-Users-test'
        path_parts = path_parts[1:]
        claude_project_name = "-" + "-".join(path_parts)
    elif len(path_parts) > 0 and len(path_parts[0]) >= 2 and path_parts[0][1:2] == ":":
        # Windows: Strip backslash and colon from drive letter, keep empty string for double dash
        # e.g., ['E:\\', 'Workspace', 'src'] -> ['E', '', 'Workspace', 'src'] -> 'E--Workspace-src'
        path_parts[0] = path_parts[0].rstrip("\\").rstrip(":")
        path_parts.insert(
            1, ""
        )  # Insert empty string to create double dash after drive letter
        claude_project_name = "-".join(path_parts)
    else:
        # Fallback for other cases
        claude_project_name = "-" + "-".join(path_parts)

    # Construct the path in the projects directory
    projects_dir = base_projects_dir or get_default_projects_dir()
    claude_projects_dir = projects_dir / claude_project_name

    return claude_projects_dir


def find_projects_by_cwd(
    projects_dir: Path, current_cwd: Optional[str] = None
) -> list[Path]:
    """Find Claude projects that match the current working directory.

    Uses three-tier priority matching:
    1. Exact match to current working directory
    2. Git repository root match
    3. Relative path matching
    """
    if current_cwd is None:
        current_cwd = os.getcwd()

    # Normalize the current working directory
    current_cwd_path = Path(current_cwd).resolve()

    # Check all project directories
    if not projects_dir.exists():
        return []

    # Get all valid project directories
    project_dirs = [
        d for d in projects_dir.iterdir() if d.is_dir() and list(d.glob("*.jsonl"))
    ]

    # Tier 1: Check for exact match to current working directory
    exact_matches = _find_exact_matches(project_dirs, current_cwd_path, projects_dir)
    if exact_matches:
        return exact_matches

    # Tier 2: Check if we're inside a git repo and match to repo root
    git_root_matches = _find_git_root_matches(
        project_dirs, current_cwd_path, projects_dir
    )
    if git_root_matches:
        return git_root_matches

    # Tier 3: Fall back to relative path matching
    return _find_relative_matches(project_dirs, current_cwd_path)


def _find_exact_matches(
    project_dirs: list[Path], current_cwd_path: Path, base_projects_dir: Path
) -> list[Path]:
    """Find projects with exact working directory matches using path-based matching."""
    expected_project_dir = convert_project_path_to_claude_dir(
        current_cwd_path, base_projects_dir
    )

    for project_dir in project_dirs:
        if project_dir == expected_project_dir:
            return [project_dir]

    return []


def _find_git_root_matches(
    project_dirs: list[Path], current_cwd_path: Path, base_projects_dir: Path
) -> list[Path]:
    """Find projects that match the git repository root using path-based matching."""
    try:
        # Check if we're inside a git repository
        repo = Repo(current_cwd_path, search_parent_directories=True)
        git_root_path = Path(repo.git_dir).parent.resolve()

        # Find projects that match the git root
        return _find_exact_matches(project_dirs, git_root_path, base_projects_dir)
    except InvalidGitRepositoryError:
        # Not in a git repository
        return []
    except Exception:
        # Other git-related errors
        return []


def _find_relative_matches(
    project_dirs: list[Path], current_cwd_path: Path
) -> list[Path]:
    """Find projects using relative path matching (original behavior)."""
    relative_matches: list[Path] = []

    for project_dir in project_dirs:
        try:
            # Load cache to check for working directories
            cache_manager = CacheManager(project_dir, get_library_version())
            working_directories = cache_manager.get_working_directories()

            # Build cache if needed
            if not working_directories:
                jsonl_files = list(project_dir.glob("*.jsonl"))
                if jsonl_files:
                    try:
                        convert_jsonl_to_html(project_dir, silent=True)
                        working_directories = cache_manager.get_working_directories()
                    except Exception as e:
                        logging.warning(
                            f"Failed to build cache for project {project_dir.name}: {e}"
                        )

            if working_directories:
                # Check for relative matches
                for cwd in working_directories:
                    cwd_path = Path(cwd).resolve()
                    if current_cwd_path.is_relative_to(cwd_path):
                        relative_matches.append(project_dir)
                        break
            else:
                # Fall back to path name matching if no cache data
                project_name = project_dir.name
                reconstructed_path = None

                if project_name.startswith("-"):
                    # Unix path: -Users-test-workspace
                    path_parts = project_name[1:].split("-")
                    if path_parts:
                        reconstructed_path = Path("/") / Path(*path_parts)
                elif len(project_name) >= 1 and not project_name.startswith("-"):
                    # Windows path: C--Users-test or E--Workspace-src
                    path_parts = project_name.split("-")
                    if (
                        len(path_parts) >= 2
                        and len(path_parts[0]) == 1
                        and path_parts[1] == ""
                    ):
                        # Drive letter detected (e.g., ['C', '', 'Users', ...])
                        drive = path_parts[0] + ":\\"
                        remaining_parts = [
                            p for p in path_parts[2:] if p
                        ]  # Skip drive and empty string
                        if remaining_parts:
                            reconstructed_path = Path(drive) / Path(*remaining_parts)
                        else:
                            reconstructed_path = Path(drive)

                if reconstructed_path and (
                    current_cwd_path == reconstructed_path
                    or current_cwd_path.is_relative_to(reconstructed_path)
                    or reconstructed_path.is_relative_to(current_cwd_path)
                ):
                    relative_matches.append(project_dir)
        except Exception:
            continue

    return relative_matches


def _clear_caches(input_path: Path, all_projects: bool) -> None:
    """Clear cache directories for the specified path."""
    try:
        library_version = get_library_version()

        if all_projects:
            # Clear cache for all project directories
            click.echo("Clearing caches for all projects...")

            # Delete the SQLite cache database (respects CLAUDE_CODE_LOG_CACHE_PATH env var)
            cache_db = get_cache_db_path(input_path)
            if cache_db.exists():
                try:
                    cache_db.unlink()
                    click.echo(f"  Deleted SQLite cache database: {cache_db}")
                except Exception as e:
                    click.echo(f"  Warning: Failed to delete cache database: {e}")

            # Also clean up old JSON cache directories (migration cleanup)
            project_dirs = [
                d
                for d in input_path.iterdir()
                if d.is_dir() and list(d.glob("*.jsonl"))
            ]

            for project_dir in project_dirs:
                try:
                    # Clean up old JSON cache directory if it exists
                    old_cache_dir = project_dir / "cache"
                    if old_cache_dir.exists():
                        import shutil

                        shutil.rmtree(old_cache_dir)
                        click.echo(f"  Cleared old JSON cache for {project_dir.name}")
                except Exception as e:
                    click.echo(
                        f"  Warning: Failed to clear old cache for {project_dir.name}: {e}"
                    )

        elif input_path.is_dir():
            # Clear cache for single directory
            click.echo(f"Clearing cache for {input_path}...")
            cache_manager = CacheManager(input_path, library_version)
            cache_manager.clear_cache()

            # Also clean up old JSON cache directory if it exists
            old_cache_dir = input_path / "cache"
            if old_cache_dir.exists():
                import shutil

                shutil.rmtree(old_cache_dir)
                click.echo("  Cleared old JSON cache directory")
        else:
            # Single file - no cache to clear
            click.echo("Cache clearing not applicable for single files.")

    except Exception as e:
        click.echo(f"Warning: Failed to clear cache: {e}")


def _list_generated_outputs(directory: Path, file_ext: str) -> list[Path]:
    """Return only files this tool generates, not every file with the extension.

    Safe for JSON in particular, where the project directory may contain
    unrelated user `.json` files that must not be deleted.
    """
    if file_ext == "json":
        return [
            *directory.glob("combined_transcripts*.json"),
            *directory.glob("session-*.json"),
        ]
    return list(directory.glob(f"*.{file_ext}"))


def _clear_output_files(
    input_path: Path, all_projects: bool, output_format: str
) -> None:
    """Clear generated output files (HTML/Markdown/JSON) for the specified path."""
    file_ext = get_file_extension(output_format)
    ext_upper = file_ext.upper()
    try:
        if all_projects:
            # Clear output files for all project directories
            click.echo(f"Clearing {ext_upper} files for all projects...")
            project_dirs = [
                d
                for d in input_path.iterdir()
                if d.is_dir() and list(d.glob("*.jsonl"))
            ]

            total_removed = 0
            for project_dir in project_dirs:
                try:
                    # Remove output files in project directory
                    output_files = _list_generated_outputs(project_dir, file_ext)
                    for output_file in output_files:
                        output_file.unlink()
                        total_removed += 1

                    if output_files:
                        click.echo(
                            f"  Removed {len(output_files)} {ext_upper} files from {project_dir.name}"
                        )
                except Exception as e:
                    click.echo(
                        f"  Warning: Failed to clear {ext_upper} files for {project_dir.name}: {e}"
                    )

            # Also remove top-level index file (shared helper keeps this in
            # sync with the generator, which uses a different name for JSON).
            index_filename = get_index_filename(output_format)
            index_file = input_path / index_filename
            if index_file.exists():
                index_file.unlink()
                total_removed += 1
                click.echo(f"  Removed top-level {index_filename}")

            if total_removed > 0:
                click.echo(f"Total: Removed {total_removed} {ext_upper} files")
            else:
                click.echo(f"No {ext_upper} files found to remove")

        elif input_path.is_dir():
            # Clear output files for single directory
            click.echo(f"Clearing {ext_upper} files for {input_path}...")
            output_files = _list_generated_outputs(input_path, file_ext)
            for output_file in output_files:
                output_file.unlink()

            if output_files:
                click.echo(f"Removed {len(output_files)} {ext_upper} files")
            else:
                click.echo(f"No {ext_upper} files found to remove")
        else:
            # Single file - remove corresponding output file
            output_file = input_path.with_suffix(f".{file_ext}")
            if output_file.exists():
                output_file.unlink()
                click.echo(f"Removed {output_file}")
            else:
                click.echo(f"No corresponding {ext_upper} file found to remove")

    except Exception as e:
        click.echo(f"Warning: Failed to clear {ext_upper} files: {e}")


# Placeholders accepted by ``--git-link`` templates. Mirrors
# ``resolve_sha`` in claude_code_log.git_remote — keep in sync.
_GIT_LINK_ALLOWED_PLACEHOLDERS = frozenset({"host", "path", "sha"})


def _validate_git_link_template(template: str) -> None:
    """Validate a ``--git-link`` template eagerly; raise ``click.UsageError`` on issues.

    Two checks:

    1. ``{sha}`` must be present (it's the only mandatory field —
       the resolver's whole job is to substitute the commit SHA).
    2. All placeholders must be in ``_GIT_LINK_ALLOWED_PLACEHOLDERS``.
       Catches typos like ``{hsot}`` before they reach
       ``template.format()`` (which would raise ``KeyError`` at
       render time). The resolver has a try/except guarding the
       env-var-only path; this validator is the loud-error path for
       CLI users.

    Uses ``string.Formatter().parse()`` rather than regex so the
    same parser Python uses for ``str.format`` decides what counts
    as a placeholder.
    """
    import string

    parsed_fields = [
        field
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    ]
    if "" in parsed_fields:
        raise click.UsageError(
            "--git-link template uses an anonymous positional placeholder ({}). "
            "Use a named placeholder ({host}, {path}, or {sha}) instead "
            f"(got: {template!r})."
        )
    fields = set(parsed_fields)
    unknown = fields - _GIT_LINK_ALLOWED_PLACEHOLDERS
    if unknown:
        raise click.UsageError(
            f"--git-link template uses unknown placeholder(s): "
            f"{', '.join('{' + f + '}' for f in sorted(unknown))}. "
            f"Allowed: {{host}}, {{path}}, {{sha}}."
        )
    if "sha" not in fields:
        raise click.UsageError(
            "--git-link template must contain a {sha} placeholder "
            f"(got: {template!r}). Example: "
            "'https://{host}/{path}/-/commit/{sha}'."
        )


@click.command()
@click.version_option(version=get_library_version(), prog_name="claude-code-log")
@click.argument("input_path", type=click.Path(path_type=Path), required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help=(
        "Output destination. With a recognised file suffix "
        "(.html/.md/.markdown/.json) treated as a single output file; "
        "otherwise treated as a directory root (and now also honoured "
        "for --all-projects, where outputs land at "
        "<output>/<project>/...). Pair with --expand-paths to project "
        "back to the real on-disk tree."
    ),
)
@click.option(
    "--expand-paths",
    is_flag=True,
    help=(
        "When set with --output and --all-projects, expand each "
        "project's flat encoded dir name (e.g. '-home-joe-project-A') "
        "back to its real path under <output>/. Resolves the encoded "
        "name via the cache's recorded `cwd`, falling back to a peek "
        "of the first JSONL when the cache is empty. Useful for "
        "projecting transcripts into Obsidian-style Markdown vaults."
    ),
)
@click.option(
    "--filter-path",
    type=str,
    default=None,
    help=(
        "Restrict --all-projects to projects matching a path prefix. "
        "With --expand-paths, the prefix is matched against the "
        "expanded real path AND truncated from the destination "
        "(`/home/joe/project/A` with --filter-path /home/joe lands at "
        "<output>/project/A/). Without --expand-paths, matches the "
        "flat encoded dir name (e.g. '-home-joe' selects projects "
        "starting with '-home-joe-')."
    ),
)
@click.option(
    "--open-browser",
    is_flag=True,
    help="Open the generated HTML file in the default browser",
)
@click.option(
    "--from-date",
    type=str,
    help='Filter messages from this date/time (e.g., "2 hours ago", "yesterday", "2025-06-08")',
)
@click.option(
    "--to-date",
    type=str,
    help='Filter messages up to this date/time (e.g., "1 hour ago", "today", "2025-06-08 15:00")',
)
@click.option(
    "--all-projects",
    is_flag=True,
    help="Process all projects in ~/.claude/projects/ hierarchy and create linked HTML files",
)
@click.option(
    "--no-individual-sessions",
    is_flag=True,
    help=(
        "Skip generating individual session files (combined transcript only). "
        "Back-compat alias for --combined only."
    ),
)
@click.option(
    "--combined",
    "combined",
    type=click.Choice(["yes", "no", "only"], case_sensitive=False),
    default=None,
    help=(
        "Control combined-vs-individual transcript generation: "
        "'yes' = both combined and per-session files (default for --all-projects); "
        "'no' = only per-session files (recommended for Obsidian / vault use — "
        "combined is dead weight); "
        "'only' = only the combined file (= --no-individual-sessions). "
        "When unset, defaults to 'no' under --expand-paths (Obsidian mode), "
        "else 'yes'."
    ),
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Disable caching and force reprocessing of all files",
)
@click.option(
    "--clear-cache",
    is_flag=True,
    help="Clear all cache directories before processing",
)
@click.option(
    "--clear-output",
    "--clear-html",
    "clear_output",
    is_flag=True,
    help="Clear generated output files (HTML or Markdown based on --format) and force regeneration",
)
@click.option(
    "--tui",
    is_flag=True,
    help="Launch interactive TUI for session browsing and management",
)
@click.option(
    "--projects-dir",
    type=click.Path(path_type=Path, exists=False),
    default=None,
    help="Custom projects directory (default: ~/.claude/projects/). Useful for testing.",
)
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["html", "md", "markdown", "json"]),
    default="html",
    help="Output format (default: html). Supports html, md/markdown, or json.",
)
@click.option(
    "--image-export-mode",
    type=click.Choice(["placeholder", "embedded", "referenced"]),
    default=None,
    help="Image export mode: placeholder (mark position), embedded (base64), referenced (PNG files). Default: embedded for HTML, referenced for Markdown.",
)
@click.option(
    "--page-size",
    type=int,
    default=2000,
    help="Maximum messages per page for combined transcript (default: 2000). Sessions are never split across pages.",
)
@click.option(
    "--session-id",
    default=None,
    help="Export a single session by ID (full ID or prefix). Project path is optional — looks up the session globally via cache.",
)
@click.option(
    "--detail",
    type=click.Choice(
        ["full", "high", "low", "minimal", "user-only"], case_sensitive=False
    ),
    default="full",
    help=(
        "Detail level for output. "
        "full: everything; "
        "high: detailed but cleaned (no system/hook noise); "
        "low: interaction-focused + key signals; "
        "minimal: user + assistant messages only; "
        "user-only: only user prompts and steering (for feeding to "
        "downstream agents, e.g. building a requirements doc)."
    ),
)
@click.option(
    "--compact",
    is_flag=True,
    help=(
        "Merge consecutive same-category headings in Markdown output. "
        "Markdown-only — a no-op for HTML."
    ),
)
@click.option(
    "--git-link",
    "git_link",
    default=None,
    envvar="CLAUDE_CODE_LOG_GIT_LINK",
    metavar="TEMPLATE",
    help=(
        "URL template for resolving commit SHAs on forges not in the built-in "
        "map (github.com, gitlab.com, bitbucket.org). Placeholders: {host}, "
        "{path}, {sha}. Example for self-hosted GitLab: "
        "--git-link 'https://{host}/{path}/-/commit/{sha}'. Can also be set "
        "via the CLAUDE_CODE_LOG_GIT_LINK env var."
    ),
)
@click.option(
    "--no-timestamps",
    is_flag=True,
    help=(
        "Suppress per-message timestamp lines in Markdown output "
        "(#160). Markdown-only — a warning is emitted (but not an "
        "error) if combined with --format html / --format json."
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Show full traceback on errors.",
)
def main(
    input_path: Optional[Path],
    output: Optional[Path],
    expand_paths: bool,
    filter_path: Optional[str],
    combined: Optional[str],
    open_browser: bool,
    from_date: Optional[str],
    to_date: Optional[str],
    all_projects: bool,
    no_individual_sessions: bool,
    no_cache: bool,
    clear_cache: bool,
    clear_output: bool,
    tui: bool,
    projects_dir: Optional[Path],
    output_format: str,
    image_export_mode: Optional[str],
    page_size: int,
    session_id: Optional[str],
    detail: str,
    compact: bool,
    git_link: Optional[str],
    no_timestamps: bool,
    debug: bool,
) -> None:
    """Convert Claude transcript JSONL files to HTML or Markdown.

    INPUT_PATH: Path to a Claude transcript JSONL file, directory containing JSONL files, or project path to convert. If not provided, defaults to ~/.claude/projects/ and --all-projects is used.
    """
    # Install signal-based stack dumper before any heavy work, so a hang
    # can be diagnosed with `kill -USR1 <pid>` without root or restart.
    _install_stack_dump_signal()

    # Custom-forge URL template: validate eagerly with a loud error,
    # then pin to the env var so the resolver (which reads the env at
    # render time) picks it up. Doing this at env-var level keeps the
    # resolver decoupled from Click; the env var is the underlying
    # contract, the CLI flag is a convenience that sets it.
    if git_link is not None:
        _validate_git_link_template(git_link)
        os.environ["CLAUDE_CODE_LOG_GIT_LINK"] = git_link

    # Configure logging to show warnings and above
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    # Resolve --combined default and back-compat with --no-individual-sessions.
    # `--combined` semantics:
    #   yes  → write combined transcript AND per-session files
    #   no   → write per-session files only (Obsidian-friendly)
    #   only → write combined transcript only (= --no-individual-sessions)
    # Default: yes, except when --expand-paths is set (Obsidian mode → no).
    if combined is None:
        combined = "no" if expand_paths else "yes"
    else:
        combined = combined.lower()
    if no_individual_sessions:
        if combined == "no":
            raise click.BadParameter(
                "--no-individual-sessions conflicts with --combined no "
                "(both attempt to skip per-session files but --no-individual-sessions "
                "implies combined-only). Pick one.",
                param_hint="--no-individual-sessions",
            )
        # `--no-individual-sessions` is a strict alias for `--combined only`;
        # honour it for back-compat (and prefer this over an unset --combined).
        combined = "only"
    # Derived flags actually consumed downstream.
    write_combined = combined in ("yes", "only")
    write_individual = combined in ("yes", "no")

    # Loud rejection of relative `--filter-path` when paired with
    # `--expand-paths` (#151). Without this, a user typing
    # `--filter-path home/joe` (forgetting the leading `/`) would
    # match against an absolute resolved path via `Path.relative_to`,
    # which raises ValueError for *any* mismatch including
    # "argument is relative" — so the silent failure mode is "every
    # project skipped". Reject up-front instead.
    #
    # `path_looks_absolute` is host-OS-agnostic (accepts POSIX `/`
    # OR Windows `C:\` form), so a Linux-recorded `/home/joe`
    # processed on Windows still passes the guard.
    from .utils import path_looks_absolute as _path_looks_absolute

    if filter_path and expand_paths and not _path_looks_absolute(filter_path):
        raise click.BadParameter(
            f"--filter-path must be an absolute path when --expand-paths is set; "
            f"got {filter_path!r}",
            param_hint="--filter-path",
        )

    # Warn early if Obsidian-friendly flags (#151) were passed in a
    # context where they're no-ops. `--all-projects` (explicit or
    # implicit via no input_path) is the only mode that consumes them;
    # `--output` must be a directory (file-suffixed output goes
    # through the single-file path which doesn't honour these flags).
    from .utils import output_path_is_file as _output_path_is_file

    will_run_all_projects = all_projects or input_path is None
    if (expand_paths or filter_path) and tui:
        click.echo(
            "Warning: --expand-paths / --filter-path are ignored in --tui mode.",
            err=True,
        )
    elif (expand_paths or filter_path) and not will_run_all_projects:
        click.echo(
            "Warning: --expand-paths / --filter-path require --all-projects "
            "(or omitting INPUT_PATH); ignoring.",
            err=True,
        )
    elif (expand_paths or filter_path) and (
        output is None or _output_path_is_file(output)
    ):
        click.echo(
            "Warning: --expand-paths / --filter-path require --output to be a "
            "directory (no recognised file suffix); ignoring.",
            err=True,
        )

    # `--no-timestamps` is Markdown-only (#160). Warn (not error) when
    # paired with HTML/JSON so the flag is benignly ignored rather than
    # silently misapplied.
    if no_timestamps and output_format not in ("md", "markdown"):
        click.echo(
            f"Warning: --no-timestamps is Markdown-only; ignoring under "
            f"--format {output_format}.",
            err=True,
        )

    from .models import DetailLevel

    detail_level = DetailLevel(detail.lower())

    try:
        # Handle TUI mode
        if tui:
            # Handle default case for TUI - use projects_dir or default ~/.claude/projects
            if input_path is None:
                input_path = projects_dir or get_default_projects_dir()

            # If targeting all projects, show project selection TUI
            if (
                all_projects
                or not input_path.exists()
                or not list(input_path.glob("*.jsonl"))
            ):
                # Show project selection interface
                if not input_path.exists():
                    click.echo(f"Error: Projects directory not found: {input_path}")
                    return

                # Initial project discovery
                project_dirs, archived_projects = _discover_projects(input_path)

                if not project_dirs:
                    click.echo(f"No projects with JSONL files found in {input_path}")
                    return

                # Try to find projects that match current working directory
                matching_projects = find_projects_by_cwd(input_path)

                if len(project_dirs) == 1 and not archived_projects:
                    # Only one project, open it directly
                    result = _launch_tui_with_cache_check(project_dirs[0])
                    if result == "back_to_projects":
                        # User wants to see project selector even though there's only one project
                        from .tui import run_project_selector

                        while True:
                            # Re-discover projects (may have changed after restore)
                            project_dirs, archived_projects = _discover_projects(
                                input_path
                            )
                            selected_project = run_project_selector(
                                project_dirs, matching_projects, archived_projects
                            )
                            if not selected_project:
                                # User cancelled
                                return

                            is_archived = selected_project in archived_projects
                            result = _launch_tui_with_cache_check(
                                selected_project, is_archived=is_archived
                            )
                            if result != "back_to_projects":
                                # User quit normally
                                return
                    return
                elif matching_projects and len(matching_projects) == 1:
                    # Found exactly one project matching current working directory
                    click.echo(
                        f"Found project matching current directory: {matching_projects[0].name}"
                    )
                    result = _launch_tui_with_cache_check(matching_projects[0])
                    if result == "back_to_projects":
                        # User wants to see project selector
                        from .tui import run_project_selector

                        while True:
                            # Re-discover projects (may have changed after restore)
                            project_dirs, archived_projects = _discover_projects(
                                input_path
                            )
                            selected_project = run_project_selector(
                                project_dirs, matching_projects, archived_projects
                            )
                            if not selected_project:
                                # User cancelled
                                return

                            is_archived = selected_project in archived_projects
                            result = _launch_tui_with_cache_check(
                                selected_project, is_archived=is_archived
                            )
                            if result != "back_to_projects":
                                # User quit normally
                                return
                    return
                else:
                    # Multiple projects or multiple matching projects - show selector
                    from .tui import run_project_selector

                    while True:
                        # Re-discover projects each iteration (may have changed after restore)
                        project_dirs, archived_projects = _discover_projects(input_path)
                        selected_project = run_project_selector(
                            project_dirs, matching_projects, archived_projects
                        )
                        if not selected_project:
                            # User cancelled
                            return

                        is_archived = selected_project in archived_projects
                        result = _launch_tui_with_cache_check(
                            selected_project, is_archived=is_archived
                        )
                        if result != "back_to_projects":
                            # User quit normally
                            return
            else:
                # Single project directory
                _launch_tui_with_cache_check(input_path)
                return

        # Handle --session-id: export a single session by ID
        if session_id is not None:
            if input_path is None:
                # Global lookup via cache
                effective_projects_dir = projects_dir or get_default_projects_dir()
                matches = find_session_in_cache(session_id, effective_projects_dir)
                if not matches:
                    click.echo(
                        f"Error: Session '{session_id}' not found in cache. "
                        "Try providing a project directory path, or run "
                        "claude-code-log first to populate the cache.",
                        err=True,
                    )
                    sys.exit(1)
                if len(matches) > 1:
                    # Check if all matches resolve to the same session ID
                    unique_ids = {m[1] for m in matches}
                    if len(unique_ids) > 1:
                        click.echo(
                            f"Error: Ambiguous session ID prefix '{session_id}' "
                            "matches multiple sessions:",
                            err=True,
                        )
                        for proj_path, sid in matches:
                            click.echo(f"  {sid[:8]} in {proj_path}", err=True)
                        sys.exit(1)
                input_path = Path(matches[0][0])
                session_id = matches[0][1]
            else:
                # Convert project path if needed
                if not input_path.exists() or (
                    input_path.is_dir() and not list(input_path.glob("*.jsonl"))
                ):
                    claude_path = convert_project_path_to_claude_dir(
                        input_path, projects_dir
                    )
                    if claude_path.exists():
                        input_path = claude_path

            output_path = generate_single_session_file(
                output_format,
                input_path,
                session_id,
                output,
                not no_cache,
                image_export_mode,
                detail=detail_level,
                compact=compact,
                no_timestamps=no_timestamps,
            )
            click.echo(f"Successfully exported session to {output_path}")
            if open_browser:
                click.launch(str(output_path))
            return

        # Handle default case - process all projects hierarchy if no input path and --all-projects flag
        if input_path is None:
            input_path = projects_dir or get_default_projects_dir()
            all_projects = True

        # Handle cache clearing
        if clear_cache:
            _clear_caches(input_path, all_projects)
            if clear_cache and not (from_date or to_date or input_path.is_file()):
                # If only clearing cache, exit after clearing
                click.echo("Cache cleared successfully.")
                return

        # Handle output files clearing
        if clear_output:
            _clear_output_files(input_path, all_projects, output_format)
            if clear_output and not (from_date or to_date or input_path.is_file()):
                # If only clearing output files, exit after clearing
                file_ext = get_file_extension(output_format)
                click.echo(f"{file_ext.upper()} files cleared successfully.")
                return

        # Handle --all-projects flag or default behavior
        if all_projects:
            if not input_path.exists():
                raise FileNotFoundError(f"Projects directory not found: {input_path}")

            click.echo(f"Processing all projects in {input_path}...")
            # `--output` for `--all-projects` (#151): pass a *directory*
            # to project per-project outputs into. File-suffixed values
            # are routed to the single-file path elsewhere; here we
            # only honour directory-shaped `--output`.
            from .utils import output_path_is_file

            output_dir_for_projects: Optional[Path] = None
            if output is not None and not output_path_is_file(output):
                output_dir_for_projects = output

            output_path = process_projects_hierarchy(
                input_path,
                from_date,
                to_date,
                not no_cache,
                write_individual,
                output_format,
                image_export_mode,
                page_size=page_size,
                detail=detail_level,
                compact=compact,
                output_dir=output_dir_for_projects,
                expand_paths=expand_paths,
                filter_path=filter_path,
                write_combined=write_combined,
                no_timestamps=no_timestamps,
            )

            # Count processed projects
            project_count = len(
                [
                    d
                    for d in input_path.iterdir()
                    if d.is_dir() and list(d.glob("*.jsonl"))
                ]
            )
            click.echo(
                f"Successfully processed {project_count} projects and created index at {output_path}"
            )

            if open_browser:
                click.launch(str(output_path))
            return

        # Original single file/directory processing logic
        should_convert = False

        if not input_path.exists():
            # Path doesn't exist, try conversion
            should_convert = True
        elif input_path.is_dir():
            # Path exists and is a directory, check if it has JSONL files
            jsonl_files = list(input_path.glob("*.jsonl"))
            if len(jsonl_files) == 0:
                # No JSONL files found, try conversion
                should_convert = True

        if should_convert:
            claude_path = convert_project_path_to_claude_dir(input_path, projects_dir)
            if claude_path.exists():
                click.echo(f"Converting project path {input_path} to {claude_path}")
                input_path = claude_path
            elif not input_path.exists():
                # Original path doesn't exist and conversion failed
                raise FileNotFoundError(
                    f"Neither {input_path} nor {claude_path} exists"
                )

        output_path = convert_jsonl_to(
            output_format,
            input_path,
            output,
            from_date,
            to_date,
            write_individual,
            not no_cache,
            image_export_mode=image_export_mode,
            page_size=page_size,
            detail=detail_level,
            compact=compact,
            # User's `-o` path is a one-off export, not a cached artifact:
            # don't occupy a cache slot keyed by an arbitrary destination.
            update_cache=output is None,
            write_combined=write_combined,
            no_timestamps=no_timestamps,
        )
        if input_path.is_file():
            click.echo(f"Successfully converted {input_path} to {output_path}")
        else:
            jsonl_count = len(list(input_path.glob("*.jsonl")))
            if write_individual:
                ext = get_file_extension(output_format)
                session_files = list(input_path.glob(f"session-*.{ext}"))
                click.echo(
                    f"Successfully combined {jsonl_count} transcript files from {input_path} to {output_path} and generated {len(session_files)} individual session files"
                )
            else:
                click.echo(
                    f"Successfully combined {jsonl_count} transcript files from {input_path} to {output_path}"
                )

        if open_browser:
            click.launch(str(output_path))

    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        if debug:
            import traceback

            traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error converting file: {e}", err=True)
        if debug:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
