#!/usr/bin/env python3
"""CLI interface for claude-code-log."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional, List

import click
from git import Repo, InvalidGitRepositoryError

from .converter import convert_jsonl_to_html, process_projects_hierarchy
from .cache import CacheManager, get_library_version


def _launch_tui_with_cache_check(project_path: Path) -> Optional[str]:
    """Launch TUI with proper cache checking and user feedback."""
    click.echo("Checking cache and loading session data...")

    # Check if we need to rebuild cache
    cache_manager = CacheManager(project_path, get_library_version())
    jsonl_files = list(project_path.glob("*.jsonl"))
    modified_files = cache_manager.get_modified_files(jsonl_files)
    project_cache = cache_manager.get_cached_project_data()

    if not (project_cache and project_cache.sessions and not modified_files):
        # Need to rebuild cache
        if modified_files:
            click.echo(
                f"Found {len(modified_files)} modified files, rebuilding cache..."
            )
        else:
            click.echo("Building session cache...")

        # Pre-build the cache before launching TUI
        try:
            convert_jsonl_to_html(project_path, silent=True)
            click.echo("Cache ready! Launching TUI...")
        except Exception as e:
            click.echo(f"Error building cache: {e}", err=True)
            return
    else:
        click.echo(
            f"Cache up to date. Found {len(project_cache.sessions)} sessions. Launching TUI..."
        )

    # Small delay to let user see the message before TUI clears screen
    import time

    time.sleep(0.5)

    from .tui import run_session_browser

    result = run_session_browser(project_path)
    return result


def convert_project_path_to_claude_dir(input_path: Path) -> Path:
    """Convert a project path to the corresponding directory in ~/.claude/projects/."""
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

    # Construct the path in ~/.claude/projects/
    claude_projects_dir = Path.home() / ".claude" / "projects" / claude_project_name

    return claude_projects_dir


def find_projects_by_cwd(
    projects_dir: Path, current_cwd: Optional[str] = None
) -> List[Path]:
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
    exact_matches = _find_exact_matches(project_dirs, current_cwd_path)
    if exact_matches:
        return exact_matches

    # Tier 2: Check if we're inside a git repo and match to repo root
    git_root_matches = _find_git_root_matches(project_dirs, current_cwd_path)
    if git_root_matches:
        return git_root_matches

    # Tier 3: Fall back to relative path matching
    return _find_relative_matches(project_dirs, current_cwd_path)


def _find_exact_matches(project_dirs: List[Path], current_cwd_path: Path) -> List[Path]:
    """Find projects with exact working directory matches using path-based matching."""
    expected_project_dir = convert_project_path_to_claude_dir(current_cwd_path)

    for project_dir in project_dirs:
        if project_dir == expected_project_dir:
            return [project_dir]

    return []


def _find_git_root_matches(
    project_dirs: List[Path], current_cwd_path: Path
) -> List[Path]:
    """Find projects that match the git repository root using path-based matching."""
    try:
        # Check if we're inside a git repository
        repo = Repo(current_cwd_path, search_parent_directories=True)
        git_root_path = Path(repo.git_dir).parent.resolve()

        # Find projects that match the git root
        return _find_exact_matches(project_dirs, git_root_path)
    except InvalidGitRepositoryError:
        # Not in a git repository
        return []
    except Exception:
        # Other git-related errors
        return []


def _find_relative_matches(
    project_dirs: List[Path], current_cwd_path: Path
) -> List[Path]:
    """Find projects using relative path matching (original behavior)."""
    relative_matches: List[Path] = []

    for project_dir in project_dirs:
        try:
            # Load cache to check for working directories
            cache_manager = CacheManager(project_dir, get_library_version())
            project_cache = cache_manager.get_cached_project_data()

            # Build cache if needed
            if not project_cache or not project_cache.working_directories:
                jsonl_files = list(project_dir.glob("*.jsonl"))
                if jsonl_files:
                    try:
                        convert_jsonl_to_html(project_dir, silent=True)
                        project_cache = cache_manager.get_cached_project_data()
                    except Exception as e:
                        logging.warning(
                            f"Failed to build cache for project {project_dir.name}: {e}"
                        )
                        project_cache = None

            if project_cache and project_cache.working_directories:
                # Check for relative matches
                for cwd in project_cache.working_directories:
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
            project_dirs = [
                d
                for d in input_path.iterdir()
                if d.is_dir() and list(d.glob("*.jsonl"))
            ]

            for project_dir in project_dirs:
                try:
                    cache_manager = CacheManager(project_dir, library_version)
                    cache_manager.clear_cache()
                    click.echo(f"  Cleared cache for {project_dir.name}")
                except Exception as e:
                    click.echo(
                        f"  Warning: Failed to clear cache for {project_dir.name}: {e}"
                    )

        elif input_path.is_dir():
            # Clear cache for single directory
            click.echo(f"Clearing cache for {input_path}...")
            cache_manager = CacheManager(input_path, library_version)
            cache_manager.clear_cache()
        else:
            # Single file - no cache to clear
            click.echo("Cache clearing not applicable for single files.")

    except Exception as e:
        click.echo(f"Warning: Failed to clear cache: {e}")


def _clear_html_files(input_path: Path, all_projects: bool) -> None:
    """Clear HTML files for the specified path."""
    try:
        if all_projects:
            # Clear HTML files for all project directories
            click.echo("Clearing HTML files for all projects...")
            project_dirs = [
                d
                for d in input_path.iterdir()
                if d.is_dir() and list(d.glob("*.jsonl"))
            ]

            total_removed = 0
            for project_dir in project_dirs:
                try:
                    # Remove HTML files in project directory
                    html_files = list(project_dir.glob("*.html"))
                    for html_file in html_files:
                        html_file.unlink()
                        total_removed += 1

                    if html_files:
                        click.echo(
                            f"  Removed {len(html_files)} HTML files from {project_dir.name}"
                        )
                except Exception as e:
                    click.echo(
                        f"  Warning: Failed to clear HTML files for {project_dir.name}: {e}"
                    )

            # Also remove top-level index.html
            index_file = input_path / "index.html"
            if index_file.exists():
                index_file.unlink()
                total_removed += 1
                click.echo("  Removed top-level index.html")

            if total_removed > 0:
                click.echo(f"Total: Removed {total_removed} HTML files")
            else:
                click.echo("No HTML files found to remove")

        elif input_path.is_dir():
            # Clear HTML files for single directory
            click.echo(f"Clearing HTML files for {input_path}...")
            html_files = list(input_path.glob("*.html"))
            for html_file in html_files:
                html_file.unlink()

            if html_files:
                click.echo(f"Removed {len(html_files)} HTML files")
            else:
                click.echo("No HTML files found to remove")
        else:
            # Single file - remove corresponding HTML file
            html_file = input_path.with_suffix(".html")
            if html_file.exists():
                html_file.unlink()
                click.echo(f"Removed {html_file}")
            else:
                click.echo("No corresponding HTML file found to remove")

    except Exception as e:
        click.echo(f"Warning: Failed to clear HTML files: {e}")


@click.command()
@click.argument("input_path", type=click.Path(path_type=Path), required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output HTML file path (default: input file with .html extension or combined_transcripts.html for directories)",
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
    help="Skip generating individual session HTML files (only create combined transcript)",
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
    "--clear-html",
    is_flag=True,
    help="Clear all HTML files and force regeneration",
)
@click.option(
    "--tui",
    is_flag=True,
    help="Launch interactive TUI for session browsing and management",
)
def main(
    input_path: Optional[Path],
    output: Optional[Path],
    open_browser: bool,
    from_date: Optional[str],
    to_date: Optional[str],
    all_projects: bool,
    no_individual_sessions: bool,
    no_cache: bool,
    clear_cache: bool,
    clear_html: bool,
    tui: bool,
) -> None:
    """Convert Claude transcript JSONL files to HTML.

    INPUT_PATH: Path to a Claude transcript JSONL file, directory containing JSONL files, or project path to convert. If not provided, defaults to ~/.claude/projects/ and --all-projects is used.
    """
    # Configure logging to show warnings and above
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        # Handle TUI mode
        if tui:
            # Handle default case for TUI - use ~/.claude/projects if no input path
            if input_path is None:
                input_path = Path.home() / ".claude" / "projects"

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

                project_dirs = [
                    d
                    for d in input_path.iterdir()
                    if d.is_dir() and list(d.glob("*.jsonl"))
                ]

                if not project_dirs:
                    click.echo(f"No projects with JSONL files found in {input_path}")
                    return

                # Try to find projects that match current working directory
                matching_projects = find_projects_by_cwd(input_path)

                if len(project_dirs) == 1:
                    # Only one project, open it directly
                    result = _launch_tui_with_cache_check(project_dirs[0])
                    if result == "back_to_projects":
                        # User wants to see project selector even though there's only one project
                        from .tui import run_project_selector

                        while True:
                            selected_project = run_project_selector(
                                project_dirs, matching_projects
                            )
                            if not selected_project:
                                # User cancelled
                                return

                            result = _launch_tui_with_cache_check(selected_project)
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
                            selected_project = run_project_selector(
                                project_dirs, matching_projects
                            )
                            if not selected_project:
                                # User cancelled
                                return

                            result = _launch_tui_with_cache_check(selected_project)
                            if result != "back_to_projects":
                                # User quit normally
                                return
                    return
                else:
                    # Multiple projects or multiple matching projects - show selector
                    from .tui import run_project_selector

                    while True:
                        selected_project = run_project_selector(
                            project_dirs, matching_projects
                        )
                        if not selected_project:
                            # User cancelled
                            return

                        result = _launch_tui_with_cache_check(selected_project)
                        if result != "back_to_projects":
                            # User quit normally
                            return
            else:
                # Single project directory
                _launch_tui_with_cache_check(input_path)
                return

        # Handle default case - process all projects hierarchy if no input path and --all-projects flag
        if input_path is None:
            input_path = Path.home() / ".claude" / "projects"
            all_projects = True

        # Handle cache clearing
        if clear_cache:
            _clear_caches(input_path, all_projects)
            if clear_cache and not (from_date or to_date or input_path.is_file()):
                # If only clearing cache, exit after clearing
                click.echo("Cache cleared successfully.")
                return

        # Handle HTML files clearing
        if clear_html:
            _clear_html_files(input_path, all_projects)
            if clear_html and not (from_date or to_date or input_path.is_file()):
                # If only clearing HTML files, exit after clearing
                click.echo("HTML files cleared successfully.")
                return

        # Handle --all-projects flag or default behavior
        if all_projects:
            if not input_path.exists():
                raise FileNotFoundError(f"Projects directory not found: {input_path}")

            click.echo(f"Processing all projects in {input_path}...")
            output_path = process_projects_hierarchy(
                input_path, from_date, to_date, not no_cache
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
            claude_path = convert_project_path_to_claude_dir(input_path)
            if claude_path.exists():
                click.echo(f"Converting project path {input_path} to {claude_path}")
                input_path = claude_path
            elif not input_path.exists():
                # Original path doesn't exist and conversion failed
                raise FileNotFoundError(
                    f"Neither {input_path} nor {claude_path} exists"
                )

        output_path = convert_jsonl_to_html(
            input_path,
            output,
            from_date,
            to_date,
            not no_individual_sessions,
            not no_cache,
        )
        if input_path.is_file():
            click.echo(f"Successfully converted {input_path} to {output_path}")
        else:
            jsonl_count = len(list(input_path.glob("*.jsonl")))
            if not no_individual_sessions:
                session_files = list(input_path.glob("session-*.html"))
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
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error converting file: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
