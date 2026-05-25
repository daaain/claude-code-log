"""HTML renderer implementation for Claude Code transcripts."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple, cast

if TYPE_CHECKING:
    from ..dag import SessionTree

from ..cache import get_library_version
from ..models import (
    AssistantTextMessage,
    AwaySummaryMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    DetailLevel,
    HookAttachmentMessage,
    HookSummaryMessage,
    ImageContent,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskNotificationMessage,
    TeammateMessage,
    ThinkingMessage,
    ToolUseMessage,
    TranscriptEntry,
    UnknownMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserTextMessage,
    # Tool input types
    AskUserQuestionInput,
    BashInput,
    EditInput,
    ExitPlanModeInput,
    GlobInput,
    GrepInput,
    MultiEditInput,
    ReadInput,
    SendMessageInput,
    TaskCreateInput,
    TaskInput,
    TaskListInput,
    TaskOutputInput,
    TaskStopInput,
    TaskUpdateInput,
    TeamCreateInput,
    TeamDeleteInput,
    TodoWriteInput,
    ToolUseContent,
    SkillInput,
    WebSearchInput,
    WebFetchInput,
    MonitorInput,
    MonitorOutput,
    ScheduleWakeupInput,
    ScheduleWakeupOutput,
    CronCreateInput,
    CronCreateOutput,
    CronListInput,
    CronListOutput,
    CronDeleteInput,
    CronDeleteOutput,
    WriteInput,
    # Tool output types
    AskUserQuestionOutput,
    BashOutput,
    EditOutput,
    ExitPlanModeOutput,
    ReadOutput,
    SendMessageOutput,
    TaskCreateOutput,
    TaskListOutput,
    TaskOutput,
    TaskOutputResult,
    TaskStopOutput,
    TaskUpdateOutput,
    TeamCreateOutput,
    TeamDeleteOutput,
    ToolResultContent,
    WebSearchOutput,
    WebFetchOutput,
    WriteOutput,
)
from ..renderer import (
    Renderer,
    TemplateMessage,
    generate_template_messages,
    prepare_projects_index,
    title_for_projects_index,
)
from ..renderer_timings import (
    DEBUG_TIMING,
    log_timing,
    report_timing_statistics,
    set_timing_var,
)
from ..utils import format_timestamp
from .system_formatters import (
    format_away_summary_content,
    format_hook_attachment_content,
    format_hook_summary_content,
    format_session_header_content,
    format_system_content,
)
from .user_formatters import (
    format_bash_input_content,
    format_bash_output_content,
    format_command_output_content,
    format_compacted_summary_content,
    format_slash_command_content,
    format_user_memory_content,
    format_user_slash_command_content,
    format_user_text_model_content,
)
from .assistant_formatters import (
    format_assistant_text_content,
    format_thinking_content,
    format_unknown_content,
)
from .teammate_formatter import (
    format_sendmessage_input as _format_sendmessage_input,
    format_sendmessage_output as _format_sendmessage_output,
    format_task_input_teammate_extras,
    format_task_output_teammate_extras,
    format_taskcreate_input as _format_taskcreate_input,
    format_taskcreate_output as _format_taskcreate_output,
    format_tasklist_output as _format_tasklist_output,
    format_taskupdate_input as _format_taskupdate_input,
    format_taskupdate_output as _format_taskupdate_output,
    format_teamcreate_input as _format_teamcreate_input,
    format_teamcreate_output as _format_teamcreate_output,
    format_teamdelete_input as _format_teamdelete_input,
    format_teamdelete_output as _format_teamdelete_output,
    format_teammate_content,
)
from .async_formatter import (
    format_task_notification_content as _format_task_notification_content,
    format_taskoutput_input as _format_taskoutput_input,
    format_taskoutput_output as _format_taskoutput_output,
)
from .tool_formatters import (
    format_askuserquestion_input,
    format_askuserquestion_output,
    format_bash_input,
    format_bash_output,
    format_edit_input,
    format_edit_output,
    format_exitplanmode_input,
    format_exitplanmode_output,
    format_multiedit_input,
    format_read_input,
    format_read_output,
    format_task_input,
    format_task_output,
    format_taskstop_input,
    format_taskstop_output,
    format_todowrite_input,
    format_tool_result_content_raw,
    format_grep_input,
    format_websearch_input,
    format_websearch_output,
    format_webfetch_input,
    format_webfetch_output,
    format_monitor_input,
    format_monitor_output,
    format_schedulewakeup_input,
    format_schedulewakeup_output,
    format_croncreate_input,
    format_croncreate_output,
    format_cronlist_input,
    format_cronlist_output,
    format_crondelete_input,
    format_crondelete_output,
    format_write_input,
    format_write_output,
    render_params_table,
)
from .utils import (
    css_class_from_message,
    escape_html,
    get_message_emoji,
    get_template_environment,
    is_session_header,
    render_markdown_collapsible,
)

if TYPE_CHECKING:
    from ..cache import CacheManager


def check_html_version(html_file_path: Path) -> Optional[str]:
    """Check the version of an existing HTML file from its comment.

    Returns:
        The version string if found, None if no version comment or file doesn't exist.
    """
    if not html_file_path.exists():
        return None

    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            # Read only the first few lines to find the version comment
            for _ in range(5):  # Check first 5 lines
                line = f.readline()
                if not line:
                    break
                # Look for comment like: <!-- Generated by claude-code-log v0.3.4 -->
                if "<!-- Generated by claude-code-log v" in line:
                    # Extract version between 'v' and ' -->'
                    start = line.find("v") + 1
                    end = line.find(" -->")
                    if start > 0 and end > start:
                        return line[start:end]
    except (IOError, UnicodeDecodeError):
        pass

    return None


def _build_html_project_tree(template_projects: list[Any]) -> dict[str, Any]:
    """Build a nested directory tree from project paths for the HTML index.

    Each project lands at the directory level of its ``html_file``'s
    parent (i.e. the rel-dest directory under the index root). The
    returned shape is a recursive dict:

    ::

        {
          "_projects": [TemplateProject, ...],   # leaves at this level
          "<subdir-name>": <subtree>,
        }

    Directories are sorted alphabetically by the recursive template
    macro; the ``_projects`` lists keep their insertion order (which
    is the by-last-modified order set in ``prepare_projects_index``).
    """

    def _to_posix(s: str) -> str:
        return s.replace("\\", "/")

    root: dict[str, Any] = {}
    for project in template_projects:
        # Use html_file's parent path components as the directory chain.
        url = _to_posix(project.html_file)
        parts = url.split("/")
        node = root
        # All but the last component are directories.
        for part in parts[:-1]:
            if not part:
                continue
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node.setdefault("_projects", []).append(project)
    return root


class HtmlRenderer(Renderer):
    """HTML renderer for Claude Code transcripts."""

    # Consulted by Renderer._dispatch_format Strategy 2: plugin-defined
    # content classes contributing a ``format_html`` method get picked up
    # here. See dev-docs/plugins.md §5 for the resolution order.
    #
    # v1 contract: ``format_html`` MUST return a real string. The
    # absence of the method on a plugin class drives the fallback —
    # ``_dispatch_format`` (overridden below) synthesizes HTML from
    # the class-side ``format_markdown`` via mistune when only the
    # Markdown side is implemented. There is no None-as-sentinel.
    _class_dispatch_format: str = "html"

    def _dispatch_format(self, obj: Any, message: "TemplateMessage") -> str:
        """HtmlRenderer-specific dispatch with Markdown→HTML synthesis.

        Resolution order on the actual class (`type(obj)`):

        1. Class defines ``format_html`` in its ``__dict__`` → use it
           verbatim. The return MUST be a real string (no None sentinel).
        2. Class defines ``format_markdown`` (but not ``format_html``)
           in its ``__dict__`` → synthesize HTML by rendering the
           Markdown via mistune, wrapped in ``<div class="markdown">``
           so theme rules scoped under ``.markdown`` fire. By
           definition the synthesized output is Markdown-derived, so
           the wrap is automatic — plugin authors don't need
           ``has_markdown = True`` for this path.
        3. Neither on the actual class → defer to the base MRO walk
           (which finds renderer-side ``format_<ClassName>`` methods
           for built-in content classes, or class-side methods on
           ancestors).

        Step 2 deliberately wins over an ancestor's renderer-side
        ``format_<ClassName>``: a plugin author who defined
        ``format_markdown`` on their subclass meant for their Markdown
        to drive the rendering, not for the parent class's built-in
        renderer behaviour to take over.
        """
        from .utils import render_markdown

        # ``obj`` is intentionally untyped (``Any``); the class-side
        # methods we look up on its ``__dict__`` are plugin-defined.
        obj_cls = cast("type[object]", type(obj))
        html_method = obj_cls.__dict__.get("format_html")
        if html_method is not None:
            return cast(str, html_method(obj, self, message))
        md_method = obj_cls.__dict__.get("format_markdown")
        if md_method is not None:
            md_source = cast(str, md_method(obj, self, message))
            return f'<div class="markdown">{render_markdown(md_source)}</div>'
        return super()._dispatch_format(obj, message)

    def __init__(self, image_export_mode: str = "embedded"):
        """Initialize the HTML renderer.

        Args:
            image_export_mode: Image export mode - "placeholder", "embedded", or "referenced".
        """
        super().__init__()
        self.image_export_mode = image_export_mode
        self._output_dir: Path | None = None
        self._image_counter = 0
        # session_id -> {teammate_id -> color}, snapshotted from the
        # RenderingContext at the start of each render. Formatters look
        # up the per-session map via self._colors_for(message) so
        # combined transcripts don't cross-contaminate teammate colors
        # between sessions.
        self._teammate_colors_by_session: dict[str, dict[str, str]] = {}
        # session_id -> {task_id -> subject}, populated by
        # `_populate_task_metadata` from TaskCreate/TaskList tool_results
        # so TaskCreate / TaskUpdate tool_use titles can surface the
        # human-readable subject. Empty for sessions without task
        # activity.
        self._task_subjects_by_session: dict[str, dict[str, str]] = {}
        # session_id -> {tool_use_id -> task_id}, populated alongside
        # task_subjects so the TaskCreate tool_use title can render the
        # backend-assigned ``#N`` (TaskCreateInput itself doesn't carry
        # the id; it's minted on creation and only appears on the
        # tool_result).
        self._task_id_by_tool_use: dict[str, dict[str, str]] = {}

    # -------------------------------------------------------------------------
    # Private Utility Methods
    # -------------------------------------------------------------------------

    def _colors_for(self, message: TemplateMessage) -> dict[str, str]:
        """Return the teammate_id→color map scoped to *message*'s session.

        Empty dict when the session has no known teammate colors yet.
        Scoping matters for combined transcripts (see
        RenderingContext.teammate_colors).
        """
        sid = message.meta.session_id if message.meta else ""
        return self._teammate_colors_by_session.get(sid, {})

    def _format_image(self, image: ImageContent) -> str:
        """Format image based on export mode."""
        from ..image_export import export_image

        self._image_counter += 1
        src = export_image(
            image,
            self.image_export_mode,
            output_dir=self._output_dir,
            counter=self._image_counter,
        )
        if src is None:
            return "[Image]"
        return f'<img src="{src}" alt="image" class="uploaded-image" />'

    # -------------------------------------------------------------------------
    # System Content Formatters
    # -------------------------------------------------------------------------

    def format_SystemMessage(self, content: SystemMessage, _: TemplateMessage) -> str:
        return format_system_content(content)

    def format_HookSummaryMessage(
        self, content: HookSummaryMessage, _: TemplateMessage
    ) -> str:
        return format_hook_summary_content(content)

    def format_HookAttachmentMessage(
        self, content: HookAttachmentMessage, _: TemplateMessage
    ) -> str:
        return format_hook_attachment_content(content)

    def format_AwaySummaryMessage(
        self, content: AwaySummaryMessage, _: TemplateMessage
    ) -> str:
        return format_away_summary_content(content)

    def format_SessionHeaderMessage(
        self, content: SessionHeaderMessage, _: TemplateMessage
    ) -> str:
        return format_session_header_content(content)

    # -------------------------------------------------------------------------
    # User Content Formatters
    # -------------------------------------------------------------------------

    def format_UserTextMessage(
        self, content: UserTextMessage, _: TemplateMessage
    ) -> str:
        return format_user_text_model_content(
            content, image_formatter=self._format_image
        )

    def format_UserSlashCommandMessage(
        self, content: UserSlashCommandMessage, _: TemplateMessage
    ) -> str:
        return format_user_slash_command_content(content)

    def format_SlashCommandMessage(
        self, content: SlashCommandMessage, _: TemplateMessage
    ) -> str:
        return format_slash_command_content(content)

    def format_CommandOutputMessage(
        self, content: CommandOutputMessage, _: TemplateMessage
    ) -> str:
        return format_command_output_content(content)

    def format_BashInputMessage(
        self, content: BashInputMessage, _: TemplateMessage
    ) -> str:
        return format_bash_input_content(content)

    def format_BashOutputMessage(
        self, content: BashOutputMessage, _: TemplateMessage
    ) -> str:
        return format_bash_output_content(content)

    def format_CompactedSummaryMessage(
        self, content: CompactedSummaryMessage, _: TemplateMessage
    ) -> str:
        return format_compacted_summary_content(content)

    def format_UserMemoryMessage(
        self, content: UserMemoryMessage, _: TemplateMessage
    ) -> str:
        return format_user_memory_content(content)

    def format_TeammateMessage(
        self, content: TeammateMessage, _: TemplateMessage
    ) -> str:
        """Format → one colored card per <teammate-message> block.

        Passes the session-wide teammate_colors map so blocks without an
        inline color still inherit the teammate's learned color.
        """
        return format_teammate_content(content, self._colors_for(_))

    def format_TaskNotificationMessage(
        self, content: TaskNotificationMessage, _: TemplateMessage
    ) -> str:
        """Format → metadata `<dl>` + collapsible Markdown body for an
        async-agent ``<task-notification>`` user entry (issue #90).

        At ``DetailLevel.LOW`` a duplicate-flagged notification renders
        empty so the rendering loop's "skip empty messages" elision
        drops the card — the spawn-fold already shows the answer in
        place, and "ghosting" via empty output avoids the index-remap
        cascade that deleting the message would trigger (ancestry
        classes, backlinks, session nav anchors).
        """
        if self.detail == DetailLevel.LOW and content.result_is_duplicate:
            return ""
        return _format_task_notification_content(content)

    # -------------------------------------------------------------------------
    # Assistant Content Formatters
    # -------------------------------------------------------------------------

    def format_AssistantTextMessage(
        self, content: AssistantTextMessage, _: TemplateMessage
    ) -> str:
        return format_assistant_text_content(
            content, image_formatter=self._format_image
        )

    def format_ThinkingMessage(
        self, content: ThinkingMessage, _: TemplateMessage
    ) -> str:
        """Format → <details class='thinking'>...</details> (foldable if >10 lines)."""
        return format_thinking_content(content, line_threshold=10)

    def format_UnknownMessage(self, content: UnknownMessage, _: TemplateMessage) -> str:
        """Format → <pre class='unknown'>JSON dump</pre>."""
        return format_unknown_content(content)

    # -------------------------------------------------------------------------
    # Tool Input Formatters
    # -------------------------------------------------------------------------

    def format_BashInput(self, input: BashInput, _: TemplateMessage) -> str:
        """Format → <pre>$ command</pre>."""
        return format_bash_input(input)

    def format_ReadInput(self, input: ReadInput, _: TemplateMessage) -> str:
        """Format → <table class='params'>file_path | ...</table>."""
        return format_read_input(input)

    def format_WriteInput(self, input: WriteInput, _: TemplateMessage) -> str:
        """Format → file path + syntax-highlighted content preview."""
        return format_write_input(input)

    def format_EditInput(self, input: EditInput, _: TemplateMessage) -> str:
        """Format → file path + diff of old_string/new_string."""
        return format_edit_input(input)

    def format_MultiEditInput(self, input: MultiEditInput, _: TemplateMessage) -> str:
        """Format → file path + multiple diffs."""
        return format_multiedit_input(input)

    def format_TaskInput(self, input: TaskInput, _: TemplateMessage) -> str:
        """Format → prompt text, plus teammate-spawn extras when relevant."""
        base = format_task_input(input)
        extras = format_task_input_teammate_extras(input, self._colors_for(_))
        return base + extras if extras else base

    def format_TodoWriteInput(self, input: TodoWriteInput, _: TemplateMessage) -> str:
        """Format → <ul class='todo-list'>...</ul>."""
        return format_todowrite_input(input)

    def format_AskUserQuestionInput(
        self, input: AskUserQuestionInput, _: TemplateMessage
    ) -> str:
        """Format → questions as definition list."""
        return format_askuserquestion_input(input)

    def format_ExitPlanModeInput(
        self, input: ExitPlanModeInput, _: TemplateMessage
    ) -> str:
        """Format → empty string (no content)."""
        return format_exitplanmode_input(input)

    def format_GrepInput(self, input: GrepInput, _: TemplateMessage) -> str:
        """Format → params table (path, glob, type, etc.) without pattern."""
        return format_grep_input(input)

    def format_WebSearchInput(self, input: WebSearchInput, _: TemplateMessage) -> str:
        """Format → search query display."""
        return format_websearch_input(input)

    def format_SkillInput(self, _input: SkillInput, _: TemplateMessage) -> str:
        """Format → empty: skill name moves to the title, body folds in via skill_body."""
        return ""

    # --- Teammate-feature tool inputs --------------------------------------

    def format_TeamCreateInput(self, input: TeamCreateInput, _: TemplateMessage) -> str:
        """Format → team-card with team_name / description / agent_type."""
        return _format_teamcreate_input(input)

    def format_TeamDeleteInput(self, input: TeamDeleteInput, _: TemplateMessage) -> str:
        """Format → empty placeholder (TeamDelete takes no meaningful params)."""
        return _format_teamdelete_input(input)

    def format_TaskCreateInput(self, input: TaskCreateInput, _: TemplateMessage) -> str:
        """Format → task-create card."""
        return _format_taskcreate_input(input)

    def format_TaskUpdateInput(self, input: TaskUpdateInput, _: TemplateMessage) -> str:
        """Format → task-update card (colored owner badge when present)."""
        return _format_taskupdate_input(input, self._colors_for(_))

    def format_TaskListInput(self, input: TaskListInput, _: TemplateMessage) -> str:
        """Format → empty placeholder (TaskList takes no params)."""
        del input  # TaskListInput has no surfaces worth rendering
        return ""

    def format_SendMessageInput(
        self, input: SendMessageInput, _: TemplateMessage
    ) -> str:
        """Format → send-message card with colored recipient badge."""
        return _format_sendmessage_input(input, self._colors_for(_))

    def format_TaskOutputInput(self, input: TaskOutputInput, _: TemplateMessage) -> str:
        """Format → minimal TaskOutput input card (block / timeout if set)."""
        return _format_taskoutput_input(input)

    def format_TaskStopInput(self, input: TaskStopInput, _: TemplateMessage) -> str:
        """Format → empty (id lives in the title, no further params)."""
        return format_taskstop_input(input)

    def format_ToolUseContent(self, content: ToolUseContent, _: TemplateMessage) -> str:
        """Format → <table class='params'>key | value rows</table>."""
        return render_params_table(content.input)

    def format_ToolUseMessage(
        self, content: ToolUseMessage, message: TemplateMessage
    ) -> str:
        """Format Skill tool_use with folded skill body (issue #93).

        For every other tool_use, delegate to the base dispatcher
        (→ params table). For Skill, append the expanded skill body —
        set by `_pair_skill_tool_uses` from the matching isMeta=True
        slash-command entry — as collapsible markdown below the params.
        """
        rendered = super().format_ToolUseMessage(content, message)
        if content.skill_body:
            body_html = render_markdown_collapsible(
                content.skill_body,
                "skill-body",
                line_threshold=30,
                preview_line_count=10,
            )
            rendered = f"{rendered}\n{body_html}"
        return rendered

    # -------------------------------------------------------------------------
    # Tool Output Formatters
    # -------------------------------------------------------------------------

    def format_ReadOutput(self, output: ReadOutput, _: TemplateMessage) -> str:
        """Format → syntax-highlighted file content."""
        return format_read_output(output)

    def format_WriteOutput(self, output: WriteOutput, _: TemplateMessage) -> str:
        """Format → status message (e.g. 'Wrote 42 bytes')."""
        return format_write_output(output)

    def format_EditOutput(self, output: EditOutput, _: TemplateMessage) -> str:
        """Format → status message (e.g. 'Applied edit')."""
        return format_edit_output(output)

    def format_BashOutput(self, output: BashOutput, _: TemplateMessage) -> str:
        """Format → <pre>stdout/stderr</pre>."""
        return format_bash_output(output)

    def format_TaskOutput(self, output: TaskOutput, _: TemplateMessage) -> str:
        """Format → markdown of task result plus teammate-metadata extras."""
        base = format_task_output(output)
        extras = format_task_output_teammate_extras(output, self._colors_for(_))
        return base + extras if extras else base

    def format_AskUserQuestionOutput(
        self, output: AskUserQuestionOutput, _: TemplateMessage
    ) -> str:
        """Format → user's answers as definition list."""
        return format_askuserquestion_output(output)

    def format_ExitPlanModeOutput(
        self, output: ExitPlanModeOutput, _: TemplateMessage
    ) -> str:
        """Format → status message."""
        return format_exitplanmode_output(output)

    def format_WebSearchOutput(
        self, output: WebSearchOutput, _: TemplateMessage
    ) -> str:
        """Format → list of clickable search result links."""
        return format_websearch_output(output)

    # --- Teammate-feature tool outputs -------------------------------------

    def format_TeamCreateOutput(
        self, output: TeamCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → team-card with team_name / lead / config path."""
        return _format_teamcreate_output(output)

    def format_TeamDeleteOutput(
        self, output: TeamDeleteOutput, _: TemplateMessage
    ) -> str:
        """Format → success/refused notice; active members listed when blocked."""
        return _format_teamdelete_output(output, self._colors_for(_))

    def format_TaskCreateOutput(
        self, output: TaskCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → created task id + subject."""
        return _format_taskcreate_output(output)

    def format_TaskUpdateOutput(
        self, output: TaskUpdateOutput, _: TemplateMessage
    ) -> str:
        """Format → task id + updated fields (+ transition if present)."""
        return _format_taskupdate_output(output)

    def format_TaskListOutput(self, output: TaskListOutput, _: TemplateMessage) -> str:
        """Format → <table class='task-list'> with id/status/subject/owner."""
        return _format_tasklist_output(output, self._colors_for(_))

    def format_SendMessageOutput(
        self, output: SendMessageOutput, _: TemplateMessage
    ) -> str:
        """Format → sent/failed notice with colored target badge + request id."""
        return _format_sendmessage_output(output, self._colors_for(_))

    def format_TaskOutputResult(
        self, output: TaskOutputResult, _: TemplateMessage
    ) -> str:
        """Format → minimal TaskOutput result card (metadata only, no transcript)."""
        return _format_taskoutput_output(output)

    def format_TaskStopOutput(self, output: TaskStopOutput, _: TemplateMessage) -> str:
        """Format → ``Stopped`` / ``Not stopped`` badge + message body."""
        return format_taskstop_output(output)

    def format_ToolResultContent(
        self, output: ToolResultContent, _: TemplateMessage
    ) -> str:
        """Format → <pre>raw content</pre> (fallback for unknown tools)."""
        return format_tool_result_content_raw(output)

    def format_WebFetchInput(self, input: WebFetchInput, _: TemplateMessage) -> str:
        """Format → prompt text if long, empty if shown in title."""
        return format_webfetch_input(input)

    def format_WebFetchOutput(self, output: WebFetchOutput, _: TemplateMessage) -> str:
        """Format → collapsible markdown with metadata badge."""
        return format_webfetch_output(output)

    def format_MonitorInput(self, input: MonitorInput, _: TemplateMessage) -> str:
        """Format → 4-row params table with collapsible command."""
        return format_monitor_input(input)

    def format_MonitorOutput(self, output: MonitorOutput, _: TemplateMessage) -> str:
        """Format → start-confirmation paragraph verbatim."""
        return format_monitor_output(output)

    # -- ScheduleWakeup / Cron* family ---------------------------------------

    def format_ScheduleWakeupInput(
        self, input: ScheduleWakeupInput, _: TemplateMessage
    ) -> str:
        """Format → 3-row grid (delaySeconds, reason, collapsible prompt)."""
        return format_schedulewakeup_input(input)

    def format_ScheduleWakeupOutput(
        self, output: ScheduleWakeupOutput, _: TemplateMessage
    ) -> str:
        """Format → ``Next wakeup scheduled for …`` paragraph verbatim."""
        return format_schedulewakeup_output(output)

    def format_CronCreateInput(self, input: CronCreateInput, _: TemplateMessage) -> str:
        """Format → grid (cron, recurring?, durable?, collapsible prompt)."""
        return format_croncreate_input(input)

    def format_CronCreateOutput(
        self, output: CronCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → confirmation paragraph verbatim."""
        return format_croncreate_output(output)

    def format_CronListInput(self, input: CronListInput, _: TemplateMessage) -> str:
        """Format → empty (no inputs)."""
        return format_cronlist_input(input)

    def format_CronListOutput(self, output: CronListOutput, _: TemplateMessage) -> str:
        """Format → table when parseable, raw text otherwise."""
        return format_cronlist_output(output)

    def format_CronDeleteInput(self, input: CronDeleteInput, _: TemplateMessage) -> str:
        """Format → empty (id is in the title)."""
        return format_crondelete_input(input)

    def format_CronDeleteOutput(
        self, output: CronDeleteOutput, _: TemplateMessage
    ) -> str:
        """Format → confirmation paragraph verbatim."""
        return format_crondelete_output(output)

    # -------------------------------------------------------------------------
    # Tool Input Title Methods (for Renderer.title_ToolUseMessage dispatch)
    # -------------------------------------------------------------------------

    def _tool_title(
        self, message: TemplateMessage, icon: str, summary: Optional[str] = None
    ) -> str:
        """Format tool title with icon and optional summary."""
        content = cast(ToolUseMessage, message.content)
        escaped_name = escape_html(content.tool_name)
        prefix = f"{icon} " if icon else ""
        if summary:
            escaped_summary = escape_html(summary)
            return f"{prefix}{escaped_name} <span class='tool-summary'>{escaped_summary}</span>"
        return f"{prefix}{escaped_name}"

    def title_TodoWriteInput(
        self, _input: TodoWriteInput, _message: TemplateMessage
    ) -> str:
        """Title → '📝 Todo List'."""
        return "📝 Todo List"

    def title_AskUserQuestionInput(
        self, _input: AskUserQuestionInput, _message: TemplateMessage
    ) -> str:
        """Title → '❓ Asking questions...'."""
        return "❓ Asking questions..."

    def title_TaskInput(self, input: TaskInput, message: TemplateMessage) -> str:
        """Title → '🔧 Task <desc> (subagent_type) [async #<id>]'.

        ``[async]`` muted hint appears when ``run_in_background=True``
        so the reader can tell at a glance which spawns will be
        followed up later by a ``<task-notification>`` user entry
        (issue #90), as opposed to synchronous Task calls whose
        result returns inline. Once the launch confirmation has been
        parsed, the minted ``#<agent_id>`` is appended; when there's
        a later ``TaskOutput`` poll for the same agent, ``#<agent_id>``
        wraps in a forward-link anchor (PR #158 follow-up, mirroring
        the consumer-side backlink from #154).
        """
        content = cast(ToolUseMessage, message.content)
        escaped_name = escape_html(content.tool_name)
        escaped_subagent = (
            escape_html(input.subagent_type) if input.subagent_type else ""
        )
        async_hint = (
            " "
            + self._async_id_suffix(
                input.minted_agent_id,
                input.linked_consumer_message_index,
            )
            if input.run_in_background
            else ""
        )
        if input.description and input.subagent_type:
            escaped_desc = escape_html(input.description)
            return (
                f"🔧 {escaped_name} <span class='tool-summary'>{escaped_desc}</span>"
                f" <span class='tool-subagent'>({escaped_subagent})</span>"
                f"{async_hint}"
            )
        elif input.description:
            return self._tool_title(message, "🔧", input.description) + async_hint
        elif input.subagent_type:
            return (
                f"🔧 {escaped_name} <span class='tool-subagent'>({escaped_subagent})</span>"
                f"{async_hint}"
            )
        return f"🔧 {escaped_name}{async_hint}"

    def _async_id_suffix(
        self,
        minted_id: Optional[str],
        consumer_idx: Optional[int],
    ) -> str:
        """Compose the trailing ``[async ...]`` marker for spawn cards
        of background ``Bash`` / async ``Task`` calls (PR #158).

        Shape:
        - ``run_in_background`` but id not (yet) hoisted onto the
          spawn input → ``[async]``
        - id known → ``[async #<id>]``
        - id known and a later consumer's index known → wraps the
          ``#<id>`` in a forward-link anchor pointing at the first
          ``TaskOutput`` poll (mirrors the backlink direction).

        The leading bracket-tagged hint reuses the existing
        ``.task-async-hint`` styling (muted blue, smaller font); the
        anchor is tagged ``.task-id-forward-link`` (initially same
        dotted-underline as ``.task-id-backlink`` but a distinct class
        so tests can disambiguate the two directions unambiguously
        and styling can diverge later).
        """
        if not minted_id:
            return "<span class='task-async-hint'>[async]</span>"
        id_html = f"<code>#{escape_html(minted_id)}</code>"
        if consumer_idx is not None:
            anchor = f"msg-d-{consumer_idx}"
            id_html = f"<a class='task-id-forward-link' href='#{anchor}'>{id_html}</a>"
        return f"<span class='task-async-hint'>[async {id_html}]</span>"

    def title_EditInput(self, input: EditInput, message: TemplateMessage) -> str:
        """Title → '📝 Edit <file_path>'."""
        return self._tool_title(message, "📝", input.file_path)

    def title_WriteInput(self, input: WriteInput, message: TemplateMessage) -> str:
        """Title → '📝 Write <file_path>'."""
        return self._tool_title(message, "📝", input.file_path)

    def title_ReadInput(self, input: ReadInput, message: TemplateMessage) -> str:
        """Title → '📄 Read <file_path>[, lines N-M]'."""
        summary = input.file_path
        # Add line range info if available. ``offset`` in the Read tool's
        # input is the 1-based starting line number (matches what the
        # ``toolUseResult.file.startLine`` and the cat-n line numbers in
        # the rendered content show). ``None`` or ``0`` both mean "start
        # from line 1". The displayed range is inclusive on both ends, so
        # the end is ``start + limit - 1`` — not ``start + limit``.
        if input.limit is not None:
            start = input.offset if input.offset else 1
            end = start + input.limit - 1
            if input.limit == 1:
                summary = f"{summary}, line {start}"
            else:
                summary = f"{summary}, lines {start}-{end}"
        return self._tool_title(message, "📄", summary)

    def title_GlobInput(self, input: GlobInput, message: TemplateMessage) -> str:
        """Title → '🔍 Glob <pattern>[ in path]'."""
        summary = input.pattern
        if input.path:
            summary = f"{summary} in {input.path}"
        return self._tool_title(message, "🔍", summary)

    def title_GrepInput(self, input: GrepInput, message: TemplateMessage) -> str:
        """Title → '🔎 Grep <pattern>'."""
        return self._tool_title(message, "🔎", input.pattern)

    def title_BashInput(self, input: BashInput, message: TemplateMessage) -> str:
        """Title → '💻 Bash <description> [async #<id>]'.

        Plain shape for foreground Bash. For background spawns, append
        the ``[async]`` muted hint and — once the matching tool_result
        has been parsed — the minted ``#<id>``. When a later
        ``TaskOutput`` poll for the same id is present, the ``#<id>``
        wraps in a forward-link anchor (PR #158 follow-up).

        The async signal is the OR of (a) ``input.run_in_background``
        (caller-set hint) and (b) ``input.minted_background_task_id``
        (propagated from the tool_result by
        ``_link_task_id_consumers``). The harness may background a
        Bash command on its own (e.g. timeout-driven) WITHOUT setting
        the input flag, so gating on the input alone misses real-world
        shapes — the authoritative signal lives on the result side.
        """
        base = self._tool_title(message, "💻", input.description)
        if not (input.run_in_background or input.minted_background_task_id):
            return base
        suffix = self._async_id_suffix(
            input.minted_background_task_id,
            input.linked_consumer_message_index,
        )
        return f"{base} {suffix}"

    def title_WebSearchInput(
        self, input: WebSearchInput, message: TemplateMessage
    ) -> str:
        """Title → '🔎 WebSearch <query>'."""
        return self._tool_title(message, "🔎", input.query)

    def title_WebFetchInput(
        self, input: WebFetchInput, message: TemplateMessage
    ) -> str:
        """Title → '🌐 WebFetch <url>'."""
        return self._tool_title(message, "🌐", input.url)

    def title_MonitorInput(self, input: MonitorInput, message: TemplateMessage) -> str:
        """Title → '🔭 Monitor <description>'."""
        return self._tool_title(message, "🔭", input.description)

    def title_ScheduleWakeupInput(
        self, input: ScheduleWakeupInput, message: TemplateMessage
    ) -> str:
        """Title → '⏰ ScheduleWakeup +<delay>s — <reason>'."""
        summary = f"+{input.delaySeconds}s — {input.reason}"
        return self._tool_title(message, "⏰", summary)

    def title_CronCreateInput(
        self, input: CronCreateInput, message: TemplateMessage
    ) -> str:
        """Title → '⏰ CronCreate <cron>'."""
        return self._tool_title(message, "⏰", input.cron)

    def title_CronListInput(
        self, _input: CronListInput, message: TemplateMessage
    ) -> str:
        """Title → '⏰ CronList'.

        Drop the summary entirely — ``_tool_title`` already emits the
        message's tool name (``CronList``) ahead of any summary span,
        so passing the tool name as the summary too would render it
        twice (caught by monk on #148 review). The other three tools
        in the family pass distinct summaries (delay, cron expression,
        id), so they don't trigger the duplication.
        """
        return self._tool_title(message, "⏰")

    def title_CronDeleteInput(
        self, input: CronDeleteInput, message: TemplateMessage
    ) -> str:
        """Title → '⏰ CronDelete <id>'."""
        return self._tool_title(message, "⏰", input.id)

    def title_SkillInput(self, input: SkillInput, message: TemplateMessage) -> str:
        """Title → '💡 Skill <skill_name>'."""
        return self._tool_title(message, "💡", input.skill)

    def _task_title(
        self,
        message: TemplateMessage,
        action: str,
        subject: str,
        task_id: str,
        linked_creating_call_index: Optional[int] = None,
    ) -> str:
        """Compose the compact ``Task #N <subject> [action]`` tool title.

        Used by both TaskCreate (action="created") and TaskUpdate
        (action="updated"). ``task_id`` is empty when unknown
        (TaskCreate before its tool_result has been observed); the ``#N``
        segment is then dropped. ``subject`` is escaped here, so callers
        pass the raw value. The leading emoji comes from the template.

        ``linked_creating_call_index`` is the message_index of the
        originating ``TaskCreate`` call, set by
        ``_link_task_id_consumers`` for ``TaskUpdate`` titles. When
        present, the ``#N`` segment wraps in an anchor pointing back
        to the create card (#154).
        """
        del message  # Reserved for future per-message hints.
        parts: list[str] = ["Task"]
        if task_id:
            id_html = f"<code>#{escape_html(task_id)}</code>"
            if linked_creating_call_index is not None:
                anchor = f"msg-d-{linked_creating_call_index}"
                id_html = f"<a class='task-id-backlink' href='#{anchor}'>{id_html}</a>"
            parts.append(id_html)
        if subject:
            parts.append(f"<span class='tool-summary'>{escape_html(subject)}</span>")
        parts.append(f"<span class='task-action'>[{action}]</span>")
        return " ".join(parts)

    def title_TaskCreateInput(
        self, input: TaskCreateInput, message: TemplateMessage
    ) -> str:
        """Title → 'Task #N <subject> [created]'.

        ``#N`` resolves via the per-session ``tool_use_id → task_id`` map
        populated from the matching TaskCreate tool_result; absent when
        the result hasn't been loaded.
        """
        content = cast(ToolUseMessage, message.content)
        sid = message.meta.session_id if message.meta else ""
        task_id = self._task_id_by_tool_use.get(sid, {}).get(content.tool_use_id, "")
        return self._task_title(message, "created", input.subject or "", task_id)

    def title_TaskUpdateInput(
        self, input: TaskUpdateInput, message: TemplateMessage
    ) -> str:
        """Title → 'Task #N <subject> [updated]'.

        Subject resolves via the per-session ``task_id → subject`` map
        populated from earlier TaskCreate tool_results (or TaskList
        snapshots). Empty when not found — the title degrades to the
        bare ``#N``.

        ``#N`` wraps in a backlink anchor pointing at the originating
        ``TaskCreate`` card when ``_link_task_id_consumers`` matched
        the ``taskId`` to a create call earlier in the transcript
        (#154).
        """
        sid = message.meta.session_id if message.meta else ""
        subject = self._task_subjects_by_session.get(sid, {}).get(input.taskId, "")
        return self._task_title(
            message,
            "updated",
            subject,
            input.taskId,
            linked_creating_call_index=input.creating_call_message_index,
        )

    def title_SendMessageInput(
        self, input: SendMessageInput, message: TemplateMessage
    ) -> str:
        """Title → '✉️ SendMessage to <recipient_badge>'.

        The leading ✉️ replaces the default 🛠️ tool emoji (the template
        suppresses the default when the title already starts with one).
        Inlining the recipient frees the body to render the message
        content directly as markdown.
        """
        # Re-use the formatter module's badge helper. The underscore is
        # legacy intra-module convention; surfacing the title here is
        # the only cross-module call.
        from .teammate_formatter import _teammate_badge  # pyright: ignore[reportPrivateUsage]

        if input.recipient:
            color = self._colors_for(message).get(input.recipient)
            badge = _teammate_badge(input.recipient, color)
            return f"✉️ SendMessage <span class='tool-summary'>to {badge}</span>"
        return "✉️ SendMessage"

    def title_TaskOutputInput(self, input: TaskOutputInput, _: TemplateMessage) -> str:
        """Title → '🔍 TaskOutput #<task_id>' for the async-agent polling tool.

        ``🔍`` reads as "look up / inspect" — distinct from the
        spawning ``🔧 Task`` so the visual scan separates spawn from
        poll. The leading emoji also short-circuits the template's
        default ``🛠️`` prepend.

        ``#<task_id>`` wraps in a backlink anchor pointing at the
        originating spawn (a ``Bash`` with ``run_in_background`` for
        ``local_bash`` taskType, or a ``Task`` async-agent launch for
        ``local_agent`` taskType) when ``_link_task_id_consumers``
        matched the id (#154).
        """
        if not input.task_id:
            return "🔍 TaskOutput"
        id_html = f"<code>#{escape_html(input.task_id)}</code>"
        if input.creating_call_message_index is not None:
            anchor = f"msg-d-{input.creating_call_message_index}"
            id_html = f"<a class='task-id-backlink' href='#{anchor}'>{id_html}</a>"
        return f"🔍 TaskOutput {id_html}"

    def title_TaskStopInput(self, input: TaskStopInput, _: TemplateMessage) -> str:
        """Title → '🛑 TaskStop #<task_id>' for the background-task
        termination tool (PR #158 follow-up — was rendered as a generic
        tool block before).

        ``🛑`` reads as "halt", visually distinct from the ``🔍``
        TaskOutput poll. The same backlink machinery as
        ``TaskOutputInput`` applies: ``#<task_id>`` wraps in an anchor
        pointing back at the originating spawn when
        ``_link_task_id_consumers`` matched the id.
        """
        if not input.task_id:
            return "🛑 TaskStop"
        id_html = f"<code>#{escape_html(input.task_id)}</code>"
        if input.creating_call_message_index is not None:
            anchor = f"msg-d-{input.creating_call_message_index}"
            id_html = f"<a class='task-id-backlink' href='#{anchor}'>{id_html}</a>"
        return f"🛑 TaskStop {id_html}"

    def title_TaskNotificationMessage(
        self, content: TaskNotificationMessage, _: TemplateMessage
    ) -> str:
        """Title → '🔄 Async result • <summary>' for an async-agent
        completion notification (issue #90). The summary is the most
        useful at-a-glance hint; the rest of the metadata renders in
        the body card.

        Empty at ``DetailLevel.LOW`` for duplicate-flagged
        notifications — pairs with ``format_TaskNotificationMessage``
        to "ghost" the card while keeping the message in
        ``ctx.messages``.
        """
        if self.detail == DetailLevel.LOW and content.result_is_duplicate:
            return ""
        if content.summary:
            return (
                "🔄 Async result "
                f"<span class='tool-summary'>{escape_html(content.summary)}</span>"
            )
        if content.task_id:
            return f"🔄 Async result <code>#{escape_html(content.task_id)}</code>"
        return "🔄 Async result"

    def _flatten_preorder(
        self, roots: list[TemplateMessage]
    ) -> list[Tuple[TemplateMessage, str, str, str]]:
        """Flatten message tree via pre-order traversal, formatting each message.

        Traverses the tree depth-first (pre-order), computes title and formats
        content to HTML, building a flat list of (message, title, html, timestamp) tuples.

        Also tracks and reports timing statistics for Markdown and Pygments operations
        when DEBUG_TIMING is enabled.

        Args:
            roots: Root messages (typically session headers) with children populated

        Returns:
            Flat list of (message, title, html_content, formatted_timestamp) tuples
        """
        flat: list[Tuple[TemplateMessage, str, str, str]] = []

        # Initialize timing tracking for expensive operations
        markdown_timings: list[Tuple[float, str]] = []
        pygments_timings: list[Tuple[float, str]] = []
        set_timing_var("_markdown_timings", markdown_timings)
        set_timing_var("_pygments_timings", pygments_timings)

        # Build index_map so we can clear stranded `pair_first` flags
        # when their partner tool_result gets skipped (see suppression
        # logic in visit()).
        index_map: dict[int, TemplateMessage] = {}

        def index_tree(msg: TemplateMessage) -> None:
            if msg.message_index is not None:
                index_map[msg.message_index] = msg
            for child in msg.children:
                index_tree(child)

        for root in roots:
            index_tree(root)

        def visit(msg: TemplateMessage) -> None:
            # Update current message ID for timing tracking
            set_timing_var("_current_msg_id", msg.message_id)
            title = self.title_content(msg)
            html = self.format_content(msg)
            formatted_ts = format_timestamp(msg.meta.timestamp if msg.meta else None)
            # Skip messages with nothing to show — e.g. TaskCreate /
            # TaskUpdate tool_results whose output formatter returns "".
            # Without this they render as a bare timestamp-only card.
            # Children still render at the same flat-list level since
            # the recursion below is unconditional.
            if title or html or msg.children:
                flat.append((msg, title, html, formatted_ts))
            else:
                # Skipped message: if it's the second half of a pair
                # (msg.pair_first → first message's index), clear the
                # first half's `pair_last` so it loses its `pair_first`
                # CSS class. Otherwise the surviving tool_use renders
                # with a flat bottom border and no margin, expecting a
                # companion that never arrives.
                if msg.pair_first is not None:
                    partner = index_map.get(msg.pair_first)
                    if partner is not None:
                        partner.pair_last = None
                        partner.pair_duration = None
            for child in msg.children:
                visit(child)

        for root in roots:
            visit(root)

        # Report timing statistics for Markdown/Pygments operations
        if DEBUG_TIMING:
            report_timing_statistics(
                [
                    ("Markdown", markdown_timings),
                    ("Pygments", pygments_timings),
                ]
            )

        return flat

    def generate(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
        page_info: Optional[dict[str, Any]] = None,
        page_stats: Optional[dict[str, Any]] = None,
    ) -> str:
        """Generate HTML from transcript messages.

        Args:
            messages: List of transcript entries to render.
            title: Optional title for the output.
            combined_transcript_link: Optional link to combined transcript.
            output_dir: Optional output directory for referenced images.
            page_info: Optional pagination info (page_number, prev_link, next_link).
            page_stats: Optional page statistics (message_count, date_range, token_summary).
            session_tree: Optional pre-built SessionTree (avoids rebuilding DAG).
        """

        from ..git_remote import canonical_cwd_from_messages, render_with_repo_context

        # Bind the per-render canonical repo cwd for the SHA-link
        # plugin (issue #156). The mistune renderers themselves are
        # cached singletons; the resolver reads the cwd from a
        # ContextVar so different transcripts can scope to different
        # repos without cache invalidation.
        repo_cwd = canonical_cwd_from_messages(messages)
        with render_with_repo_context(repo_cwd):
            return self._generate_inner(
                messages,
                title=title,
                combined_transcript_link=combined_transcript_link,
                output_dir=output_dir,
                session_tree=session_tree,
                page_info=page_info,
                page_stats=page_stats,
            )

    def _generate_inner(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
        page_info: Optional[dict[str, Any]] = None,
        page_stats: Optional[dict[str, Any]] = None,
    ) -> str:
        """Body of ``generate`` running inside the SHA-resolver context."""
        import time

        t_start = time.time()

        # Set output directory for image export (used in "referenced" mode)
        self._output_dir = output_dir
        self._image_counter = 0

        if not title:
            title = "Claude Transcript"

        # Get root messages (tree) and session navigation from format-neutral renderer
        root_messages, session_nav, ctx = generate_template_messages(
            messages, session_tree=session_tree, detail=self.detail
        )
        # Snapshot the teammate-color map onto the renderer so per-message
        # format methods can consult it without threading ctx through every
        # dispatch. Reset for subsequent renders on the same instance.
        self._teammate_colors_by_session = {
            sid: dict(colors) for sid, colors in ctx.teammate_colors.items()
        }
        self._task_subjects_by_session = {
            sid: dict(subjects) for sid, subjects in ctx.task_subjects.items()
        }
        self._task_id_by_tool_use = {
            sid: dict(ids) for sid, ids in ctx.task_id_for_tool_use.items()
        }

        # Flatten tree via pre-order traversal, formatting content along the way
        with log_timing("Content formatting (pre-order)", t_start):
            template_messages = self._flatten_preorder(root_messages)

        # Render template
        with log_timing("Template environment setup", t_start):
            env = get_template_environment()
            template = env.get_template("transcript.html")

        with log_timing(
            lambda: f"Template rendering ({len(html_output)} chars)", t_start
        ):
            html_output = str(
                template.render(
                    title=title,
                    messages=template_messages,
                    sessions=session_nav,
                    combined_transcript_link=combined_transcript_link,
                    library_version=get_library_version(),
                    css_class_from_message=css_class_from_message,
                    get_message_emoji=get_message_emoji,
                    is_session_header=is_session_header,
                    page_info=page_info,
                    page_stats=page_stats,
                )
            )

        return html_output

    def generate_session(
        self,
        messages: list[TranscriptEntry],
        session_id: str,
        title: Optional[str] = None,
        cache_manager: Optional["CacheManager"] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
        suppress_combined_link: bool = False,
    ) -> str:
        """Generate HTML for a single session."""
        # Filter messages for this session (SummaryTranscriptEntry.sessionId is always None).
        # Also accept entries whose sessionId was rewritten to
        # ``{session_id}#agent-{agent_id}`` by ``_integrate_agent_entries``;
        # otherwise per-session exports drop the inlined subagent
        # conversation (CodeRabbit on PR #125).
        agent_prefix = f"{session_id}#agent-"
        session_messages = [
            msg
            for msg in messages
            if msg.sessionId == session_id
            or (msg.sessionId or "").startswith(agent_prefix)
        ]

        # Get combined transcript link if cache manager is available.
        # The back-link must point at the combined file of the *same*
        # variant this session is being rendered at — mixing variants
        # would land the user on a different detail/compact rendering.
        # Suppressed under `--combined no` where the combined file is
        # never written.
        combined_link = None
        if cache_manager is not None and not suppress_combined_link:
            try:
                project_cache = cache_manager.get_cached_project_data()
                if project_cache and project_cache.sessions:
                    from ..utils import variant_suffix as _variant_suffix

                    suffix = _variant_suffix(self.detail, self.compact, "html")
                    combined_link = f"combined_transcripts{suffix}.html"
            except Exception:
                pass

        return self.generate(
            session_messages,
            title or f"Session {session_id[:8]}",
            combined_transcript_link=combined_link,
            output_dir=output_dir,
            session_tree=session_tree,
        )

    def generate_projects_index(
        self,
        project_summaries: list[dict[str, Any]],
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        expand_paths_tree: bool = False,
    ) -> str:
        """Generate an HTML projects index page.

        Args:
            project_summaries: Per-project summary dicts.
            from_date / to_date: Date-filter labels for the title.
            expand_paths_tree: When True (Obsidian mode — `--expand-paths`),
                render the project list as a nested folder hierarchy that
                mirrors the projected directory tree, instead of a flat
                grid of cards.
        """
        title = title_for_projects_index(project_summaries, from_date, to_date)
        template_projects, template_summary = prepare_projects_index(project_summaries)

        project_tree: Optional[dict[str, Any]] = None
        if expand_paths_tree:
            project_tree = _build_html_project_tree(template_projects)

        env = get_template_environment()
        template = env.get_template("index.html")
        return str(
            template.render(
                title=title,
                projects=template_projects,
                project_tree=project_tree,
                summary=template_summary,
                library_version=get_library_version(),
            )
        )

    def is_outdated(self, file_path: Path) -> bool:
        """Check if an HTML file is outdated based on version.

        Returns:
            True if the file should be regenerated (missing version,
            different version, or file doesn't exist).
            False if the file is current.
        """
        html_version = check_html_version(file_path)
        current_version = get_library_version()
        # If no version found or different version, it's outdated
        return html_version != current_version


# -- Convenience Functions ----------------------------------------------------


def generate_html(
    messages: list[TranscriptEntry],
    title: Optional[str] = None,
    combined_transcript_link: Optional[str] = None,
    page_info: Optional[dict[str, Any]] = None,
    page_stats: Optional[dict[str, Any]] = None,
    session_tree: Optional["SessionTree"] = None,
) -> str:
    """Generate HTML from transcript messages using Jinja2 templates.

    This is a convenience function that delegates to HtmlRenderer.generate.

    Args:
        messages: List of transcript entries to render.
        title: Optional title for the output.
        combined_transcript_link: Optional link to combined transcript.
        page_info: Optional pagination info (page_number, prev_link, next_link).
        page_stats: Optional page statistics (message_count, date_range, token_summary).
        session_tree: Optional pre-built SessionTree (avoids rebuilding DAG).
    """
    return HtmlRenderer().generate(
        messages,
        title,
        combined_transcript_link,
        page_info=page_info,
        page_stats=page_stats,
        session_tree=session_tree,
    )


def generate_session_html(
    messages: list[TranscriptEntry],
    session_id: str,
    title: Optional[str] = None,
    cache_manager: Optional["CacheManager"] = None,
) -> str:
    """Generate HTML for a single session using Jinja2 templates."""
    return HtmlRenderer().generate_session(messages, session_id, title, cache_manager)


def generate_projects_index_html(
    project_summaries: list[dict[str, Any]],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """Generate an index HTML page listing all projects using Jinja2 templates.

    This is a convenience function that delegates to HtmlRenderer.generate_projects_index.
    """
    return HtmlRenderer().generate_projects_index(project_summaries, from_date, to_date)
