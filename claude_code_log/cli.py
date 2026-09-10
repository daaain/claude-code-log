#!/usr/bin/env python3
"""CLI interface for claude-code-log."""

import faulthandler
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import click
from git import Repo, InvalidGitRepositoryError

from .watch import (
    DEFAULT_MAX_LATENCY,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_PERIOD,
)
from .converter import (
    RegenerationReport,
    convert_jsonl_to,
    convert_jsonl_to_html,
    ensure_fresh_cache,
    generate_single_session_file,
    render_normalized_session_file,
    render_provider_wholesale,
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
    is_corrupt_database_error,
)
from .models import RenderingDepth
from .render_pool import resolve_render_jobs
from .search import (
    ENV_INDEX_FIELDS,
    ENV_SEARCH_FIELDS,
    SEARCH_FIELDS,
)


# Output values that mean "stream the rendered document to stdout" (issue
# #223): write the document to stdout, always regenerate, never consult or
# write the cache, never open a browser, and keep status text off stdout
# (it goes to stderr) so the stream carries only the rendered document.
_STDOUT_TARGETS = {"-", "/dev/stdout"}


def _is_stdout_target(output: "Optional[Path]") -> bool:
    """Whether ``--output`` requests a stdout stream (``-`` or /dev/stdout)."""
    return output is not None and str(output) in _STDOUT_TARGETS


def _render_to_stdout(
    input_path: Path, render_to_temp: "Callable[[Path], Path]"
) -> None:
    """Render to a throwaway temp file, then stream it to stdout (issue #223).

    The temp file lets the full conversion pipeline run unchanged; we then
    copy its raw bytes to stdout and discard the temp dir. ``render_to_temp``
    receives the temp directory and returns the written file path. The
    rendered document is the only thing written to stdout; a confirmation
    goes to stderr so the stream stays clean for piping.
    """
    import contextlib
    import io
    import shutil
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="ccl-stream-"))
    # Capture anything the render prints to stdout (e.g. the per-file
    # "Processing ..." progress, which generate_single_session_file emits
    # without a silent switch) so it can't pollute the document stream;
    # forward it to stderr instead.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            written = render_to_temp(tmpdir)
        # Read/write RAW BYTES: the document is UTF-8 (transcripts are
        # emoji-heavy), but stdout's text encoding follows the locale and may
        # not be UTF-8 — decoding then re-encoding via sys.stdout.write would
        # mangle or raise on those characters. Pass the bytes through verbatim.
        content = Path(written).read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    progress = captured.getvalue()
    if progress:
        click.echo(progress, nl=False, err=True)
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(content)
        stdout_buffer.flush()
    else:
        # Fallback (e.g. a text-only stdout shim): decode best-effort.
        sys.stdout.write(content.decode("utf-8", errors="replace"))
    click.echo(f"Successfully converted {input_path} to stdout", err=True)


def _render_provider_input_file(
    provider_name: str,
    input_path: Path,
    output: "Optional[Path]",
    output_format: str,
    image_export_mode: "Optional[str]",
    depth: RenderingDepth,
    compact: bool,
    no_timestamps: bool,
    no_recaps: bool,
    open_browser: bool,
) -> None:
    """Render a single provider session file handed in as an INPUT_PATH.

    Shared by the explicit ``--provider <p> <file>`` path and the no-flag
    auto-detected-rollout path. Fails LOUDLY if a provider claimed the file but
    it yields no renderable messages, so a rollout can never fall through to a
    near-empty page (the silent-empty gap).
    """
    from .providers import discover_providers
    from .utils import output_path_is_file

    # A directory INPUT_PATH is a mini sessions root and is routed to the
    # wholesale walker by the dispatcher before it reaches here, so this helper
    # only ever sees a single rollout file.
    selected = discover_providers().get_provider(provider_name)
    if selected is None:
        raise click.UsageError(f"Unknown provider: {provider_name}")
    messages = list(selected.load_session_from_path(input_path))
    if not messages:
        raise click.UsageError(
            f"{input_path} was detected as a {provider_name} session but produced "
            "no renderable messages; it may be truncated or malformed."
        )
    # generate_session filters entries by sessionId, so the render key MUST be
    # the session's own id (the thread id carried on the entries), not the
    # filename stem — a mismatch silently drops every message (the empty-page bug).
    session_key = messages[0].sessionId or input_path.stem
    title = f"{provider_name.title()}: {session_key}"

    def render(destination: Path) -> Path:
        return render_normalized_session_file(
            messages,
            session_key,
            destination,
            output_format,
            title,
            image_export_mode,
            depth,
            compact,
            no_timestamps,
            no_recaps,
        )

    extension = get_file_extension(output_format)
    if _is_stdout_target(output):
        _render_to_stdout(
            input_path,
            lambda tmpdir: render(tmpdir / f"session.{extension}"),
        )
        return
    filename = f"session-{session_key}.{extension}"
    if output is None:
        destination = Path.cwd() / filename
    elif output_path_is_file(output):
        destination = output
    else:
        destination = output / filename
    output_path = render(destination)
    click.echo(f"Successfully rendered {provider_name} session to {output_path}")
    if open_browser:
        click.launch(str(output_path))


def _resolve_provider_output_root(provider_name: str, output: "Optional[Path]") -> Path:
    """Resolve where a provider wholesale run writes its project hierarchy.

    An explicit ``-o DIR`` wins (a file-shaped ``-o`` is rejected — wholesale
    writes many files). Otherwise the default is ``<provider_home>/claude-code-log/``
    (DECIDED #4: the sessions tree stays pristine, so output never lands inside
    it). If the provider has no discoverable home, there is nowhere to default
    to — fail loudly asking for ``-o``.
    """
    from .providers import discover_providers
    from .utils import output_path_is_file

    if output is not None:
        if output_path_is_file(output):
            raise click.UsageError(
                f"provider wholesale rendering writes many files; pass -o as a "
                f"directory, not a file ({output})."
            )
        return output
    provider = discover_providers().get_provider(provider_name)
    data_dir = provider.get_data_dir() if provider is not None else None
    if data_dir is None:
        raise click.UsageError(
            f"no {provider_name} home found to place output; pass -o DIR to "
            "choose an output directory."
        )
    return data_dir / "claude-code-log"


def _run_provider_wholesale(
    provider_name: str,
    sessions_root: "Optional[Path]",
    output: "Optional[Path]",
    output_format: str,
    image_export_mode: "Optional[str]",
    depth: RenderingDepth,
    compact: bool,
    no_timestamps: bool,
    no_recaps: bool,
    write_combined: bool,
    write_individual: bool,
    from_date: "Optional[str]",
    to_date: "Optional[str]",
    no_cache: bool,
    clear_cache: bool,
    clear_output: bool,
    open_browser: bool,
    expand_paths: bool,
    filter_path: "Optional[str]",
) -> None:
    """Render a whole provider sessions tree into a project hierarchy.

    ``sessions_root`` None walks the provider's own data dir; a directory
    (an INPUT_PATH dir, or ``--projects-dir``) selects a mini sessions root.

    ``--clear-cache`` / ``--clear-output`` are scoped to the provider output
    root so the pristine sessions tree is never touched (DECIDED #4). Mirroring
    the Claude path, they clear-and-exit on their own, but when a date filter is
    also given they clear then fall through to REGENERATE the filtered view
    (else ``--clear-output --from-date`` would leave an empty directory).
    """
    output_root = _resolve_provider_output_root(provider_name, output)

    dated = from_date is not None or to_date is not None
    if clear_cache:
        _clear_provider_cache(output_root)
        if not dated:
            return
    if clear_output:
        _clear_provider_output(output_root, output_format)
        if not dated:
            return

    index_path = render_provider_wholesale(
        provider_name,
        sessions_root,
        output_root,
        from_date=from_date,
        to_date=to_date,
        output_format=output_format,
        image_export_mode=image_export_mode,
        depth=depth,
        compact=compact,
        no_timestamps=no_timestamps,
        no_recaps=no_recaps,
        write_combined=write_combined,
        write_individual=write_individual,
        use_cache=not no_cache,
        expand_paths=expand_paths,
        filter_path=filter_path,
        silent=False,
    )
    if open_browser:
        click.launch(str(index_path))


def _clear_provider_cache(output_root: Path) -> None:
    """Delete the provider wholesale cache DB (and its WAL/SHM sidecars) under
    the output root. Never touches the sessions tree — the DB lives beside the
    generated output, not inside the sources."""
    from .cache import get_cache_db_path

    db = get_cache_db_path(output_root)
    removed = False
    for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError as exc:
                click.echo(f"  Warning: failed to delete {path}: {exc}")
    click.echo(
        f"Cleared provider cache database: {db}"
        if removed
        else f"No provider cache database at {db}."
    )


def _clear_provider_output(output_root: Path, output_format: str) -> None:
    """Remove generated output files under the provider output root only.

    Scoped by known generated filenames (index + per-project
    ``combined_transcripts*`` / ``session-*``), so unrelated files — and every
    file in the sessions tree, which lives elsewhere — are left untouched."""
    file_ext = get_file_extension(output_format)
    removed = 0
    if output_root.is_dir():
        index_file = output_root / get_index_filename(output_format)
        if index_file.exists():
            index_file.unlink()
            removed += 1
        for project_dir in output_root.iterdir():
            if not project_dir.is_dir():
                continue
            for generated in _list_generated_outputs(project_dir, file_ext):
                generated.unlink()
                removed += 1
    click.echo(
        f"Cleared {removed} generated {file_ext.upper()} file(s) under {output_root}."
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
        faulthandler.register(sigusr1, all_threads=True, chain=False)  # ty: ignore[possibly-missing-attribute]
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


class DefaultCommandGroup(click.Group):
    """A group that falls back to a default subcommand.

    The CLI was a single flat command for its whole life, so
    ``claude-code-log``, ``claude-code-log some/file.jsonl`` and
    ``claude-code-log --from-date yesterday`` all have to keep working
    unchanged. Anything whose first argument isn't a registered subcommand
    is therefore rewritten as ``<default_command> <args...>``.

    A path that collides with a subcommand name can be forced through with
    ``--``: ``claude-code-log -- serve``.
    """

    default_command = "convert"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] == "--":
            # Explicit escape hatch: everything after `--` is the default
            # command's arguments, even if it looks like a subcommand name.
            args = [self.default_command] + args[1:]
        elif not args or args[0] not in self.commands:
            # No args at all, an INPUT_PATH, or an option like --help /
            # --version / --from-date: all belong to the default command.
            # Sending --help there is deliberate — it keeps the familiar
            # full option list, and `convert`'s epilog advertises the
            # subcommands (see _default_command_epilog).
            args = [self.default_command] + args
        return super().parse_args(ctx, args)


def _subcommand_epilog(group: click.Group) -> str:
    """Build the default command's epilog from what's actually registered.

    The group's own help is unreachable (``parse_args`` always rewrites to a
    subcommand), so the subcommand list has to appear in the default
    command's ``--help`` or subcommands would be undiscoverable. Deriving it
    from ``group.commands`` keeps it from advertising commands that don't
    exist. Called at the bottom of the module, once everything is registered.
    """
    others = [
        (name, cmd)
        for name, cmd in sorted(group.commands.items())
        if name != DefaultCommandGroup.default_command
    ]
    if not others:
        return ""
    width = max(len(name) for name, _ in others)
    # The leading \b stops Click's formatter rewrapping the block, so the
    # command list keeps its alignment.
    lines = ["\b", "Subcommands:"]
    lines += [
        f"  {name:<{width}}  {(cmd.short_help or cmd.help or '').splitlines()[0]}"
        for name, cmd in others
    ]
    lines += [
        "",
        "Run 'claude-code-log <subcommand> --help' for its options. Any "
        "other invocation runs the conversion described above.",
    ]
    return "\n".join(lines)


@click.group(cls=DefaultCommandGroup)
@click.version_option(version=get_library_version(), prog_name="claude-code-log")
def main() -> None:
    """Convert Claude transcript JSONL files to HTML or Markdown."""


@main.command(name="convert")
@click.version_option(version=get_library_version(), prog_name="claude-code-log")
@click.argument("input_path", type=click.Path(path_type=Path), required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help=(
        "Output destination. Use '-' (or /dev/stdout) to stream the "
        "rendered document to stdout (status goes to stderr) for piping. "
        "With a recognised file suffix (.html/.md/.markdown/.json) treated "
        "as a single output file; otherwise treated as a directory root "
        "(and now also honoured for --all-projects, where outputs land at "
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
        "combined is dead weight; note a prior 'yes' run's combined files are "
        "not deleted, use --clear-output to sweep them); "
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
    help="Output format. Supports html, md/markdown, or json. When omitted, "
    "inferred from the --output file suffix (.md/.markdown/.html/.json); "
    "otherwise defaults to html.",
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
    "--jobs",
    "-j",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Worker processes for converting projects in --all-projects mode "
        "(default: CPU count; 1 disables parallelism). Peak memory scales "
        "with jobs x the largest stale project. Also caps the per-project "
        "render fan-out, which is on by default and controlled by "
        "$CLAUDE_CODE_LOG_RENDER_JOBS (1 or 'off' to disable, auto, or a "
        "worker count)."
    ),
)
@click.option(
    "--provider",
    default=None,
    metavar="NAME",
    help="Load a single session from a registered provider (for example, codex).",
)
@click.option(
    "--session-id",
    default=None,
    help="Export a single session by ID (full ID or prefix). Project path is optional — looks up the session globally via cache.",
)
@click.option(
    "--depth",
    type=click.Choice(
        ["session", "user", "assistant", "agent", "tool", "hook"],
        case_sensitive=False,
    ),
    default=None,
    help=(
        "How deep into the message hierarchy to render "
        "(session > user > assistant > agent > tool > hook); output stops "
        "at the named level. DEFAULT: tool. "
        "session: session structure only (headers/nav); "
        "user: user prompts and steering only; "
        "assistant: user + assistant messages; "
        "agent: + sub-agents and key tool signals; "
        "tool: + tools, cleaned of system/hook noise (default); "
        "hook: everything, including hooks and system notices. "
        "Mutually exclusive with the deprecated --detail."
    ),
)
@click.option(
    "--detail",
    type=click.Choice(
        ["full", "high", "low", "minimal", "user-only"], case_sensitive=False
    ),
    default=None,
    help=(
        "DEPRECATED (removed in 2.0) — prefer --depth. Detail level for "
        "output. full (=--depth hook): everything; "
        "high (=--depth tool): detailed but cleaned (no system/hook noise); "
        "low (=--depth agent): interaction-focused + key signals; "
        "minimal (=--depth assistant): user + assistant messages only; "
        "user-only (=--depth user): only user prompts and steering."
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
    "--no-recaps",
    is_flag=True,
    help=(
        "Suppress '※ recap' (away-summary) messages. Recaps are otherwise "
        "shown at every depth level — they are themselves a high-level "
        "summary of activity (#179). Use this to get a 'really user-only' "
        "view (--depth user --no-recaps) or to drop the recap/agent "
        "redundancy at --depth assistant."
    ),
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Show full traceback on errors.",
)
@click.pass_context
def convert(
    ctx: click.Context,
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
    jobs: Optional[int],
    provider: Optional[str],
    session_id: Optional[str],
    depth: Optional[str],
    detail: Optional[str],
    compact: bool,
    git_link: Optional[str],
    no_timestamps: bool,
    no_recaps: bool,
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

    # Provider mode has three sub-modes:
    #   * export     — `--session-id <id>`: render one session by id
    #   * single-file — INPUT_PATH is a rollout FILE: render that one session
    #   * wholesale  — no id, and no INPUT_PATH (or an INPUT_PATH directory):
    #                  walk the whole sessions tree into a project hierarchy
    # Each rejects the flags that don't apply to it LOUDLY (never a silent
    # no-op); the matrix in test_codex_cli.py pins which combos are legal.
    provider_wholesale = (
        provider is not None
        and session_id is None
        and (input_path is None or input_path.is_dir())
    )
    if provider is not None:
        # Validate the provider name up front so an unknown one is a clean
        # UsageError (exit 2), consistent with the other flag errors, instead of
        # surfacing later as a broad-except "Error converting file" (exit 1).
        from .providers import discover_providers as _discover_providers

        _known = _discover_providers().get_all_providers()
        if provider not in _known:
            raise click.UsageError(
                f"Unknown provider: {provider}. Available providers: "
                f"{', '.join(_known) or 'none'}."
            )
        if input_path is not None and session_id is not None:
            raise click.UsageError(
                "--provider with an INPUT_PATH renders that path; drop "
                "--session-id (or drop the INPUT_PATH to export a session by id)."
            )
        # The TUI is always illegal in provider mode (provider TUI support is out
        # of scope, tracked in the backlog). --expand-paths/--filter-path used to
        # be always-illegal too ("Claude-only projection semantics"), but they are
        # well-defined for wholesale: provider projects are synthetic group-by-cwd,
        # so the group key IS the real cwd and the flat name expands unambiguously.
        # They stay illegal for single-session export (one session has no
        # multi-project projection to apply).
        conflicts: list[str] = []
        if tui:
            conflicts.append("--tui")
        if provider_wholesale:
            # Wholesale honors --expand-paths/--filter-path (Obsidian projection),
            # --combined, date range, -o/-f, --open-browser, and the cache flags
            # (--no-cache/--clear-cache/--clear-output). Only pagination
            # (--page-size) and job-parallelism (--jobs) remain deferred, so reject
            # those loudly rather than accept-and-ignore.
            if jobs is not None:
                conflicts.append("--jobs")
            if (
                ctx.get_parameter_source("page_size")
                is not click.core.ParameterSource.DEFAULT
            ):
                conflicts.append("--page-size")
        else:
            # export / single-file render one session; the wholesale-only flags
            # (multi-project hierarchy + projection, pagination, date range, cache)
            # don't apply.
            for enabled, flag in (
                (expand_paths, "--expand-paths"),
                (filter_path is not None, "--filter-path"),
                (all_projects, "--all-projects"),
                (projects_dir is not None, "--projects-dir"),
                (no_individual_sessions, "--no-individual-sessions"),
                (from_date is not None, "--from-date"),
                (to_date is not None, "--to-date"),
                (no_cache, "--no-cache"),
                (clear_cache, "--clear-cache"),
                (clear_output, "--clear-output"),
            ):
                if enabled:
                    conflicts.append(flag)
            for parameter, flag in (
                ("combined", "--combined"),
                ("page_size", "--page-size"),
            ):
                if (
                    ctx.get_parameter_source(parameter)
                    is not click.core.ParameterSource.DEFAULT
                ):
                    conflicts.append(flag)
        if conflicts:
            detail = (
                "provider wholesale rendering"
                if provider_wholesale
                else "provider single-session rendering (--session-id / a rollout file)"
            )
            raise click.UsageError(
                f"--provider does not support {', '.join(conflicts)} with {detail}."
            )

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

    # Provider wholesale honours --expand-paths/--filter-path (it defaults its
    # own output root and forwards the flags), so the Claude-path "these are
    # no-ops here" warnings would LIE for it — announce "ignoring" for a run that
    # actually projects. Exempt provider_wholesale from both; keep them verbatim
    # for every other mode, where they remain correct.
    will_run_all_projects = all_projects or input_path is None
    if (expand_paths or filter_path) and tui:
        click.echo(
            "Warning: --expand-paths / --filter-path are ignored in --tui mode.",
            err=True,
        )
    elif (
        (expand_paths or filter_path)
        and not will_run_all_projects
        and not provider_wholesale
    ):
        click.echo(
            "Warning: --expand-paths / --filter-path require --all-projects "
            "(or omitting INPUT_PATH); ignoring.",
            err=True,
        )
    elif (
        (expand_paths or filter_path)
        and not provider_wholesale
        and (output is None or _output_path_is_file(output))
    ):
        click.echo(
            "Warning: --expand-paths / --filter-path require --output to be a "
            "directory (no recognised file suffix); ignoring.",
            err=True,
        )

    # `--output` / `--format` are no-ops under --tui: the TUI's export
    # actions write to a fixed per-session path and run_session_browser
    # never receives these flags (issue #220). Warn rather than silently
    # ignore, mirroring the --expand-paths / --filter-path cases above.
    if tui and (
        output is not None
        or ctx.get_parameter_source("output_format")
        is not click.core.ParameterSource.DEFAULT
    ):
        click.echo(
            "Warning: --output / --format are ignored in --tui mode; "
            "use the TUI's in-app export actions instead.",
            err=True,
        )

    # Infer --format from an explicit --output file suffix when -f was not
    # given; error on an explicit conflict like `-o foo.md -f html` rather
    # than writing mismatched content (issue #222). `.md`/`.markdown` both
    # imply the canonical `markdown` format. Skipped under --tui: both flags
    # are no-ops there (warned above), so erroring on their conflict would
    # contradict the warning and block the TUI from launching (#220).
    if not tui and output is not None and _output_path_is_file(output):
        from .utils import format_from_output_suffix

        suffix_format = format_from_output_suffix(output)
        if suffix_format is not None:
            format_explicit = (
                ctx.get_parameter_source("output_format")
                is not click.core.ParameterSource.DEFAULT
            )
            canonical_format = (
                "markdown" if output_format in ("md", "markdown") else output_format
            )
            if not format_explicit:
                output_format = suffix_format
            elif canonical_format != suffix_format:
                raise click.UsageError(
                    f"--format {output_format} conflicts with the --output "
                    f"suffix '{output.suffix}' (implies {suffix_format}); "
                    "pass only one, or make them agree."
                )

    # Streaming the rendered document to stdout (`-o -`) is a single-document
    # mode; it can't express the multi-file --all-projects export (issue #223).
    # `--session-id` is exempt: it's a single-session export (resolved from
    # cache when no input path is given), which streams fine — so don't reject
    # it just because `input_path is None` makes will_run_all_projects true.
    if _is_stdout_target(output) and will_run_all_projects and session_id is None:
        raise click.UsageError(
            "--output - (stream to stdout) is not supported with --all-projects; "
            "pass a single transcript file, directory, or --session-id."
        )

    # `--combined no` asks to skip the combined transcript (per-session files
    # only); stdout can carry only one document, so streaming forces the
    # combined doc — fail fast rather than silently doing the opposite (#223).
    if _is_stdout_target(output) and not write_combined:
        raise click.UsageError(
            "--combined no is incompatible with --output - (stream to stdout), "
            "which emits a single combined document."
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

    from .models import DEFAULT_DEPTH, DETAIL_ALIASES, RenderingDepth

    # Resolve the RenderingDepth from --depth (preferred) or the deprecated
    # --detail (#159). Both default to None so an explicit choice is
    # detectable; they are mutually exclusive. The --depth names ARE the
    # RenderingDepth values; --detail's legacy names map via DETAIL_ALIASES.
    if depth is not None and detail is not None:
        raise click.UsageError(
            "--depth and --detail are mutually exclusive; --detail is the "
            "deprecated alias — prefer --depth."
        )
    if detail is not None:
        click.echo(
            "Warning: --detail is deprecated and will be removed in 2.0; "
            "prefer --depth (session|user|assistant|agent|tool|hook).",
            err=True,
        )
        depth_level = DETAIL_ALIASES[detail.lower()]
    elif depth is not None:
        depth_level = RenderingDepth(depth.lower())
    else:
        depth_level = DEFAULT_DEPTH

    try:
        if provider is not None:
            from .providers import SessionInfo, discover_providers

            # Wholesale: no --session-id, and either no INPUT_PATH (walk the
            # provider's data dir) or an INPUT_PATH directory / --projects-dir
            # (a mini sessions root). Renders the whole project hierarchy.
            if provider_wholesale:
                sessions_root = (
                    input_path
                    if input_path is not None
                    else projects_dir
                    if projects_dir is not None
                    else None
                )
                _run_provider_wholesale(
                    provider,
                    sessions_root,
                    output,
                    output_format,
                    image_export_mode,
                    depth_level,
                    compact,
                    no_timestamps,
                    no_recaps,
                    write_combined,
                    write_individual,
                    from_date,
                    to_date,
                    no_cache,
                    clear_cache,
                    clear_output,
                    open_browser,
                    expand_paths,
                    filter_path,
                )
                return

            # Explicit --provider with an INPUT_PATH FILE renders that file
            # directly (distinct from --session-id export below). The fence
            # guarantees not both id and INPUT_PATH are set.
            if input_path is not None:
                _render_provider_input_file(
                    provider,
                    input_path,
                    output,
                    output_format,
                    image_export_mode,
                    depth_level,
                    compact,
                    no_timestamps,
                    no_recaps,
                    open_browser,
                )
                return

            assert session_id is not None  # fence guarantees this in export mode
            provider_session_id = session_id
            registry = discover_providers()
            selected = registry.get_provider(provider)
            if selected is None:
                raise ValueError(f"Unknown provider: {provider}")
            if not selected.is_available():
                raise ValueError(f"Provider {provider} is not available")

            sessions_by_id: dict[str, list[SessionInfo]] = {}
            for info in selected.discover_sessions():
                sessions_by_id.setdefault(info.session_id, []).append(info)
            if provider_session_id in sessions_by_id:
                if len(sessions_by_id[provider_session_id]) != 1:
                    raise ValueError(
                        f"Duplicate session ID '{provider_session_id}' for provider {provider}"
                    )
                matched_id = provider_session_id
            else:
                matches = sorted(
                    sid for sid in sessions_by_id if sid.startswith(provider_session_id)
                )
                if not matches:
                    raise ValueError(
                        f"Session '{provider_session_id}' not found for provider {provider}"
                    )
                if len(matches) > 1:
                    raise ValueError(
                        f"Ambiguous session ID prefix '{provider_session_id}' matches: "
                        + ", ".join(matches)
                    )
                matched_id = matches[0]

            if len(sessions_by_id[matched_id]) != 1:
                raise ValueError(
                    f"Duplicate session ID '{matched_id}' for provider {provider}"
                )

            info = sessions_by_id[matched_id][0]
            messages = list(selected.load_session(matched_id))
            title = info.title or f"{provider.title()}: Session {matched_id[:8]}"

            def render_provider(destination: Path) -> Path:
                return render_normalized_session_file(
                    messages,
                    matched_id,
                    destination,
                    output_format,
                    title,
                    image_export_mode,
                    depth_level,
                    compact,
                    no_timestamps,
                    no_recaps,
                )

            if _is_stdout_target(output):
                _render_to_stdout(
                    Path(f"{provider}:{matched_id}"),
                    lambda tmpdir: render_provider(
                        tmpdir / f"session.{get_file_extension(output_format)}"
                    ),
                )
                return

            filename = f"session-{matched_id}.{get_file_extension(output_format)}"
            if output is None:
                destination = Path.cwd() / filename
            elif _output_path_is_file(output):
                destination = output
            else:
                destination = output / filename
            output_path = render_provider(destination)
            click.echo(f"Successfully exported {provider} session to {output_path}")
            if open_browser:
                click.launch(str(output_path))
            return

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

            if _is_stdout_target(output):
                # Stream a single session to stdout (issue #223): render to a
                # throwaway file (no cache, embedded images), copy to stdout.
                session_input = input_path
                _render_to_stdout(
                    session_input,
                    lambda tmpdir: generate_single_session_file(
                        output_format,
                        session_input,
                        session_id,
                        tmpdir / f"session.{get_file_extension(output_format)}",
                        False,  # use_cache: one-off stream, don't touch cache
                        "embedded",  # inline images; the temp dir is discarded
                        depth=depth_level,
                        compact=compact,
                        no_timestamps=no_timestamps,
                        no_recaps=no_recaps,
                    ),
                )
                return

            output_path = generate_single_session_file(
                output_format,
                input_path,
                session_id,
                output,
                not no_cache,
                image_export_mode,
                depth=depth_level,
                compact=compact,
                no_timestamps=no_timestamps,
                no_recaps=no_recaps,
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
                depth=depth_level,
                compact=compact,
                output_dir=output_dir_for_projects,
                expand_paths=expand_paths,
                filter_path=filter_path,
                write_combined=write_combined,
                no_timestamps=no_timestamps,
                no_recaps=no_recaps,
                jobs=jobs,
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

        # Provider auto-detection (silent-empty pin): a rollout handed as an
        # INPUT_PATH must route to the provider pipeline, not the Claude parser,
        # which skips every record and renders a near-empty page. A single file
        # renders that session; a DIRECTORY of rollouts renders the whole tree
        # via the wholesale walker — either way it never falls to the empty parse.
        if provider is None and input_path.exists():
            from .providers import discover_providers

            detected = discover_providers().detect_provider_for_path(input_path)
            if detected is not None:
                if input_path.is_dir():
                    _run_provider_wholesale(
                        detected,
                        input_path,
                        output,
                        output_format,
                        image_export_mode,
                        depth_level,
                        compact,
                        no_timestamps,
                        no_recaps,
                        write_combined,
                        write_individual,
                        from_date,
                        to_date,
                        no_cache,
                        clear_cache,
                        clear_output,
                        open_browser,
                        expand_paths,
                        filter_path,
                    )
                    return
                _render_provider_input_file(
                    detected,
                    input_path,
                    output,
                    output_format,
                    image_export_mode,
                    depth_level,
                    compact,
                    no_timestamps,
                    no_recaps,
                    open_browser,
                )
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
                # Route to stderr when streaming so the document stream stays
                # clean (issue #223); normal runs keep this on stdout as before.
                click.echo(
                    f"Converting project path {input_path} to {claude_path}",
                    err=_is_stdout_target(output),
                )
                input_path = claude_path
            elif not input_path.exists():
                # Original path doesn't exist and conversion failed
                raise FileNotFoundError(
                    f"Neither {input_path} nor {claude_path} exists"
                )

        if _is_stdout_target(output):
            # Stream the combined document to stdout (issue #223): render to a
            # throwaway file (no cache so no pagination, embedded images, always
            # regenerate, no individual session files), then copy to stdout.
            stream_input = input_path
            _render_to_stdout(
                stream_input,
                lambda tmpdir: convert_jsonl_to(
                    output_format,
                    stream_input,
                    tmpdir / f"stream.{get_file_extension(output_format)}",
                    from_date,
                    to_date,
                    generate_individual_sessions=False,
                    use_cache=False,
                    silent=True,
                    image_export_mode="embedded",
                    page_size=page_size,
                    depth=depth_level,
                    compact=compact,
                    update_cache=False,
                    write_combined=True,
                    no_timestamps=no_timestamps,
                    no_recaps=no_recaps,
                    force_regenerate=True,
                ),
            )
            return

        # Out-param: convert_jsonl_to reports what it actually (re)wrote —
        # the combined output and/or how many session files — so we don't
        # print a success line on top of its own "is current, skipping
        # regeneration" line, and don't claim to have "combined" anything
        # when only session files were written.
        report = RegenerationReport()
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
            depth=depth_level,
            compact=compact,
            # User's `-o` path is a one-off export, not a cached artifact:
            # don't occupy a cache slot keyed by an arbitrary destination.
            update_cache=output is None,
            write_combined=write_combined,
            no_timestamps=no_timestamps,
            no_recaps=no_recaps,
            # An explicit `-o` *file* always regenerates: the version-marker
            # skip only knows the embedded version, not which source produced
            # the file, so it would keep stale content at a user-chosen path
            # (issue #221). Scoped to file destinations — directory exports
            # keep the cache's per-source incremental skip (is_transcript_stale),
            # which already tracks the source (a different transcript to the
            # same dir still regenerates), and `--all-projects` calls this
            # with output=None anyway, so its skip is never forced.
            force_regenerate=output is not None and _output_path_is_file(output),
            report=report,
            # A single-project conversion has the whole machine to itself,
            # so `--jobs` is the cap for its render fan-out. Whether that
            # fan-out runs at all is decided by
            # $CLAUDE_CODE_LOG_RENDER_JOBS, which is off by default —
            # `render_jobs=None` consults it, and the min() keeps an
            # explicit `--jobs` as the ceiling.
            render_jobs=(
                min(jobs, resolve_render_jobs(None)) if jobs is not None else None
            ),
        )
        # Report only work actually done this run. On a pure skip the converter
        # already printed its "... is current, skipping regeneration" line, so
        # print nothing more. Otherwise gate the wording on WHICH output was
        # (re)written: "combined" only when the combined transcript itself was,
        # and the per-session count only for sessions rewritten this run — so
        # we never claim to have combined something we skipped.
        combined_written = report.combined_regenerated
        sessions_written = report.sessions_regenerated
        if not combined_written and not sessions_written:
            pass
        elif input_path.is_file():
            click.echo(f"Successfully converted {input_path} to {output_path}")
        else:
            jsonl_count = len(list(input_path.glob("*.jsonl")))
            session_suffix = (
                f" and generated {sessions_written} individual session files"
                if sessions_written
                else ""
            )
            if combined_written:
                click.echo(
                    f"Successfully combined {jsonl_count} transcript files "
                    f"from {input_path} to {output_path}{session_suffix}"
                )
            else:
                # The combined transcript was skipped as current (or never
                # requested, e.g. `--combined no`); only per-session files were
                # written. Don't claim to have "combined" anything or name a
                # combined file that wasn't produced this run.
                click.echo(
                    f"Successfully processed {jsonl_count} transcript files "
                    f"from {input_path}{session_suffix}"
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


# Registered last, once every subcommand exists, so the default command's
# --help advertises exactly the subcommands that are really available.
@main.command(name="serve")
@click.option(
    "--port",
    type=int,
    default=8010,
    show_default=True,
    help="Port to listen on. Use 0 to pick a free one.",
)
@click.option(
    "--projects-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help=(
        "Projects directory to serve. Defaults to ~/.claude/projects "
        "(the same directory the conversion uses)."
    ),
)
@click.option(
    "--no-convert",
    is_flag=True,
    default=False,
    help=(
        "Skip the startup conversion and serve whatever HTML is already "
        "there. Faster to start; pages may be stale."
    ),
)
@click.option(
    "--open-browser",
    is_flag=True,
    default=False,
    help="Open the index page in a browser once the server is up.",
)
@click.option(
    "--search-fields",
    envvar=ENV_SEARCH_FIELDS,
    help=(
        "Which field groups archive search looks in by default. "
        f"Groups: {', '.join(SEARCH_FIELDS)}. Accepts an absolute list "
        "('text,thinking'), 'all'/'none', or deltas against the default "
        "('+tool_result', '-thinking'). Default excludes tool_result: it is "
        "69% of a typical archive's text and mostly file dumps. "
        f"Env: {ENV_SEARCH_FIELDS}."
    ),
)
@click.option(
    "--index-fields",
    envvar=ENV_INDEX_FIELDS,
    help=(
        "Which field groups get indexed at all. Defaults to everything, so "
        "enabling a group at search time never needs a reindex. Narrowing "
        "this shrinks the index (dropping tool_result took a real 253 MB "
        f"index to 94 MB) but forces a rebuild. Env: {ENV_INDEX_FIELDS}."
    ),
)
@click.option(
    "--reindex",
    is_flag=True,
    default=False,
    help="Rebuild the search index from scratch instead of updating it.",
)
@click.option(
    "--no-index",
    is_flag=True,
    default=False,
    help=(
        "Start without building or updating the search index. The search "
        "page will report the index as unavailable."
    ),
)
@click.option(
    "--watch",
    "watch_sources",
    is_flag=True,
    default=False,
    help=(
        "Keep the served pages current: re-convert whenever a transcript "
        "changes, in the background. Reload a page to see new messages."
    ),
)
def serve(
    port: int,
    projects_dir: Optional[Path],
    no_convert: bool,
    open_browser: bool,
    search_fields: Optional[str],
    index_fields: Optional[str],
    reindex: bool,
    no_index: bool,
    watch_sources: bool,
) -> None:
    """Serve the projects directory over loopback, with full-archive search.

    The generated HTML stays canonical and keeps working from `file://`;
    this adds an origin, which is what full-archive search needs in order to
    reach the SQLite cache.
    """
    from .api import SearchApi
    from .search import (
        DEFAULT_INDEX_FIELDS,
        DEFAULT_SEARCH_FIELDS,
        fts5_available,
        parse_field_spec,
    )
    from .server import ArchiveServer

    projects_path = projects_dir or get_default_projects_dir()
    if not projects_path.exists():
        click.echo(f"Error: projects directory not found: {projects_path}", err=True)
        sys.exit(1)

    try:
        resolved_search_fields = parse_field_spec(search_fields, DEFAULT_SEARCH_FIELDS)
        resolved_index_fields = parse_field_spec(index_fields, DEFAULT_INDEX_FIELDS)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    if not no_convert:
        # Same conversion the default command runs, so the pages being served
        # are current. --no-convert skips it for a fast start.
        click.echo(f"Refreshing {projects_path}...")
        process_projects_hierarchy(projects_path, silent=True)

    db_path = get_cache_db_path(projects_path)
    if not no_index:
        _build_search_index(db_path, resolved_index_fields, rebuild=reindex)
    elif not db_path.exists():
        click.echo("No cache database found; search will be unavailable.", err=True)

    api = SearchApi(db_path, default_fields=resolved_search_fields)
    server = ArchiveServer(
        projects_path, api_routes=api.routes(), host="127.0.0.1", port=port
    )
    click.echo(f"Serving {projects_path}")
    click.echo(f"  {server.url}/index.html")
    click.echo(f"  {server.url}/search.html")
    if not no_index and not fts5_available(sqlite3.connect(":memory:")):
        click.echo(
            "  (this SQLite has no FTS5, so archive search is unavailable)", err=True
        )
    click.echo("Press Ctrl+C to stop.")

    if open_browser:
        click.launch(f"{server.url}/index.html")

    watch_stop: Optional[threading.Event] = None
    watch_thread: Optional[threading.Thread] = None
    if watch_sources:
        from .watch import WatchEngine
        from .entry_store import ParsedEntryStore, entry_store_enabled

        # One store for the whole serve, not one per tick, for the same
        # reason `watch` owns one: it lets a tick resume its parse from
        # the bytes the previous tick already read. Only the *inline*
        # conversion path takes it (see `_convert_plan_inline`), which is
        # the steady state here -- one live project stale per tick means
        # `resolved_jobs == 1` and no pool.
        serve_store = ParsedEntryStore() if entry_store_enabled() else None

        def reconvert(_changed: set[Path]) -> None:
            # The server never renders. It re-runs the ordinary conversion
            # and lets the pages on disk stay canonical, so a page served
            # over http and the same file opened from file:// can never
            # disagree.
            #
            # `write_combined=False` for the same reason the `watch`
            # command defaults to it: regenerating the combined pages is
            # what forces a tick to reload the project rather than just
            # the session that grew. Measured on a 302-session/373MB
            # archive, one appended line cost 7.6s with the combined
            # pages and 0.7s without. The combined pages written by the
            # startup conversion below stay on disk and keep serving --
            # they just stop tracking the live session until the next
            # restart, which is the trade `watch` already makes.
            process_projects_hierarchy(
                projects_path,
                silent=True,
                write_combined=False,
                entry_store=serve_store,
            )

        def report(exc: BaseException) -> None:
            click.echo(f"  watch: conversion failed: {exc!r}", err=True)

        engine = WatchEngine([projects_path], reconvert, on_error=report)
        # Prime before starting the thread so the baseline is taken at a
        # known moment rather than whenever the thread gets scheduled.
        engine.prime()
        watch_stop = threading.Event()
        watch_thread = engine.run_in_thread(watch_stop)
        click.echo(
            "  watching for changes (reload a session page to see new messages;"
            " combined pages refresh on restart)"
        )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopping...")
    finally:
        if watch_stop is not None:
            watch_stop.set()
        if watch_thread is not None:
            watch_thread.join(timeout=5)
        server.stop()


def _build_search_index(
    db_path: Path, index_fields: tuple[str, ...], *, rebuild: bool
) -> None:
    """Bring the FTS index up to date, with a progress bar.

    Blocking rather than backgrounded, deliberately. A full build is ~50 s
    for a 532k-message archive and only happens once; keeping it in the
    foreground avoids two problems a background build would create — SQLite
    has a single writer, so it would contend with the converter, and a
    partially built index returns *wrong* answers (a silent subset) rather
    than slow ones.
    """
    from .search import ensure_index, fts5_available

    if not db_path.exists():
        click.echo("No cache database found; search will be unavailable.", err=True)
        return

    conn = sqlite3.connect(db_path)
    try:
        if not fts5_available(conn):
            click.echo(
                "This SQLite build has no FTS5; archive search is unavailable.",
                err=True,
            )
            return
        bar: Optional[Any] = None

        def report(done: int, total: int) -> None:
            nonlocal bar
            if total == 0:
                return
            if bar is None:
                bar = click.progressbar(
                    length=total, label="Indexing transcripts for search"
                )
                bar.__enter__()
            bar.update(1)

        try:
            status = ensure_index(
                conn, index_fields=index_fields, progress=report, rebuild=rebuild
            )
        except sqlite3.DatabaseError as e:
            if not is_corrupt_database_error(e):
                raise
            # The ordinary conversion heals a corrupt cache when it builds a
            # CacheManager, so the only way to arrive here is --no-convert,
            # which skipped it. Don't delete the database behind the user's
            # back on the one flag that asked us to touch nothing; serving
            # the pages without search beats refusing to start.
            if bar is not None:
                bar.__exit__(None, None, None)
            click.echo(f"Cache database is corrupt ({e}): {db_path}", err=True)
            click.echo(
                "  Search is unavailable. Re-run without --no-convert to "
                "rebuild the cache.",
                err=True,
            )
            return
        if bar is not None:
            bar.__exit__(None, None, None)
            click.echo(
                f"Search index ready: {status.indexed_messages:,} messages "
                f"({', '.join(status.fields)})."
            )
    finally:
        conn.close()


@main.command(name="watch")
@click.argument(
    "input_path",
    type=click.Path(exists=True, path_type=Path, file_okay=False),
    required=False,
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help=(
        "Output destination, as for `convert`. Pair with --format md to "
        "keep an Obsidian vault current."
    ),
)
@click.option(
    "-f",
    "--format",
    "output_format",
    type=click.Choice(["html", "md", "markdown"]),
    default="html",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--combined",
    type=click.Choice(["yes", "no", "only"]),
    default="no",
    show_default=True,
    help=(
        "As for `convert`, but defaulting to 'no'. Per-session files are "
        "what a watch is for, and skipping the combined page is what lets "
        "a tick regenerate just the changed session instead of reloading "
        "the whole project."
    ),
)
@click.option(
    "--projects-dir",
    type=click.Path(exists=True, path_type=Path, file_okay=False),
    help="Projects directory (default: ~/.claude/projects/).",
)
@click.option(
    "--all-projects",
    is_flag=True,
    default=False,
    help=(
        "Watch every project instead of one. Off by default: a tick over a "
        "large archive is far more expensive than over a single project."
    ),
)
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_POLL_INTERVAL,
    show_default=True,
    help="Seconds between filesystem polls.",
)
@click.option(
    "--quiet-period",
    type=float,
    default=DEFAULT_QUIET_PERIOD,
    show_default=True,
    help=(
        "Seconds of no further change before converting. Claude Code writes "
        "several entries per turn; without this every one would trigger its "
        "own render."
    ),
)
@click.option(
    "--max-latency",
    type=float,
    default=DEFAULT_MAX_LATENCY,
    show_default=True,
    help=(
        "Convert anyway after this long, so an unbroken stream of appends "
        "still surfaces instead of starving behind the quiet period."
    ),
)
@click.option("--debug", is_flag=True, default=False, help="Show full tracebacks.")
def watch(
    input_path: Optional[Path],
    output: Optional[Path],
    output_format: str,
    combined: str,
    projects_dir: Optional[Path],
    all_projects: bool,
    interval: float,
    quiet_period: float,
    max_latency: float,
    debug: bool,
) -> None:
    """Re-convert transcripts as they change, until interrupted.

    Points at one project by default -- the one for the current directory
    if it has transcripts, otherwise the given INPUT_PATH. Watching the
    whole archive is available via --all-projects but is rarely what you
    want: a tick's cost scales with the project, and only one project is
    ever being written to.

    The generated files on disk stay canonical, so anything that reloads
    them picks the changes up: an editor or Obsidian for Markdown, a
    browser refresh for HTML.
    """
    from .watch import WatchEngine

    projects_path = projects_dir or get_default_projects_dir()
    root = _resolve_watch_root(input_path, projects_path, all_projects)
    if root is None:
        # `raise` rather than `sys.exit` so the type checkers can see that
        # `root` is a Path from here on.
        raise SystemExit(1)

    fmt = "markdown" if output_format == "md" else output_format
    write_combined = combined != "no"
    individual = combined != "only"

    # One store for the whole watch, not one per tick: it lets each tick
    # resume its parse from the bytes the previous tick already read,
    # instead of re-reading a growing session's whole history every time
    # a line lands (entry_store.py, work/watch-mode.md Fix B). Only the
    # single-project path takes one — `--all-projects` re-converts a
    # hierarchy and has no single growing file to follow.
    from .entry_store import ParsedEntryStore, entry_store_enabled

    store = ParsedEntryStore() if (entry_store_enabled() and not all_projects) else None

    def convert(_changed: set[Path]) -> None:
        started = time.monotonic()
        if all_projects:
            process_projects_hierarchy(
                root,
                silent=True,
                output_format=fmt,
                output_dir=output,
                write_combined=write_combined,
                generate_individual_sessions=individual,
            )
        else:
            convert_jsonl_to(
                fmt,
                root,
                # `output_root` is the directory form; leaving output_path
                # unset lets the converter derive the filenames, which is
                # what keeps the destination-aware freshness check working.
                output_root=output,
                silent=True,
                write_combined=write_combined,
                generate_individual_sessions=individual,
                entry_store=store,
            )
        click.echo(
            f"  {time.strftime('%H:%M:%S')}  converted in "
            f"{time.monotonic() - started:.2f}s"
        )

    def report(exc: BaseException) -> None:
        if debug:
            traceback.print_exc()
        click.echo(f"  conversion failed: {exc!r}", err=True)

    engine = WatchEngine(
        [root],
        convert,
        poll_interval=interval,
        quiet_period=quiet_period,
        max_latency=max_latency,
        on_error=report,
    )

    click.echo(f"Watching {root}")
    click.echo("Press Ctrl+C to stop.")
    # Convert once up front so the output is current before the first
    # change, then prime -- priming after means our own output writes are
    # already in the baseline and can't trigger a spurious first tick.
    #
    # A failure here is a misconfigured watch, not a transient one: the
    # root doesn't exist, or `--all-projects` was pointed at a single
    # project rather than an archive. `report` is for the per-tick case
    # where the loop should carry on; this one is fatal, and gets the
    # same one-line diagnosis `convert` gives rather than a traceback.
    try:
        convert(set())
    except Exception as e:
        if debug:
            traceback.print_exc()
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    engine.prime()
    try:
        engine.run()
    except KeyboardInterrupt:
        click.echo("\nStopping.")
    click.echo(
        f"{engine.stats.conversions} conversions, "
        f"{engine.stats.polls} polls, {engine.stats.errors} errors."
    )


def _resolve_watch_root(
    input_path: Optional[Path], projects_path: Path, all_projects: bool
) -> Optional[Path]:
    """Pick the directory to watch, or report why we can't.

    Order: an explicit path wins; --all-projects means the hierarchy;
    otherwise the project for the current directory, which is what someone
    running this alongside a live session almost always means.
    """
    if input_path is not None:
        return input_path
    if all_projects:
        return projects_path

    from .utils import real_path_to_project_dirname

    encoded = real_path_to_project_dirname(Path.cwd())
    candidate = projects_path / encoded
    if candidate.is_dir():
        return candidate

    click.echo(
        f"No transcripts found for the current directory ({Path.cwd()}).\n"
        f"  Looked for: {candidate}\n"
        "  Pass a project directory explicitly, or use --all-projects.",
        err=True,
    )
    return None


convert.epilog = _subcommand_epilog(main)


if __name__ == "__main__":
    main()
