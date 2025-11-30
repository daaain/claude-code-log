#!/usr/bin/env python3
"""Interactive Terminal User Interface for Claude Code Log."""

import os
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Dict, Optional, cast

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Static,
)
from textual.reactive import reactive

from .cache import CacheManager, SessionCacheData, get_library_version
from .converter import ensure_fresh_cache
from .renderer import get_project_display_name


class ProjectSelector(App[Path]):
    """TUI for selecting a Claude project when multiple are found."""

    CSS = """
    #info-container {
        height: 3;
        border: solid $primary;
        margin-bottom: 1;
    }
    
    DataTable {
        height: auto;
    }
    """

    TITLE = "Claude Code Log - Project Selector"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("s", "select_project", "Select Project"),
    ]

    selected_project_path: reactive[Optional[Path]] = reactive(
        cast(Optional[Path], None)
    )
    projects: list[Path]
    matching_projects: list[Path]

    def __init__(self, projects: list[Path], matching_projects: list[Path]):
        """Initialize the project selector."""
        super().__init__()
        self.theme = "gruvbox"
        self.projects = projects
        self.matching_projects = matching_projects

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()

        with Container(id="main-container"):
            with Vertical():
                # Info
                with Container(id="info-container"):
                    info_text = f"Found {len(self.projects)} projects total"
                    if self.matching_projects:
                        info_text += (
                            f", {len(self.matching_projects)} match current directory"
                        )
                    yield Label(info_text, id="info")

                # Project table
                yield DataTable[str](id="projects-table", cursor_type="row")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize the application when mounted."""
        self.populate_table()

    def on_resize(self) -> None:
        """Handle terminal resize events."""
        self.populate_table()

    def populate_table(self) -> None:
        """Populate the projects table."""
        table = cast(DataTable[str], self.query_one("#projects-table", DataTable))
        table.clear(columns=True)

        # Add columns
        table.add_column("Project", width=self.size.width - 13)
        table.add_column("Sessions", width=10)

        # Add rows
        for project_path in self.projects:
            try:
                cache_manager = CacheManager(project_path, get_library_version())
                project_cache = cache_manager.get_cached_project_data()

                if not project_cache or not project_cache.sessions:
                    try:
                        ensure_fresh_cache(project_path, cache_manager, silent=True)
                        # Reload cache after ensuring it's fresh
                        project_cache = cache_manager.get_cached_project_data()
                    except Exception:
                        # If cache building fails, continue with empty cache
                        project_cache = None

                # Get project info
                session_count = (
                    len(project_cache.sessions)
                    if project_cache and project_cache.sessions
                    else 0
                )

                # Create project display - just use the directory name
                project_display = f"  {project_path.name}"

                # Add indicator if matches current directory
                if project_path in self.matching_projects:
                    project_display = f"→ {project_display[2:]}"

                table.add_row(
                    project_display,
                    str(session_count),
                )
            except Exception:
                # If we can't read cache, show basic info
                project_display = f"  {project_path.name}"
                if project_path in self.matching_projects:
                    project_display = f"→ {project_display[2:]}"

                table.add_row(
                    project_display,
                    "Unknown",
                )

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        """Handle row highlighting (cursor movement) in the projects table."""
        self._update_selected_project_from_cursor()

    def _update_selected_project_from_cursor(self) -> None:
        """Update the selected project based on the current cursor position."""
        table = cast(DataTable[str], self.query_one("#projects-table", DataTable))
        try:
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                # Extract project display from the first column
                project_display = str(row_data[0]).strip()

                # Remove the arrow indicator if present
                if project_display.startswith("→"):
                    project_display = project_display[1:].strip()

                # Find the matching project path
                for project_path in self.projects:
                    if project_path.name == project_display:
                        self.selected_project_path = project_path
                        break
        except Exception:
            # If we can't get the row data, don't update selection
            pass

    def action_select_project(self) -> None:
        """Select the highlighted project."""
        if self.selected_project_path:
            self.exit(self.selected_project_path)
        else:
            # If no selection, use the first project
            if self.projects:
                self.exit(self.projects[0])

    async def action_quit(self) -> None:
        """Quit the application with proper cleanup."""
        self.exit(None)


class SessionBrowser(App[Optional[str]]):
    """Interactive TUI for browsing and managing Claude Code Log sessions."""

    CSS = """
    #main-container {
        padding: 0;
        height: 100%;
    }
    
    #stats-container {
        height: auto;
        min-height: 3;
        max-height: 5;
        border: solid $primary;
    }
    
    .stat-label {
        color: $primary;
        text-style: bold;
    }
    
    .stat-value {
        color: $accent;
    }
    
    #sessions-table {
        height: 1fr;
    }
    
    #expanded-content {
        display: none;
        height: 1fr;
        border: solid $secondary;
        overflow-y: auto;
    }
    """

    TITLE = "Claude Code Log - Session Browser"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("h", "export_selected", "Open HTML page"),
        Binding("c", "resume_selected", "Resume in Claude Code"),
        Binding("e", "toggle_expanded", "Toggle Expanded View"),
        Binding("p", "back_to_projects", "Open Project Selector"),
        Binding("?", "toggle_help", "Help"),
    ]

    selected_session_id: reactive[Optional[str]] = reactive(cast(Optional[str], None))
    is_expanded: reactive[bool] = reactive(False)
    project_path: Path
    cache_manager: CacheManager
    sessions: Dict[str, SessionCacheData]

    def __init__(self, project_path: Path):
        """Initialize the session browser with a project path."""
        super().__init__()
        self.theme = "gruvbox"
        self.project_path = project_path
        self.cache_manager = CacheManager(project_path, get_library_version())
        self.sessions = {}

    def compose(self) -> ComposeResult:
        """Create the UI layout."""
        yield Header()

        with Container(id="main-container"):
            with Vertical():
                # Project statistics
                with Container(id="stats-container"):
                    yield Label("Loading project information...", id="stats")

                # Session table
                yield DataTable[str](id="sessions-table", cursor_type="row")

                # Expanded content container (initially hidden)
                yield Static("", id="expanded-content")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize the application when mounted."""
        self.load_sessions()

    def on_resize(self) -> None:
        """Handle terminal resize events."""
        # Only update if we have sessions loaded
        if self.sessions:
            self.populate_table()
            self.update_stats()

    def load_sessions(self) -> None:
        """Load session information from cache or build cache if needed."""
        # Check if we need to rebuild cache by checking for modified files
        jsonl_files = list(self.project_path.glob("*.jsonl"))
        modified_files = self.cache_manager.get_modified_files(jsonl_files)

        # Get cached project data
        project_cache = self.cache_manager.get_cached_project_data()

        if project_cache and project_cache.sessions and not modified_files:
            # Use cached session data - cache is up to date
            self.sessions = project_cache.sessions
        else:
            # Need to build cache - use ensure_fresh_cache to populate cache if needed
            try:
                # Use ensure_fresh_cache to build cache (it handles all the session processing)
                ensure_fresh_cache(self.project_path, self.cache_manager, silent=True)

                # Now get the updated cache data
                project_cache = self.cache_manager.get_cached_project_data()
                if project_cache and project_cache.sessions:
                    self.sessions = project_cache.sessions
                else:
                    self.sessions = {}

            except Exception:
                # Don't show notification during startup - just return
                return

        # Only update UI if we're in app context
        try:
            self.populate_table()
            self.update_stats()
        except Exception:
            # Not in app context, skip UI updates
            pass

    def populate_table(self) -> None:
        """Populate the sessions table with session data."""
        table = cast(DataTable[str], self.query_one("#sessions-table", DataTable))
        table.clear(columns=True)

        # Calculate responsive column widths based on terminal size
        terminal_width = self.size.width

        # Fixed widths for specific columns
        session_id_width = 10
        messages_width = 10
        tokens_width = 14

        # Responsive time column widths - shorter on narrow terminals
        time_width = 16 if terminal_width >= 120 else 12

        # Calculate remaining space for title column
        fixed_width = (
            session_id_width + messages_width + tokens_width + (time_width * 2)
        )
        padding_estimate = 8  # Account for column separators and padding
        title_width = max(30, terminal_width - fixed_width - padding_estimate)

        # Add columns with calculated widths
        table.add_column("Session ID", width=session_id_width)
        table.add_column("Title or First Message", width=title_width)
        table.add_column("Start Time", width=time_width)
        table.add_column("End Time", width=time_width)
        table.add_column("Messages", width=messages_width)
        table.add_column("Tokens", width=tokens_width)

        # Sort sessions by start time (newest first)
        sorted_sessions = sorted(
            self.sessions.items(), key=lambda x: x[1].first_timestamp, reverse=True
        )

        # Add rows
        for session_id, session_data in sorted_sessions:
            # Format timestamps - use short format for narrow terminals
            use_short_format = terminal_width < 120
            start_time = self.format_timestamp(
                session_data.first_timestamp, short_format=use_short_format
            )
            end_time = self.format_timestamp(
                session_data.last_timestamp, short_format=use_short_format
            )

            # Format token count
            total_tokens = (
                session_data.total_input_tokens + session_data.total_output_tokens
            )
            token_display = f"{total_tokens:,}" if total_tokens > 0 else "-"

            # Get summary or first user message
            preview = (
                session_data.summary
                or session_data.first_user_message
                or "No preview available"
            )
            # Let Textual handle truncation based on column width

            table.add_row(
                session_id[:8],
                preview,
                start_time,
                end_time,
                str(session_data.message_count),
                token_display,
            )

    def update_stats(self) -> None:
        """Update the project statistics display."""
        total_sessions = len(self.sessions)
        total_messages = sum(s.message_count for s in self.sessions.values())
        total_tokens = sum(
            s.total_input_tokens + s.total_output_tokens for s in self.sessions.values()
        )

        # Get project name using shared logic
        working_directories = None
        try:
            project_cache = self.cache_manager.get_cached_project_data()
            if project_cache and project_cache.working_directories:
                working_directories = project_cache.working_directories
        except Exception:
            # Fall back to directory name if cache fails
            pass

        project_name = get_project_display_name(
            self.project_path.name, working_directories
        )

        # Find date range
        if self.sessions:
            timestamps = [
                s.first_timestamp for s in self.sessions.values() if s.first_timestamp
            ]
            earliest = min(timestamps) if timestamps else ""
            latest = (
                max(
                    s.last_timestamp for s in self.sessions.values() if s.last_timestamp
                )
                if self.sessions
                else ""
            )

            date_range = ""
            if earliest and latest:
                earliest_date = self.format_timestamp(earliest, date_only=True)
                latest_date = self.format_timestamp(latest, date_only=True)
                if earliest_date == latest_date:
                    date_range = earliest_date
                else:
                    date_range = f"{earliest_date} to {latest_date}"
        else:
            date_range = "No sessions found"

        # Create spaced layout: Project (left), Sessions info (center), Date range (right)
        terminal_width = self.size.width

        # Project section (left aligned)
        project_section = f"[bold]Project:[/bold] {project_name}"

        # Sessions info section (center)
        sessions_section = f"[bold]Sessions:[/bold] {total_sessions:,} | [bold]Messages:[/bold] {total_messages:,} | [bold]Tokens:[/bold] {total_tokens:,}"

        # Date range section (right aligned)
        date_section = f"[bold]Date Range:[/bold] {date_range}"

        if terminal_width >= 120:
            # Wide terminal: single row with proper spacing
            # Calculate spacing to distribute sections across width
            project_len = len(
                project_section.replace("[bold]", "").replace("[/bold]", "")
            )
            sessions_len = len(
                sessions_section.replace("[bold]", "").replace("[/bold]", "")
            )
            date_len = len(date_section.replace("[bold]", "").replace("[/bold]", ""))

            # Calculate spaces needed for center and right alignment
            total_content_width = project_len + sessions_len + date_len
            available_space = (
                terminal_width - total_content_width - 4
            )  # Account for margins

            if available_space > 0:
                left_padding = available_space // 2
                right_padding = available_space - left_padding
                stats_text = f"{project_section}{' ' * left_padding}{sessions_section}{' ' * right_padding}{date_section}"
            else:
                # Fallback if terminal too narrow for proper spacing
                stats_text = f"{project_section}  {sessions_section}  {date_section}"
        else:
            # Narrow terminal: multi-row layout
            stats_text = f"{project_section}\n{sessions_section}\n{date_section}"

        stats_label = self.query_one("#stats", Label)
        stats_label.update(stats_text)

    def format_timestamp(
        self, timestamp: str, date_only: bool = False, short_format: bool = False
    ) -> str:
        """Format timestamp for display."""
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if date_only:
                return dt.strftime("%Y-%m-%d")
            elif short_format:
                return dt.strftime("%m-%d %H:%M")
            else:
                return dt.strftime("%m-%d %H:%M")
        except (ValueError, AttributeError):
            return "Unknown"

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        """Handle row highlighting (cursor movement) in the sessions table."""
        self._update_selected_session_from_cursor()

        # Update expanded content if it's visible
        if self.is_expanded:
            self._update_expanded_content()

    def _update_selected_session_from_cursor(self) -> None:
        """Update the selected session based on the current cursor position."""
        table = cast(DataTable[str], self.query_one("#sessions-table", DataTable))
        try:
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                # Extract session ID from the first column (now just first 8 chars)
                session_id_display = str(row_data[0])
                # Find the full session ID
                for full_session_id in self.sessions.keys():
                    if full_session_id.startswith(session_id_display):
                        self.selected_session_id = full_session_id
                        break
        except Exception:
            # If we can't get the row data, don't update selection
            pass

    def action_export_selected(self) -> None:
        """Export the selected session to HTML."""
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        try:
            # Use cached session HTML file directly
            session_file = (
                self.project_path / f"session-{self.selected_session_id}.html"
            )

            webbrowser.open(f"file://{session_file}")
            self.notify(f"Opened session HTML: {session_file}")

        except Exception as e:
            self.notify(f"Error opening session HTML: {e}", severity="error")

    def action_resume_selected(self) -> None:
        """Resume the selected session in Claude Code."""
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        try:
            # Get the session's working directory if available
            session_data = self.sessions.get(self.selected_session_id)
            if session_data and session_data.cwd:
                # Change to the session's working directory
                target_dir = Path(session_data.cwd)
                if target_dir.exists() and target_dir.is_dir():
                    os.chdir(target_dir)
                else:
                    self.notify(
                        f"Warning: Session working directory not found: {session_data.cwd}",
                        severity="warning",
                    )

            # Use Textual's suspend context manager for proper terminal cleanup
            with self.suspend():
                # Terminal is properly restored here by Textual
                # Replace the current process with claude -r <sessionId>
                os.execvp("claude", ["claude", "-r", self.selected_session_id])
        except FileNotFoundError:
            self.notify(
                "Claude Code CLI not found. Make sure 'claude' is in your PATH.",
                severity="error",
            )
        except Exception as e:
            self.notify(f"Error resuming session: {e}", severity="error")

    def _escape_rich_markup(self, text: str) -> str:
        """Escape Rich markup characters in text to prevent parsing errors."""
        if not text:
            return text
        # Escape square brackets which are used for Rich markup
        return text.replace("[", "\\[").replace("]", "\\]")

    def _update_expanded_content(self) -> None:
        """Update the expanded content for the currently selected session."""
        if (
            not self.selected_session_id
            or self.selected_session_id not in self.sessions
        ):
            return

        expanded_content = self.query_one("#expanded-content", Static)
        session_data = self.sessions[self.selected_session_id]

        # Build expanded content
        content_parts: list[str] = []

        # Session ID (safe - UUID format)
        content_parts.append(f"[bold]Session ID:[/bold] {self.selected_session_id}")

        # Summary (if available) - escape markup
        if session_data.summary:
            escaped_summary = self._escape_rich_markup(session_data.summary)
            content_parts.append(f"\n[bold]Summary:[/bold] {escaped_summary}")

        # First user message - escape markup
        if session_data.first_user_message:
            escaped_message = self._escape_rich_markup(session_data.first_user_message)
            content_parts.append(
                f"\n[bold]First User Message:[/bold] {escaped_message}"
            )

        # Working directory (if available) - escape markup
        if session_data.cwd:
            escaped_cwd = self._escape_rich_markup(session_data.cwd)
            content_parts.append(f"\n[bold]Working Directory:[/bold] {escaped_cwd}")

        # Token usage (safe - numeric data)
        total_tokens = (
            session_data.total_input_tokens + session_data.total_output_tokens
        )
        if total_tokens > 0:
            token_details = f"Input: {session_data.total_input_tokens:,} | Output: {session_data.total_output_tokens:,}"
            if session_data.total_cache_creation_tokens > 0:
                token_details += (
                    f" | Cache Creation: {session_data.total_cache_creation_tokens:,}"
                )
            if session_data.total_cache_read_tokens > 0:
                token_details += (
                    f" | Cache Read: {session_data.total_cache_read_tokens:,}"
                )
            content_parts.append(f"\n[bold]Token Usage:[/bold] {token_details}")

        expanded_content.update("\n".join(content_parts))

    def action_toggle_expanded(self) -> None:
        """Toggle the expanded view for the selected session."""
        if (
            not self.selected_session_id
            or self.selected_session_id not in self.sessions
        ):
            return

        expanded_content = self.query_one("#expanded-content", Static)

        if self.is_expanded:
            # Hide expanded content
            self.is_expanded = False
            expanded_content.set_styles("display: none;")
            expanded_content.update("")
        else:
            # Show expanded content
            self.is_expanded = True
            expanded_content.set_styles("display: block;")
            self._update_expanded_content()

    def action_toggle_help(self) -> None:
        """Show help information."""
        help_text = (
            "Claude Code Log - Session Browser\n\n"
            "Navigation:\n"
            "- Use arrow keys to select sessions\n"
            "- Expanded content updates automatically when visible\n\n"
            "Actions:\n"
            "- e: Toggle expanded view for session\n"
            "- h: Open selected session's HTML page log\n"
            "- c: Resume selected session in Claude Code\n"
            "- p: Open project selector\n"
            "- q: Quit\n\n"
        )
        self.notify(help_text, timeout=10)

    def action_back_to_projects(self) -> None:
        """Navigate to the project selector."""
        # Exit with a special return value to signal we want to go to project selector
        self.exit(result="back_to_projects")

    async def action_quit(self) -> None:
        """Quit the application with proper cleanup."""
        self.exit()


def run_project_selector(
    projects: list[Path], matching_projects: list[Path]
) -> Optional[Path]:
    """Run the project selector TUI and return the selected project path."""
    if not projects:
        print("Error: No projects provided")
        return None

    app = ProjectSelector(projects, matching_projects)
    try:
        return app.run()
    except KeyboardInterrupt:
        # Textual handles terminal cleanup automatically
        print("\nInterrupted")
        return None


def run_session_browser(project_path: Path) -> Optional[str]:
    """Run the session browser TUI for the given project path."""
    if not project_path.exists():
        print(f"Error: Project path {project_path} does not exist")
        return None

    if not project_path.is_dir():
        print(f"Error: {project_path} is not a directory")
        return None

    # Check if there are any JSONL files
    jsonl_files = list(project_path.glob("*.jsonl"))
    if not jsonl_files:
        print(f"Error: No JSONL transcript files found in {project_path}")
        return None

    app = SessionBrowser(project_path)
    try:
        return app.run()
    except KeyboardInterrupt:
        # Textual handles terminal cleanup automatically
        print("\nInterrupted")
        return None
