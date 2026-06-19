#!/usr/bin/env python3
"""Interactive Terminal User Interface for Claude Code Log."""

import os
import webbrowser
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any, ClassVar, List, Optional, cast

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    MarkdownViewer,
    Static,
    Tree,
)
from textual.reactive import reactive
from rich.markup import escape as escape_markup

from .cache import (
    CacheManager,
    SessionCacheData,
    get_library_version,
    scrub_surrogates,
)
from .converter import (
    build_session_title,
    ensure_fresh_cache,
    get_file_extension,
    load_directory_transcripts,
)
from .renderer import get_renderer
from .utils import get_project_display_name


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
        Binding("escape", "quit", "Quit", show=False),
        Binding("s", "select_project", "Select Project"),
        Binding("a", "archive_project", "Archive Project"),
        Binding("d", "delete_project", "Delete Project"),
        Binding("r", "restore_project", "Restore Project"),
    ]

    selected_project_path: reactive[Optional[Path]] = reactive(
        cast(Optional[Path], None)
    )
    projects: list[Path]
    matching_projects: list[Path]
    archived_projects: set[Path]

    def __init__(
        self,
        projects: list[Path],
        matching_projects: list[Path],
        archived_projects: Optional[set[Path]] = None,
    ):
        """Initialize the project selector."""
        super().__init__()
        self.theme = "gruvbox"
        self.projects = projects
        self.matching_projects = matching_projects
        self.archived_projects = archived_projects or set()

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
        table.add_column("Project", width=max(20, self.size.width - 13))
        table.add_column("Sessions", width=10)

        # Add rows
        for project_path in self.projects:
            is_archived = project_path in self.archived_projects
            try:
                cache_manager = CacheManager(project_path, get_library_version())
                project_cache = cache_manager.get_cached_project_data()

                if not project_cache or not project_cache.sessions:
                    if not is_archived:
                        # Only try to build cache for non-archived projects
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

                # Add archived indicator
                if is_archived:
                    project_display = f"{project_display} [ARCHIVED]"

                table.add_row(
                    project_display,
                    str(session_count),
                )
            except Exception:
                # If we can't read cache, show basic info
                project_display = f"  {project_path.name}"
                if project_path in self.matching_projects:
                    project_display = f"→ {project_display[2:]}"
                if is_archived:
                    project_display = f"{project_display} [ARCHIVED]"

                table.add_row(
                    project_display,
                    "Unknown",
                )

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        """Handle row highlighting (cursor movement) in the projects table."""
        self._update_selected_project_from_cursor()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """Handle row selection (Enter key) in the projects table."""
        self.action_select_project()

    def _update_selected_project_from_cursor(self) -> None:
        """Update the selected project based on the current cursor position."""
        try:
            table = cast(DataTable[str], self.query_one("#projects-table", DataTable))
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                # Extract project display from the first column
                project_display = str(row_data[0]).strip()

                # Remove the arrow indicator if present
                if project_display.startswith("→"):
                    project_display = project_display[1:].strip()

                # Remove the archived indicator if present
                if project_display.endswith(" [ARCHIVED]"):
                    project_display = project_display[:-11].strip()

                # Find the matching project path
                for project_path in self.projects:
                    if project_path.name == project_display:
                        self.selected_project_path = project_path
                        break
        except Exception:
            # If widget not mounted yet or we can't get the row data, don't update selection
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

    def _get_project_session_count(self, project_path: Path) -> int:
        """Get the number of sessions in a project from cache."""
        try:
            cache_manager = CacheManager(project_path, get_library_version())
            project_cache = cache_manager.get_cached_project_data()
            if project_cache and project_cache.sessions:
                return len(project_cache.sessions)
        except Exception:
            pass
        return 0

    def _is_project_archived(self, project_path: Path) -> bool:
        """Check if a project is archived (no JSONL files exist)."""
        return project_path in self.archived_projects

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],  # noqa: ARG002
    ) -> bool | None:
        """Control which actions are available based on context."""
        project_path = self.selected_project_path
        is_archived = project_path in self.archived_projects if project_path else False

        if action == "archive_project":
            # Can only archive non-archived projects
            return project_path is not None and not is_archived
        elif action == "restore_project":
            # Can only restore archived projects
            return project_path is not None and is_archived
        elif action == "delete_project":
            # Can delete any project
            return project_path is not None

        # Allow all other actions (quit, select_project, etc.)
        return True

    def action_archive_project(self) -> None:
        """Archive all sessions in the selected project."""
        if not self.selected_project_path:
            self.notify("No project selected", severity="warning")
            return

        if self._is_project_archived(self.selected_project_path):
            self.notify("Project is already archived", severity="warning")
            return

        session_count = self._get_project_session_count(self.selected_project_path)
        self.push_screen(
            ArchiveProjectConfirmScreen(self.selected_project_path.name, session_count),
            self._handle_archive_project_confirm,
        )

    def _handle_archive_project_confirm(self, confirmed: bool | None) -> None:
        """Handle the result of the archive project confirmation dialog."""
        if not confirmed or not self.selected_project_path:
            return

        project_path = self.selected_project_path

        # Collect all JSONL files first
        jsonl_files = list(project_path.glob("*.jsonl"))
        if not jsonl_files:
            self.notify("No sessions to archive", severity="warning")
            return

        # Track successes and failures
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []  # (filename, error message)

        # Delete all JSONL files in the project
        for jsonl_file in jsonl_files:
            try:
                jsonl_file.unlink()
                succeeded.append(jsonl_file.name)
            except Exception as e:
                failed.append((jsonl_file.name, str(e)))

        # Report results clearly
        total = len(jsonl_files)
        if failed:
            # Show detailed failure information
            failed_names = ", ".join(f[0] for f in failed[:3])
            if len(failed) > 3:
                failed_names += f" and {len(failed) - 3} more"
            self.notify(
                f"Archive incomplete: {len(succeeded)}/{total} sessions deleted. "
                f"Failed: {failed_names}",
                severity="error",
            )
        else:
            self.notify(f"Archived {len(succeeded)} sessions")

        # Only mark as fully archived if ALL files were deleted
        if not failed and succeeded:
            self.archived_projects.add(project_path)

        # Always refresh to show current state
        if succeeded:
            self.populate_table()

    def action_delete_project(self) -> None:
        """Delete the selected project from cache (and optionally JSONL files)."""
        if not self.selected_project_path:
            self.notify("No project selected", severity="warning")
            return

        is_archived = self._is_project_archived(self.selected_project_path)
        session_count = self._get_project_session_count(self.selected_project_path)
        self.push_screen(
            DeleteProjectConfirmScreen(
                self.selected_project_path.name, session_count, is_archived
            ),
            self._handle_delete_project_confirm,
        )

    def _handle_delete_project_confirm(self, result: Optional[str]) -> None:
        """Handle the result of the delete project confirmation dialog."""
        if not result or not self.selected_project_path:
            return

        project_path = self.selected_project_path

        # Delete cache
        cache_manager = CacheManager(project_path, get_library_version())
        cache_manager.clear_cache()

        # If deleting both, also delete JSONL files
        file_delete_failed = False
        if result == "both":
            jsonl_files = list(project_path.glob("*.jsonl"))
            if jsonl_files:
                succeeded: list[str] = []
                failed: list[tuple[str, str]] = []

                for jsonl_file in jsonl_files:
                    try:
                        jsonl_file.unlink()
                        succeeded.append(jsonl_file.name)
                    except Exception as e:
                        failed.append((jsonl_file.name, str(e)))

                if failed:
                    file_delete_failed = True
                    failed_names = ", ".join(f[0] for f in failed[:3])
                    if len(failed) > 3:
                        failed_names += f" and {len(failed) - 3} more"
                    self.notify(
                        f"Cache deleted but {len(failed)}/{len(jsonl_files)} "
                        f"session files failed to delete: {failed_names}",
                        severity="error",
                    )

        # Remove from projects list
        if project_path in self.projects:
            self.projects.remove(project_path)
        if project_path in self.matching_projects:
            self.matching_projects.remove(project_path)
        if project_path in self.archived_projects:
            self.archived_projects.discard(project_path)

        if not file_delete_failed:
            self.notify(f"Deleted project: {project_path.name}")
        self.selected_project_path = None
        self.populate_table()

    def action_restore_project(self) -> None:
        """Restore all archived sessions in the selected project."""
        if not self.selected_project_path:
            self.notify("No project selected", severity="warning")
            return

        if not self._is_project_archived(self.selected_project_path):
            self.notify("Project is not archived", severity="warning")
            return

        session_count = self._get_project_session_count(self.selected_project_path)
        self.push_screen(
            RestoreProjectConfirmScreen(self.selected_project_path.name, session_count),
            self._handle_restore_project_confirm,
        )

    def _handle_restore_project_confirm(self, confirmed: bool | None) -> None:
        """Handle the result of the restore project confirmation dialog."""
        if not confirmed or not self.selected_project_path:
            return

        project_path = self.selected_project_path
        cache_manager = CacheManager(project_path, get_library_version())
        project_cache = cache_manager.get_cached_project_data()

        if not project_cache or not project_cache.sessions:
            self.notify("No sessions to restore", severity="warning")
            return

        # Ensure project directory exists
        project_path.mkdir(parents=True, exist_ok=True)

        # Identify sessions that need restoration (don't already exist as files)
        sessions_to_restore = [
            session_id
            for session_id in project_cache.sessions
            if not (project_path / f"{session_id}.jsonl").exists()
        ]

        if not sessions_to_restore:
            self.notify("All sessions already exist as files", severity="warning")
            return

        # Track successes and failures
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []  # (session_id, error message)

        for session_id in sessions_to_restore:
            jsonl_path = project_path / f"{session_id}.jsonl"
            try:
                messages = cache_manager.export_session_to_jsonl(session_id)
                if messages:
                    with open(jsonl_path, "w", encoding="utf-8") as f:
                        for msg in messages:
                            f.write(msg + "\n")
                    succeeded.append(session_id)
                else:
                    failed.append((session_id, "No messages found in cache"))
            except Exception as e:
                failed.append((session_id, str(e)))

        # Report results clearly
        total = len(sessions_to_restore)
        if failed:
            # Show detailed failure information
            failed_ids = ", ".join(f[0][:8] for f in failed[:3])  # Truncate UUIDs
            if len(failed) > 3:
                failed_ids += f" and {len(failed) - 3} more"
            self.notify(
                f"Restore incomplete: {len(succeeded)}/{total} sessions restored. "
                f"Failed: {failed_ids}",
                severity="error",
            )
        else:
            self.notify(f"Restored {len(succeeded)} sessions")

        # Only mark as fully restored if ALL sessions were restored
        if not failed and succeeded:
            self.archived_projects.discard(project_path)

        # Always refresh to show current state
        if succeeded:
            self.populate_table()


class SafeMarkdownViewer(MarkdownViewer):
    """MarkdownViewer that handles link clicks safely.

    Intercepts link clicks to prevent crashes from file/external links
    while still allowing anchor navigation for ToC.
    """

    # Allow maximizing the viewer (screen will redirect children to this)
    ALLOW_MAXIMIZE = True

    def on_mount(self) -> None:
        """Configure document for proper keyboard navigation."""
        # Enable focus on the document so keys work after focus changes
        self.document.can_focus = True

    async def go(self, location: str | PurePath) -> None:
        """Navigate to a new location - intercept non-anchor links.

        Override parent's go() method to handle links appropriately:
        - Anchor links (#section): allow default scrolling
        - HTTP/HTTPS URLs: open in browser
        - Relative file links: show warning (not supported)
        """
        location_str = str(location)

        if location_str.startswith("#"):
            # Anchor link - allow default scroll behaviour
            await super().go(location)
        elif location_str.startswith(("http://", "https://")):
            # External URL - open in browser
            webbrowser.open(location_str)
            self.notify(f"Opening in browser: {location_str[:50]}...")
        else:
            # Relative file link - not supported in embedded viewer
            self.notify(
                "File links not supported in embedded viewer",
                severity="warning",
            )


class MarkdownViewerScreen(ModalScreen[None]):
    """Modal screen for viewing Markdown content with table of contents."""

    # Character-based pagination - ~50KB per page for good scroll performance
    PAGE_SIZE_CHARS = 50_000

    CSS = """
    MarkdownViewerScreen {
        align: center middle;
    }

    #md-container {
        width: 95%;
        height: 95%;
        border: solid $primary;
        background: $surface;
    }

    #md-header {
        dock: top;
        height: 3;
        background: $primary;
        color: $text;
        text-align: center;
        padding: 1;
    }

    #md-viewer {
        height: 1fr;
    }

    /* Limit ToC width to ~1/3 of the viewer */
    #md-viewer MarkdownTableOfContents {
        max-width: 60;
    }

    #pagination-controls {
        dock: top;
        height: 1;
        background: $warning;
        color: $text;
        text-align: center;
    }

    #md-footer {
        dock: bottom;
        height: 1;
        background: $primary-darken-2;
        color: $text-muted;
        text-align: center;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("t", "toggle_toc", "Toggle ToC"),
        Binding("tab", "switch_focus", "Switch focus", show=False, priority=True),
        Binding("shift+tab", "switch_focus", "Switch focus", show=False, priority=True),
        Binding("n", "next_page", "Next page"),
        Binding("right", "next_page", "Next page", show=False),
        Binding("p", "prev_page", "Prev page"),
        Binding("left", "prev_page", "Prev page", show=False),
    ]

    def __init__(self, content: str, title: str = "Markdown Viewer") -> None:
        super().__init__()
        self.md_content = content
        self.md_title = title
        self._pages = self._split_into_pages(content)
        self._current_page = 0
        self._is_paginated = len(self._pages) > 1

    def _split_into_pages(self, content: str) -> list[str]:
        """Split markdown content into pages by character count.

        Splits at section boundaries (## ) when possible to avoid
        cutting mid-section, but will split within sections if
        a single section exceeds PAGE_SIZE_CHARS.
        """
        import re

        if len(content) <= self.PAGE_SIZE_CHARS:
            return [content]

        pages: list[str] = []
        current_page = ""

        # Split by level 2 headings, keeping the delimiter
        sections = re.split(r"(\n(?=## ))", content)

        for section in sections:
            if not section:
                continue

            # If adding this section exceeds page size
            if len(current_page) + len(section) > self.PAGE_SIZE_CHARS:
                # If current page has content, save it
                if current_page.strip():
                    pages.append(current_page)
                    current_page = ""

                # If section itself exceeds page size, split it by lines
                if len(section) > self.PAGE_SIZE_CHARS:
                    lines = section.split("\n")
                    for line in lines:
                        if len(current_page) + len(line) + 1 > self.PAGE_SIZE_CHARS:
                            if current_page.strip():
                                pages.append(current_page)
                            current_page = line + "\n"
                        else:
                            current_page += line + "\n"
                else:
                    current_page = section
            else:
                current_page += section

        # Don't forget the last page
        if current_page.strip():
            pages.append(current_page)

        return pages if pages else [content]

    def compose(self) -> ComposeResult:
        with Container(id="md-container"):
            yield Static(self.md_title, id="md-header")
            if self._is_paginated:
                yield Static(
                    f"Page {self._current_page + 1}/{len(self._pages)} | "
                    "← or p: prev | → or n: next",
                    id="pagination-controls",
                )
            yield SafeMarkdownViewer(
                self._pages[self._current_page],
                id="md-viewer",
                show_table_of_contents=True,
                open_links=False,  # We handle links in go() override
            )
            footer_text = "Press ESC or q to close | t: toggle ToC"
            if self._is_paginated:
                footer_text += " | n/p: navigate pages"
            yield Static(footer_text, id="md-footer")

    def on_mount(self) -> None:
        """Customize ToC tree after mount."""
        self.call_later(self._customize_toc_tree)

    def _customize_toc_tree(self) -> None:
        """Customize ToC: collapse to 3 levels and remove roman numeral prefixes."""
        try:
            viewer = self.query_one("#md-viewer", MarkdownViewer)
            toc = viewer.query_one("MarkdownTableOfContents")
            tree = cast(Tree[Any], toc.query_one(Tree))

            # Clean up labels (remove roman numerals and message type prefixes)
            self._clean_toc_labels(tree.root)

            # Collapse all, then expand root, children, and grandchildren
            tree.root.collapse_all()
            tree.root.expand()
            for child in tree.root.children:
                child.expand()
                for grandchild in child.children:
                    grandchild.expand()
        except Exception:
            pass  # ToC might not be ready yet, or tree structure differs

    def _clean_toc_labels(self, node: Any) -> None:
        """Recursively clean tree node labels for a cleaner ToC."""
        import re

        # Unicode roman numerals used by Textual's MarkdownTableOfContents
        roman_numerals = "ⅠⅡⅢⅣⅤⅥ"
        # Message type prefixes that add clutter in ToC context
        clutter_prefixes = (
            "User: ",
            "Assistant: ",
            "Thinking: ",
            "Sub-assistant: ",
        )

        label = str(node.label)

        # Strip leading roman numeral and space (e.g., "Ⅱ Heading" -> "Heading")
        if label and label[0] in roman_numerals:
            label = label[2:] if len(label) > 1 else label

        # Strip message type prefixes wherever they appear
        # (they come after the emoji, e.g., "🤷 User: *text*" -> "🤷 *text*")
        for prefix in clutter_prefixes:
            if prefix in label:
                label = label.replace(prefix, "", 1)
                break

        # Simplify "Task (details): " to "Task: " (details are redundant)
        label = re.sub(r"Task \([^)]+\): ", "Task: ", label)

        node.set_label(label)
        for child in node.children:
            self._clean_toc_labels(child)

    async def action_dismiss(self, result: None = None) -> None:
        self.dismiss(result)

    def action_maximize(self) -> None:
        """Maximize the MarkdownViewer (not individual children)."""
        try:
            viewer = self.query_one("#md-viewer", SafeMarkdownViewer)
            self.maximize(viewer)
        except Exception:
            pass

    def on_key(self, event: Any) -> None:
        """Intercept Tab keys to handle focus switching without scroll."""
        if event.key in ("tab", "shift+tab"):
            event.prevent_default()
            event.stop()
            self.action_switch_focus()

    def _focus_viewer_content(self, viewer: SafeMarkdownViewer) -> None:
        """Focus the viewer's document content without scrolling."""
        # MarkdownViewer is a container; focus its document widget
        try:
            viewer.document.focus(scroll_visible=False)
        except Exception:
            viewer.focus(scroll_visible=False)

    def action_toggle_toc(self) -> None:
        """Toggle table of contents visibility, preserving scroll position."""
        viewer = self.query_one("#md-viewer", SafeMarkdownViewer)
        scroll_y = viewer.scroll_y
        will_show_toc = not viewer.show_table_of_contents
        viewer.show_table_of_contents = will_show_toc

        def restore_and_focus() -> None:
            viewer.scroll_to(y=scroll_y, animate=False)
            if will_show_toc:
                # Focus the Tree inside TOC when showing
                try:
                    toc = viewer.table_of_contents
                    tree = cast("Tree[Any]", toc.query_one(Tree))
                    tree.focus(scroll_visible=False)
                except Exception:
                    pass
            else:
                # Focus the document content when hiding TOC
                self._focus_viewer_content(viewer)

        self.call_later(restore_and_focus)

    def action_switch_focus(self) -> None:
        """Switch focus between TOC and content without scrolling."""
        viewer = self.query_one("#md-viewer", SafeMarkdownViewer)
        if not viewer.show_table_of_contents:
            # TOC hidden, just focus the document
            self._focus_viewer_content(viewer)
            return

        try:
            toc = viewer.table_of_contents
            # Get the Tree widget inside the TOC
            tree = cast("Tree[Any]", toc.query_one(Tree))
            if tree.has_focus:
                # Currently in TOC tree, switch to document
                self._focus_viewer_content(viewer)
            else:
                # Currently in document, switch to TOC tree
                tree.focus(scroll_visible=False)
        except Exception as e:
            self.notify(f"Focus switch error: {e}", severity="warning")
            self._focus_viewer_content(viewer)

    def action_next_page(self) -> None:
        """Navigate to next page (if paginated)."""
        if not self._is_paginated:
            return
        if self._current_page < len(self._pages) - 1:
            self._current_page += 1
            if self.is_mounted:
                self._update_viewer_content()

    def action_prev_page(self) -> None:
        """Navigate to previous page (if paginated)."""
        if not self._is_paginated:
            return
        if self._current_page > 0:
            self._current_page -= 1
            if self.is_mounted:
                self._update_viewer_content()

    def _update_viewer_content(self) -> None:
        """Update the markdown viewer with current page content."""
        try:
            # Update pagination controls
            controls = self.query_one("#pagination-controls", Static)
            controls.update(
                f"Page {self._current_page + 1}/{len(self._pages)} | ← or p: prev | → or n: next"
            )

            # Update the markdown content directly
            viewer = self.query_one("#md-viewer", SafeMarkdownViewer)
            viewer.document.update(self._pages[self._current_page])

            # Scroll to top of content
            viewer.scroll_home(animate=False)

            # Re-customize ToC after content loads
            self.call_later(self._customize_toc_tree)
        except Exception as e:
            self.notify(f"Error updating page: {e}", severity="error")


class ArchiveConfirmScreen(ModalScreen[bool]):
    """Modal screen for confirming session archiving (delete JSONL, keep cache)."""

    CSS = """
    ArchiveConfirmScreen {
        align: center middle;
    }

    #archive-container {
        width: 65;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }

    #archive-title {
        text-align: center;
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #archive-message {
        margin-bottom: 1;
    }

    #archive-info {
        color: $text-muted;
        margin-bottom: 1;
    }

    #archive-buttons {
        text-align: center;
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def compose(self) -> ComposeResult:
        with Container(id="archive-container"):
            yield Static("Archive Session", id="archive-title")
            yield Static(
                f"Session: {self.session_id[:8]}...",
                id="archive-message",
            )
            yield Static(
                "This will delete the JSONL file.\n"
                "The session will be archived and can be restored from cache.",
                id="archive-info",
            )
            yield Static("\\[Enter/y] Yes  \\[Esc/n] No", id="archive-buttons")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DeleteConfirmScreen(ModalScreen[Optional[str]]):
    """Modal screen for confirming session deletion with smart options."""

    CSS = """
    DeleteConfirmScreen {
        align: center middle;
    }

    #delete-container {
        width: 65;
        height: auto;
        border: solid $error;
        background: $surface;
        padding: 1 2;
    }

    #delete-title {
        text-align: center;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    #delete-message {
        margin-bottom: 1;
    }

    #delete-warning {
        color: $warning;
        margin-bottom: 1;
    }

    #delete-buttons {
        text-align: center;
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "delete_cache", "Cache only"),
        Binding("b", "delete_both", "Both", show=False),
        Binding("y", "delete_cache", "Yes", show=False),
        Binding("enter", "delete_cache", "Confirm", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, session_id: str, is_archived: bool = False) -> None:
        super().__init__()
        self.session_id = session_id
        self.is_archived = is_archived

    def compose(self) -> ComposeResult:
        with Container(id="delete-container"):
            yield Static("Delete Session", id="delete-title")
            yield Static(
                f"Session: {self.session_id[:8]}...",
                id="delete-message",
            )
            if self.is_archived:
                yield Static(
                    "This is an archived session with no JSONL file.\n"
                    "Deletion is PERMANENT and cannot be undone!",
                    id="delete-warning",
                )
                yield Static(
                    "\\[Enter/y/c] Delete from cache  \\[Esc/n] Cancel",
                    id="delete-buttons",
                )
            else:
                yield Static(
                    "Choose what to delete:\n"
                    "• Cache only: JSONL file remains, session can be re-parsed\n"
                    "• Both: Delete JSONL file AND cache (permanent!)",
                    id="delete-warning",
                )
                yield Static(
                    "\\[c] Cache only   \\[b] Both (permanent)   \\[Esc/n] Cancel",
                    id="delete-buttons",
                )

    def action_delete_cache(self) -> None:
        self.dismiss("cache_only")

    def action_delete_both(self) -> None:
        if not self.is_archived:
            self.dismiss("both")

    def action_cancel(self) -> None:
        self.dismiss(None)


class ArchiveProjectConfirmScreen(ModalScreen[bool]):
    """Modal screen for confirming project archival."""

    CSS = """
    ArchiveProjectConfirmScreen {
        align: center middle;
    }

    #archive-project-container {
        width: 65;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }

    #archive-project-title {
        text-align: center;
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    #archive-project-message {
        margin-bottom: 1;
    }

    #archive-project-info {
        color: $text-muted;
        margin-bottom: 1;
    }

    #archive-project-buttons {
        text-align: center;
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, project_name: str, session_count: int) -> None:
        super().__init__()
        self.project_name = project_name
        self.session_count = session_count

    def compose(self) -> ComposeResult:
        with Container(id="archive-project-container"):
            yield Static("Archive Project", id="archive-project-title")
            yield Static(
                f"Project: {self.project_name}\nSessions: {self.session_count}",
                id="archive-project-message",
            )
            yield Static(
                "This will delete ALL JSONL files in the project.\n"
                "Sessions will be archived and can be restored from cache.",
                id="archive-project-info",
            )
            yield Static("\\[Enter/y] Yes  \\[Esc/n] No", id="archive-project-buttons")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DeleteProjectConfirmScreen(ModalScreen[Optional[str]]):
    """Modal screen for confirming project deletion with smart options."""

    CSS = """
    DeleteProjectConfirmScreen {
        align: center middle;
    }

    #delete-project-container {
        width: 65;
        height: auto;
        border: solid $error;
        background: $surface;
        padding: 1 2;
    }

    #delete-project-title {
        text-align: center;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    #delete-project-message {
        margin-bottom: 1;
    }

    #delete-project-warning {
        color: $warning;
        margin-bottom: 1;
    }

    #delete-project-buttons {
        text-align: center;
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "delete_cache", "Cache only"),
        Binding("b", "delete_both", "Both", show=False),
        Binding("y", "delete_cache", "Yes", show=False),
        Binding("enter", "delete_cache", "Confirm", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self, project_name: str, session_count: int, is_archived: bool = False
    ) -> None:
        super().__init__()
        self.project_name = project_name
        self.session_count = session_count
        self.is_archived = is_archived

    def compose(self) -> ComposeResult:
        with Container(id="delete-project-container"):
            yield Static("Delete Project", id="delete-project-title")
            yield Static(
                f"Project: {self.project_name}\nSessions: {self.session_count}",
                id="delete-project-message",
            )
            if self.is_archived:
                yield Static(
                    "This is an archived project with no JSONL files.\n"
                    "Deletion is PERMANENT and cannot be undone!",
                    id="delete-project-warning",
                )
                yield Static(
                    "\\[Enter/y/c] Delete from cache  \\[Esc/n] Cancel",
                    id="delete-project-buttons",
                )
            else:
                yield Static(
                    "Choose what to delete:\n"
                    "• Cache only: JSONL files remain, sessions can be re-parsed\n"
                    "• Both: Delete ALL JSONL files AND cache (permanent!)",
                    id="delete-project-warning",
                )
                yield Static(
                    "\\[c] Cache only   \\[b] Both (permanent)   \\[Esc/n] Cancel",
                    id="delete-project-buttons",
                )

    def action_delete_cache(self) -> None:
        self.dismiss("cache_only")

    def action_delete_both(self) -> None:
        if not self.is_archived:
            self.dismiss("both")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RestoreProjectConfirmScreen(ModalScreen[bool]):
    """Modal screen for confirming project restoration."""

    CSS = """
    RestoreProjectConfirmScreen {
        align: center middle;
    }

    #restore-project-container {
        width: 65;
        height: auto;
        border: solid $success;
        background: $surface;
        padding: 1 2;
    }

    #restore-project-title {
        text-align: center;
        text-style: bold;
        color: $success;
        margin-bottom: 1;
    }

    #restore-project-message {
        margin-bottom: 1;
    }

    #restore-project-info {
        color: $text-muted;
        margin-bottom: 1;
    }

    #restore-project-buttons {
        text-align: center;
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes"),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, project_name: str, session_count: int) -> None:
        super().__init__()
        self.project_name = project_name
        self.session_count = session_count

    def compose(self) -> ComposeResult:
        with Container(id="restore-project-container"):
            yield Static("Restore Project", id="restore-project-title")
            yield Static(
                f"Project: {self.project_name}\n"
                f"Archived sessions: {self.session_count}",
                id="restore-project-message",
            )
            yield Static(
                "This will restore ALL archived sessions by writing JSONL files.\n"
                "The project directory will be created if it doesn't exist.",
                id="restore-project-info",
            )
            yield Static("\\[Enter/y] Yes  \\[Esc/n] No", id="restore-project-buttons")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


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
    
    #provider-filter {
        height: 3;
        border: solid $secondary;
        margin-bottom: 1;
    }
    """

    TITLE = "Claude Code Log - Session Browser"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "back_to_projects", "Back", show=False),
        Binding("enter", "export_selected", "Open HTML", show=False),
        Binding("a", "archive_session", "Archive Session"),
        Binding("h", "export_selected", "Open HTML page"),
        Binding("m", "export_markdown", "Open Markdown"),
        Binding("v", "view_markdown", "View Markdown"),
        # Hidden "force regenerate" variants (uppercase)
        Binding("H", "force_export_html", "Force HTML", show=False),
        Binding("M", "force_export_markdown", "Force Markdown", show=False),
        Binding("V", "force_view_markdown", "Force View", show=False),
        Binding("c", "resume_selected", "Resume in Claude Code"),
        Binding("r", "restore_jsonl", "Restore JSONL"),
        Binding("d", "delete_session", "Delete Session"),
        Binding("e", "toggle_expanded", "Toggle Expanded View"),
        Binding("p", "back_to_projects", "Open Project Selector"),
        Binding("f", "filter_provider", "Filter Provider"),
        Binding("?", "toggle_help", "Help"),
    ]

    selected_session_id: reactive[Optional[str]] = reactive(cast(Optional[str], None))
    is_expanded: reactive[bool] = reactive(False)
    selected_provider: reactive[Optional[str]] = reactive(cast(Optional[str], None))
    project_path: Path
    cache_manager: CacheManager
    sessions: dict[str, SessionCacheData]
    archived_sessions: dict[str, SessionCacheData]
    provider_sessions: dict[str, str]  # session_id -> provider_name

    def __init__(self, project_path: Path, is_archived: bool = False):
        """Initialize the session browser with a project path."""
        super().__init__()
        self.theme = "gruvbox"
        self.project_path = project_path.resolve()
        self.is_archived_project = is_archived
        self.cache_manager = CacheManager(self.project_path, get_library_version())
        self.sessions = {}
        self.archived_sessions = {}
        self.provider_sessions = {}

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
        # For archived projects, just load from cache (no JSONL files to check)
        if self.is_archived_project:
            project_cache = self.cache_manager.get_cached_project_data()
            if project_cache and project_cache.sessions:
                # All sessions are "archived" for fully archived projects
                self.sessions = {}
                self.archived_sessions = project_cache.sessions
            else:
                self.sessions = {}
                self.archived_sessions = {}
            # Update UI
            try:
                self.populate_table()
                self.update_stats()
            except Exception as e:
                # UI components may not be mounted yet during initialization
                self.log.debug(f"Skipped UI update for archived project: {e}")
            return

        # Check if we need to rebuild cache by checking for modified files
        # Exclude agent files - they are loaded via session references
        jsonl_files = [
            f
            for f in self.project_path.glob("*.jsonl")
            if not f.name.startswith("agent-")
        ]
        valid_session_ids = {f.stem for f in jsonl_files}
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

            except Exception as e:
                # Don't show notification during startup - log and return
                self.log.debug(f"Cache building failed during startup: {e}")
                return

        # Only compute archived sessions if there are JSONL files to compare against
        # (in test environments, there may be cached sessions but no JSONL files)
        if valid_session_ids:
            # Load archived sessions (cached but JSONL deleted)
            self.archived_sessions = self.cache_manager.get_archived_sessions(
                valid_session_ids
            )

            # Filter current sessions to only those with existing JSONL files
            self.sessions = {
                sid: data
                for sid, data in self.sessions.items()
                if sid in valid_session_ids
            }
        else:
            # No JSONL files to compare - treat all sessions as current
            self.archived_sessions = {}

        # Only update UI if we're in app context
        try:
            self.populate_table()
            self.update_stats()
        except Exception as e:
            # UI components may not be mounted yet during initialization
            self.log.debug(f"Skipped UI update after session load: {e}")

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

        # Combine current and archived sessions with archived flag
        all_sessions: list[tuple[str, SessionCacheData, bool]] = []
        for session_id, session_data in self.sessions.items():
            all_sessions.append((session_id, session_data, False))
        for session_id, session_data in self.archived_sessions.items():
            all_sessions.append((session_id, session_data, True))

        # Filter by provider if selected
        if self.selected_provider:
            all_sessions = [
                (sid, data, archived)
                for sid, data, archived in all_sessions
                if self.provider_sessions.get(sid) == self.selected_provider
            ]

        # Sort all sessions by start time (newest first)
        sorted_sessions = sorted(
            all_sessions, key=lambda x: x[1].first_timestamp, reverse=True
        )

        # Add rows
        for session_id, session_data, is_archived in sorted_sessions:
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

            # Get title preview: ai_title (Claude Code's curated title)
            # wins over summary, then first user message preview.
            # Escape Rich markup to prevent MarkupError from square brackets
            # in paths like [/Users/foo/bar] being parsed as closing tags.
            preview = escape_markup(
                session_data.ai_title
                or session_data.summary
                or session_data.first_user_message
                or "No preview available"
            )
            # Add [ARCHIVED] indicator for archived sessions
            if is_archived:
                preview = f"\\[ARCHIVED] {preview}"

            # Add provider badge
            provider = self.provider_sessions.get(session_id, "claude")
            provider_badge = f"[{provider.upper()}] "

            table.add_row(
                session_id[:8],
                f"{provider_badge}{preview}",
                start_time,
                end_time,
                str(session_data.message_count),
                token_display,
            )

    def update_stats(self) -> None:
        """Update the project statistics display."""
        # Combine all sessions for stats
        all_sessions = {**self.sessions, **self.archived_sessions}
        total_sessions = len(all_sessions)
        total_messages = sum(s.message_count for s in all_sessions.values())
        total_tokens = sum(
            s.total_input_tokens + s.total_output_tokens for s in all_sessions.values()
        )

        # Calculate per-provider stats
        provider_counts: dict[str, int] = {}
        for session_id in all_sessions:
            provider = self.provider_sessions.get(session_id, "claude")
            provider_counts[provider] = provider_counts.get(provider, 0) + 1

        # Get project name using shared logic
        working_directories: List[str] = []
        try:
            working_directories = self.cache_manager.get_working_directories()
        except Exception:
            pass

        project_name = get_project_display_name(
            self.project_path.name, working_directories
        )

        # Find date range
        if all_sessions:
            timestamps = [
                s.first_timestamp for s in all_sessions.values() if s.first_timestamp
            ]
            earliest = min(timestamps) if timestamps else ""
            latest = (
                max(s.last_timestamp for s in all_sessions.values() if s.last_timestamp)
                if all_sessions
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

        # Show archived count if any
        archived_count = len(self.archived_sessions)
        if archived_count > 0:
            mode_indicator = f"({archived_count} archived)"
        else:
            mode_indicator = ""

        # Show provider filter if active
        filter_indicator = ""
        if self.selected_provider:
            filter_indicator = f" [Filter: {self.selected_provider.upper()}]"

        # Project section (left aligned)
        project_section = f"[bold]Project:[/bold] {project_name} {mode_indicator}{filter_indicator}".strip()

        # Sessions info section (center)
        provider_stats = " | ".join(
            f"{k}: {v}" for k, v in sorted(provider_counts.items())
        )
        sessions_section = f"[bold]Sessions:[/bold] {total_sessions:,} ({provider_stats}) | [bold]Messages:[/bold] {total_messages:,} | [bold]Tokens:[/bold] {total_tokens:,}"

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
                return dt.strftime("%Y-%m-%d %H:%M")
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
        try:
            table = cast(DataTable[str], self.query_one("#sessions-table", DataTable))
            row_data = table.get_row_at(table.cursor_row)
            if row_data:
                # Extract session ID from the first column (now just first 8 chars)
                session_id_display = str(row_data[0])
                # Find the full session ID in both dicts (current first, then archived)
                for full_session_id in self.sessions.keys():
                    if full_session_id.startswith(session_id_display):
                        self.selected_session_id = full_session_id
                        return
                for full_session_id in self.archived_sessions.keys():
                    if full_session_id.startswith(session_id_display):
                        self.selected_session_id = full_session_id
                        return
        except Exception:
            # If widget not mounted yet or we can't get the row data, don't update selection
            pass

    def _export_to_browser(self, format: str, *, force: bool = False) -> None:
        """Export session to file and open in browser.

        Args:
            format: Output format - "html" or "md".
            force: If True, always regenerate even if file is up-to-date.
        """
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        format_name = "HTML" if format == "html" else "Markdown"
        try:
            session_file = self._ensure_session_file(
                self.selected_session_id, format, force=force
            )
            if session_file is None:
                self.notify(f"Failed to generate {format_name} file", severity="error")
                return

            webbrowser.open(f"file://{session_file}")
            msg = (
                f"Regenerated: {session_file.name}"
                if force
                else f"Opened: {session_file.name}"
            )
            self.notify(msg)

        except Exception as e:
            self.notify(f"Error with {format_name}: {e}", severity="error")

    def _view_markdown_embedded(self, *, force: bool = False) -> None:
        """View session Markdown in embedded viewer.

        Args:
            force: If True, always regenerate even if file is up-to-date.
        """
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        try:
            session_file = self._ensure_session_file(
                self.selected_session_id, "md", force=force
            )
            if session_file is None:
                self.notify("Failed to generate Markdown file", severity="error")
                return

            content = session_file.read_text(encoding="utf-8", errors="replace")
            title = f"Session: {self.selected_session_id[:8]}..."
            self.push_screen(MarkdownViewerScreen(content, title))
            if force:
                self.notify(f"Regenerated: {session_file.name}")

        except Exception as e:
            self.notify(f"Error viewing Markdown: {e}", severity="error")

    def action_export_selected(self) -> None:
        """Export the selected session to HTML and open in browser."""
        self._export_to_browser("html")

    def action_export_markdown(self) -> None:
        """Export the selected session to Markdown and open in browser."""
        self._export_to_browser("md")

    def action_view_markdown(self) -> None:
        """View the selected session's Markdown in an embedded viewer."""
        self._view_markdown_embedded()

    def action_force_export_html(self) -> None:
        """Force regenerate HTML and open in browser (hidden shortcut: H)."""
        self._export_to_browser("html", force=True)

    def action_force_export_markdown(self) -> None:
        """Force regenerate Markdown and open in browser (hidden shortcut: M)."""
        self._export_to_browser("md", force=True)

    def action_force_view_markdown(self) -> None:
        """Force regenerate and view Markdown in embedded viewer (hidden shortcut: V)."""
        self._view_markdown_embedded(force=True)

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

    def _update_expanded_content(self) -> None:
        """Update the expanded content for the currently selected session."""
        if not self.selected_session_id:
            return

        # Get session data from either current or archived sessions
        session_data = self.sessions.get(
            self.selected_session_id
        ) or self.archived_sessions.get(self.selected_session_id)
        if not session_data:
            return

        expanded_content = self.query_one("#expanded-content", Static)

        # Build expanded content
        content_parts: list[str] = []

        # Session ID (safe - UUID format)
        content_parts.append(f"[bold]Session ID:[/bold] {self.selected_session_id}")

        # AI title (Claude Code's curated short title) - escape markup
        if session_data.ai_title:
            escaped_title = escape_markup(session_data.ai_title)
            content_parts.append(f"\n[bold]Title:[/bold] {escaped_title}")

        # Summary (if available) - escape markup
        if session_data.summary:
            escaped_summary = escape_markup(session_data.summary)
            content_parts.append(f"\n[bold]Summary:[/bold] {escaped_summary}")

        # First user message - escape markup
        if session_data.first_user_message:
            escaped_message = escape_markup(session_data.first_user_message)
            content_parts.append(
                f"\n[bold]First User Message:[/bold] {escaped_message}"
            )

        # Working directory (if available) - escape markup
        if session_data.cwd:
            escaped_cwd = escape_markup(session_data.cwd)
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

    def _ensure_session_file(
        self, session_id: str, format: str, *, force: bool = False
    ) -> Optional[Path]:
        """Ensure the session file exists and is up-to-date.

        Regenerates the file if it doesn't exist or is outdated.

        Args:
            session_id: The session ID to generate a file for.
            format: Output format - "html" or "md".
            force: If True, always regenerate even if file is up-to-date.

        Returns:
            Path to the file if successful, None if regeneration failed.
        """
        ext = get_file_extension(format)
        session_file = self.project_path / f"session-{session_id}.{ext}"
        renderer = get_renderer(format)

        # Check if we need to regenerate
        needs_regeneration = (
            force or not session_file.exists() or renderer.is_outdated(session_file)
        )

        if not needs_regeneration:
            return session_file

        # Load messages - from cache for archived sessions, from JSONL otherwise
        try:
            is_archived = session_id in self.archived_sessions
            if is_archived:
                # Load from cache for archived sessions
                messages = self.cache_manager.load_session_entries(session_id)
            else:
                # Load from JSONL files for current sessions
                messages, _tree = load_directory_transcripts(
                    self.project_path, self.cache_manager, silent=True
                )
            if not messages:
                return None

            # Build session title - check both dicts
            session_data = self.sessions.get(session_id) or self.archived_sessions.get(
                session_id
            )
            project_cache = self.cache_manager.get_cached_project_data()
            project_name = get_project_display_name(
                self.project_path.name,
                project_cache.working_directories if project_cache else None,
            )
            session_title = build_session_title(project_name, session_id, session_data)

            # Generate session content
            session_content = renderer.generate_session(
                messages,
                session_id,
                session_title,
                self.cache_manager,
                self.project_path,
            )
            if session_content:
                # Scrub lone surrogates to U+FFFD before strict-UTF-8 write
                # (#139). `errors="replace"` is defense-in-depth — the
                # broader `try/except` below silently swallows any residual
                # encode error, and "Failed to generate" with no traceback
                # is the worst outcome (worse than the CLI's loud crash,
                # which is how #139 surfaced in the first place).
                scrubbed = scrub_surrogates(session_content) or session_content
                session_file.write_text(scrubbed, encoding="utf-8", errors="replace")
                return session_file
        except Exception:
            return None

        return None

    def action_toggle_expanded(self) -> None:
        """Toggle the expanded view for the selected session."""
        if not self.selected_session_id:
            return
        # Check if session exists in either current or archived sessions
        if (
            self.selected_session_id not in self.sessions
            and self.selected_session_id not in self.archived_sessions
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
            "- Expanded content updates automatically when visible\n"
            "- [ARCHIVED] sessions have no JSONL file (cache only)\n\n"
            "Actions:\n"
            "- a: Archive session (delete JSONL, keep in cache)\n"
            "- d: Delete session (with options)\n"
            "- r: Restore archived session to JSONL\n"
            "- e: Toggle expanded view for session\n"
            "- h: Open selected session's HTML page\n"
            "- m: Open selected session's Markdown file (in browser)\n"
            "- v: View Markdown in embedded viewer\n"
            "- c: Resume selected session in Claude Code\n"
            "- p: Open project selector\n"
            "- q: Quit\n\n"
        )
        self.notify(help_text, timeout=10)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Conditionally enable/disable actions based on selected session type."""
        if not self.selected_session_id:
            return True  # Allow action, it will handle missing selection

        is_archived = self.selected_session_id in self.archived_sessions
        is_current = self.selected_session_id in self.sessions

        # Archive is only available for current sessions (has JSONL file)
        if action == "archive_session" and not is_current:
            return False
        # Resume is only available for current sessions
        if action == "resume_selected" and not is_current:
            return False
        # Restore is only available for archived sessions
        if action == "restore_jsonl" and not is_archived:
            return False
        return True

    def action_restore_jsonl(self) -> None:
        """Restore the selected archived session to a JSONL file."""
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        if self.selected_session_id not in self.archived_sessions:
            self.notify(
                "Selected session not found in archived sessions", severity="error"
            )
            return

        try:
            # Export messages from cache
            messages = self.cache_manager.export_session_to_jsonl(
                self.selected_session_id
            )
            if not messages:
                self.notify("No messages found for session", severity="error")
                return

            # Ensure project directory exists (may have been deleted)
            self.project_path.mkdir(parents=True, exist_ok=True)

            # Write to JSONL file
            output_path = self.project_path / f"{self.selected_session_id}.jsonl"
            with open(output_path, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(msg + "\n")

            self.notify(
                f"Restored {len(messages)} messages to {output_path.name}",
                severity="information",
            )

            # Refresh to show the restored session as current
            self._refresh_after_restore()

        except Exception as e:
            self.notify(f"Error restoring session: {e}", severity="error")

    def _refresh_after_restore(self) -> None:
        """Refresh sessions after restoring an archived session."""
        # If this was a fully archived project, it's no longer archived
        # since we just restored a JSONL file
        if self.is_archived_project:
            self.is_archived_project = False

        # Reload sessions - this will now detect the restored JSONL file
        self.load_sessions()

        self.notify(
            "Session restored! It now appears as a current session.",
            timeout=5,
        )

    def action_archive_session(self) -> None:
        """Archive the selected session (delete JSONL file, keep in cache)."""
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        # Archive only works for current sessions (those with JSONL files)
        if self.selected_session_id not in self.sessions:
            self.notify(
                "Only current sessions can be archived (already archived or not found)",
                severity="warning",
            )
            return

        # Push archive confirmation screen
        self.push_screen(
            ArchiveConfirmScreen(session_id=self.selected_session_id),
            callback=self._on_archive_confirm,
        )

    def _on_archive_confirm(self, confirmed: Optional[bool]) -> None:
        """Handle archive confirmation result."""
        if not confirmed or not self.selected_session_id:
            return

        try:
            # Delete the JSONL file
            jsonl_path = self.project_path / f"{self.selected_session_id}.jsonl"
            if jsonl_path.exists():
                jsonl_path.unlink()
                self.notify(
                    f"Session {self.selected_session_id[:8]} archived",
                    severity="information",
                )
                # Reload sessions - this will move the session to archived
                self.load_sessions()
            else:
                self.notify("JSONL file not found", severity="error")
        except Exception as e:
            self.notify(f"Error archiving session: {e}", severity="error")

    def action_delete_session(self) -> None:
        """Delete the selected session with smart options."""
        if not self.selected_session_id:
            self.notify("No session selected", severity="warning")
            return

        # Check if session exists in either current or archived sessions
        if (
            self.selected_session_id not in self.sessions
            and self.selected_session_id not in self.archived_sessions
        ):
            self.notify("Selected session not found", severity="error")
            return

        # Determine if this is an archived session (no JSONL to fall back on)
        is_archived_session = self.selected_session_id in self.archived_sessions

        # Push confirmation screen
        self.push_screen(
            DeleteConfirmScreen(
                session_id=self.selected_session_id,
                is_archived=is_archived_session,
            ),
            callback=self._on_delete_confirm,
        )

    def _on_delete_confirm(self, delete_option: Optional[str]) -> None:
        """Handle deletion confirmation result."""
        if not delete_option or not self.selected_session_id:
            return

        try:
            deleted_what: list[str] = []

            # Delete JSONL file if requested
            if delete_option == "both":
                jsonl_path = self.project_path / f"{self.selected_session_id}.jsonl"
                if jsonl_path.exists():
                    jsonl_path.unlink()
                    deleted_what.append("JSONL file")

            # Delete from cache
            success = self.cache_manager.delete_session(self.selected_session_id)
            if success:
                deleted_what.append("cache")

            if deleted_what:
                self.notify(
                    f"Session {self.selected_session_id[:8]} deleted ({', '.join(deleted_what)})",
                    severity="information",
                )
                # Clear selection and reload
                self.selected_session_id = None
                self.load_sessions()
            else:
                self.notify("Failed to delete session", severity="error")
        except Exception as e:
            self.notify(f"Error deleting session: {e}", severity="error")

    def action_back_to_projects(self) -> None:
        """Navigate to the project selector."""
        # Exit with a special return value to signal we want to go to project selector
        self.exit(result="back_to_projects")

    def action_filter_provider(self) -> None:
        """Cycle through provider filters: None -> claude -> codex -> gemini -> opencode -> agy -> None"""
        providers = [None, "claude", "codex", "gemini", "opencode", "agy"]
        current_idx = (
            providers.index(self.selected_provider)
            if self.selected_provider in providers
            else 0
        )
        next_idx = (current_idx + 1) % len(providers)
        self.selected_provider = providers[next_idx]
        self.populate_table()
        if self.selected_provider:
            self.notify(
                f"Filtering by provider: {self.selected_provider}",
                severity="information",
            )
        else:
            self.notify("Showing all providers", severity="information")

    async def action_quit(self) -> None:
        """Quit the application with proper cleanup."""
        self.exit()


def run_project_selector(
    projects: list[Path],
    matching_projects: list[Path],
    archived_projects: Optional[set[Path]] = None,
) -> Optional[Path]:
    """Run the project selector TUI and return the selected project path."""
    if not projects:
        print("Error: No projects provided")
        return None

    app = ProjectSelector(projects, matching_projects, archived_projects)
    try:
        return app.run()
    except KeyboardInterrupt:
        # Textual handles terminal cleanup automatically
        print("\nInterrupted")
        return None


def run_session_browser(project_path: Path, is_archived: bool = False) -> Optional[str]:
    """Run the session browser TUI for the given project path."""
    if not project_path.exists():
        # For archived projects, the directory may not exist but cache may
        if is_archived:
            # Try to load from cache
            try:
                cache_manager = CacheManager(project_path, get_library_version())
                project_cache = cache_manager.get_cached_project_data()
                if project_cache and project_cache.sessions:
                    app = SessionBrowser(project_path, is_archived=True)
                    return app.run()
            except Exception:
                pass
        print(f"Error: Project path {project_path} does not exist")
        return None

    if not project_path.is_dir():
        print(f"Error: {project_path} is not a directory")
        return None

    # Check if there are any JSONL files
    jsonl_files = list(project_path.glob("*.jsonl"))
    if not jsonl_files:
        # For archived projects, check if we have cached sessions
        if is_archived:
            try:
                cache_manager = CacheManager(project_path, get_library_version())
                project_cache = cache_manager.get_cached_project_data()
                if project_cache and project_cache.sessions:
                    app = SessionBrowser(project_path, is_archived=True)
                    return app.run()
            except Exception:
                pass
        print(f"Error: No JSONL transcript files found in {project_path}")
        return None

    app = SessionBrowser(project_path, is_archived=is_archived)
    try:
        return app.run()
    except KeyboardInterrupt:
        # Textual handles terminal cleanup automatically
        print("\nInterrupted")
        return None
