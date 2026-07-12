"""Factory for tool use and tool result content.

This module handles creation of tool-related content into MessageContent subclasses:
- ToolUseMessage: Tool invocations with typed inputs (BashInput, ReadInput, etc.)
- ToolResultMessage: Tool results with output and context

Also provides creation of tool inputs into typed models:
- create_tool_input(): Create typed tool input from raw dict
- create_tool_use_message(): Process ToolUseContent into ToolItemResult
- create_tool_result_message(): Process ToolResultContent into ToolItemResult
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, cast

from pydantic import BaseModel, ValidationError

from .agent_metadata_factory import parse_agent_result_metadata
from ..plugins import apply_transformers
from ..models import (
    # Tool input models
    AskUserQuestionInput,
    AskUserQuestionItem,
    BashInput,
    EditInput,
    ExitPlanModeInput,
    GlobInput,
    GrepInput,
    MessageContent,
    MessageMeta,
    MultiEditInput,
    ReadInput,
    SendMessageInput,
    TaskCreateInput,
    TaskInput,
    TaskListInput,
    TaskOutputInput,
    TaskStopInput,
    WorkflowToolInput,
    TaskUpdateInput,
    TeamCreateInput,
    TeamDeleteInput,
    TodoWriteInput,
    ToolInput,
    ToolResultContent,
    ToolResultMessage,
    ToolUseContent,
    ToolUseMessage,
    ToolUseResult,
    CronCreateInput,
    CronCreateOutput,
    CronDeleteInput,
    CronDeleteOutput,
    CronListInput,
    CronListItem,
    CronListOutput,
    MonitorInput,
    ScheduleWakeupInput,
    ScheduleWakeupOutput,
    SkillInput,
    WebSearchInput,
    WebFetchInput,
    ArtifactInput,
    WriteInput,
    # Tool output models
    AskUserQuestionAnswer,
    AskUserQuestionOutput,
    BashOutput,
    EditOutput,
    ExitPlanModeOutput,
    MonitorOutput,
    ReadOutput,
    SendMessageOutput,
    TaskCreateOutput,
    TaskListItem,
    TaskListOutput,
    TaskOutput,
    TaskOutputResult,
    TaskStopOutput,
    TaskUpdateOutput,
    TeamCreateOutput,
    TeamDeleteOutput,
    ToolOutput,
    WebSearchLink,
    WebSearchOutput,
    WebFetchOutput,
    ArtifactOutput,
    WriteOutput,
)


# =============================================================================
# Tool Input Models Mapping
# =============================================================================

TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "Bash": BashInput,
    "Read": ReadInput,
    "Write": WriteInput,
    "Edit": EditInput,
    "MultiEdit": MultiEditInput,
    "Glob": GlobInput,
    "Grep": GrepInput,
    "Task": TaskInput,
    # Claude Code's experimental teammates feature
    # (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1) emits the spawn tool as
    # ``Agent`` rather than ``Task`` and adds ``name``, ``isolation``,
    # ``team_name`` etc. on top of TaskInput's fields. TaskInput already
    # carries name / team_name / mode and Pydantic's default extra="ignore"
    # accepts the rest cleanly, so aliasing is enough today.
    "Agent": TaskInput,
    "TodoWrite": TodoWriteInput,
    "AskUserQuestion": AskUserQuestionInput,
    "ask_user_question": AskUserQuestionInput,  # Legacy tool name
    "ExitPlanMode": ExitPlanModeInput,
    "WebSearch": WebSearchInput,
    "WebFetch": WebFetchInput,
    # Deploys a self-contained HTML/Markdown file as a claude.ai page
    # (Claude Code 2.1.172+, issue #257).
    "Artifact": ArtifactInput,
    "Monitor": MonitorInput,
    "ScheduleWakeup": ScheduleWakeupInput,
    "CronCreate": CronCreateInput,
    "CronList": CronListInput,
    "CronDelete": CronDeleteInput,
    "Skill": SkillInput,
    # Teammates feature tools
    "TeamCreate": TeamCreateInput,
    "TeamDelete": TeamDeleteInput,
    "TaskCreate": TaskCreateInput,
    "TaskUpdate": TaskUpdateInput,
    "TaskList": TaskListInput,
    "SendMessage": SendMessageInput,
    # Async-agents polling tool (issue #90). Pairs with the
    # ``<task-notification>`` user entry that delivers the actual
    # result.
    "TaskOutput": TaskOutputInput,
    # TaskStop kills a background task. Same ``task_id`` shape /
    # id space as TaskOutput (PR #158 follow-up).
    "TaskStop": TaskStopInput,
    # Dynamic Workflow orchestrator (issue #174): input.script is JS.
    "Workflow": WorkflowToolInput,
}


# =============================================================================
# Tool Input Creation
# =============================================================================


def create_tool_input(
    tool_name: str, input_data: dict[str, Any]
) -> Optional[ToolInput]:
    """Create typed tool input from raw dictionary.

    Uses Pydantic model_validate for strict validation. On failure, returns None
    and the caller should use ToolUseContent as the fallback (which preserves
    all original data for display).

    Args:
        tool_name: The name of the tool (e.g., "Bash", "Read")
        input_data: The raw input dictionary from the tool_use content

    Returns:
        A typed input model if parsing succeeds, None otherwise.
    """
    model_class = TOOL_INPUT_MODELS.get(tool_name)
    if model_class is not None:
        try:
            return cast(ToolInput, model_class.model_validate(input_data))
        except Exception:
            return None
    return None


# =============================================================================
# Tool Output Parsing
# =============================================================================
# Parse raw tool result content into typed output models (ReadOutput, EditOutput, etc.)
# Symmetric with Tool Input parsing above.


def _parse_cat_n_snippet(
    lines: list[str], start_idx: int = 0
) -> Optional[tuple[str, Optional[str], int]]:
    """Parse cat-n formatted snippet from lines.

    Args:
        lines: List of lines to parse
        start_idx: Index to start parsing from (default: 0)

    Returns:
        Tuple of (code_content, system_reminder, line_offset) or None if not parseable
    """
    code_lines: list[str] = []
    system_reminder: Optional[str] = None
    in_system_reminder = False
    line_offset = 1  # Default offset

    for line in lines[start_idx:]:
        # Check for system-reminder start
        if "<system-reminder>" in line:
            in_system_reminder = True
            system_reminder = ""
            continue

        # Check for system-reminder end
        if "</system-reminder>" in line:
            in_system_reminder = False
            continue

        # If in system reminder, accumulate reminder text
        if in_system_reminder:
            if system_reminder is not None:
                system_reminder += line + "\n"
            continue

        # Parse regular code line. Two cat-n separator variants seen in the
        # wild: the arrow form ("  123→content", used by Edit/Write result
        # snippets) and the literal-tab form ("775\tcontent", used by Read
        # results since at least Claude Code 2.1.x — see issue #170).
        match = re.match(r"\s*(\d+)[\t→](.*)$", line)
        if match:
            line_num = int(match.group(1))
            # Capture the first line number as offset
            if not code_lines:
                line_offset = line_num
            code_lines.append(match.group(2))
        elif line.strip() == "":  # Allow empty lines between cat-n lines
            continue
        else:  # Non-matching non-empty line, stop parsing
            break

    if not code_lines:
        return None

    # Join code lines and trim trailing reminder text
    code_content = "\n".join(code_lines)
    if system_reminder:
        system_reminder = system_reminder.strip()

    return (code_content, system_reminder, line_offset)


def _extract_tool_result_text(tool_result: ToolResultContent) -> str:
    """Extract text content from a ToolResultContent.

    Handles both string content and structured content (list of dicts).

    Args:
        tool_result: The tool result to extract text from

    Returns:
        Extracted text content, or empty string if none found
    """
    content = tool_result.content
    if isinstance(content, str):
        return content
    # Structured content - extract text from list of content items
    # Format: [{"type": "text", "text": "..."}, ...]
    text_parts: list[str] = []
    for item in content:
        if item.get("type") == "text":
            text_parts.append(str(item.get("text", "")))
    return "\n".join(text_parts)


def parse_read_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[ReadOutput]:
    """Parse Read tool result into structured content.

    Prefers the structured ``toolUseResult.file`` payload (clean content
    plus accurate ``filePath`` / ``startLine`` / ``numLines`` / ``totalLines``
    metadata) when available; falls back to re-parsing the ``cat -n``
    formatted ``tool_result.content`` text for older transcripts.

    Args:
        tool_result: The tool result content (cat-n formatted text)
        file_path: Path to the file that was read (required for ReadOutput)
        tool_use_result: Structured toolUseResult; ``file`` dict gives us
            clean content + exact line metadata without re-parsing cat-n

    Returns:
        ReadOutput if parsing succeeds, None otherwise
    """
    if not file_path:
        return None

    # Preferred path: structured toolUseResult.file is byte-clean and carries
    # the exact line metadata. Avoids the lossy cat-n round-trip and gives us
    # totalLines (which the text-only fallback cannot recover).
    if isinstance(tool_use_result, dict):
        file_info: dict[str, Any] = tool_use_result.get("file") or {}
        content_field: Any = file_info.get("content")
        if isinstance(content_field, str):
            # ``numLines`` may legitimately be 0 for an empty-file read, so
            # distinguish *absent* from *zero* explicitly — the
            # ``... or default`` truthiness shortcut would silently promote
            # the zero case to the fallback. Same for ``totalLines`` and
            # ``startLine``. ``splitlines()`` over ``split("\n")`` for the
            # absent-numLines fallback so content ending in ``\n`` does not
            # tack on a phantom trailing element ("x\ny\n".splitlines() →
            # ["x", "y"], length 2 not 3).
            num_lines_raw = file_info.get("numLines")
            num_lines = (
                int(num_lines_raw)
                if num_lines_raw is not None
                else len(content_field.splitlines())
            )
            total_lines_raw = file_info.get("totalLines")
            total_lines = (
                int(total_lines_raw) if total_lines_raw is not None else num_lines
            )
            start_line_raw = file_info.get("startLine")
            start_line = int(start_line_raw) if start_line_raw is not None else 1
            return ReadOutput(
                file_path=str(file_info.get("filePath") or file_path),
                content=content_field,
                start_line=start_line,
                num_lines=num_lines,
                total_lines=total_lines,
                is_truncated=num_lines < total_lines,
                system_reminder=None,
            )

    if not (content := _extract_tool_result_text(tool_result)):
        return None

    # Fallback: parse the cat-n formatted text. Accepts both the tab variant
    # (Read tool result, Claude Code 2.1.x+) and the arrow variant
    # (Edit/Write result snippets); see _parse_cat_n_snippet for the
    # combined regex.
    lines = content.split("\n")
    if not lines or not re.match(r"\s*\d+[\t→]", lines[0]):
        return None

    result = _parse_cat_n_snippet(lines)
    if result is None:
        return None

    code_content, system_reminder, line_offset = result
    num_lines = len(code_content.split("\n"))

    return ReadOutput(
        file_path=file_path,
        content=code_content,
        start_line=line_offset,
        num_lines=num_lines,
        total_lines=num_lines,  # Not recoverable from text-only fallback
        is_truncated=False,  # Same — fallback can't tell
        system_reminder=system_reminder,
    )


def parse_edit_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[EditOutput]:
    """Parse Edit tool result into structured content.

    Edit tool results typically have format:
    "The file ... has been updated. Here's the result of running `cat -n` on a snippet..."
    followed by cat-n formatted lines.

    Args:
        tool_result: The tool result content
        file_path: Path to the file that was edited (required for EditOutput)

    Returns:
        EditOutput if parsing succeeds, None otherwise
    """
    if not file_path:
        return None
    if not (content := _extract_tool_result_text(tool_result)):
        return None

    # Look for the cat-n snippet after the preamble
    # Pattern: look for first line that matches the cat-n format
    lines = content.split("\n")
    code_start_idx = None

    for i, line in enumerate(lines):
        if re.match(r"\s+\d+→", line):
            code_start_idx = i
            break

    if code_start_idx is None:
        return None

    result = _parse_cat_n_snippet(lines, code_start_idx)
    if result is None:
        return None

    code_content, _system_reminder, line_offset = result
    # Edit tool doesn't use system_reminder

    return EditOutput(
        file_path=file_path,
        success=True,  # If we got here, edit succeeded
        diffs=[],  # We don't have diff info from result
        message=code_content,
        start_line=line_offset,
    )


def parse_write_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[WriteOutput]:
    """Parse Write tool result into structured content.

    Write tool results contain an acknowledgment on the first line.
    We extract just the first line for display.

    Args:
        tool_result: The tool result content
        file_path: Path to the file that was written (required for WriteOutput)

    Returns:
        WriteOutput if parsing succeeds, None otherwise
    """
    if not file_path:
        return None
    if not (content := _extract_tool_result_text(tool_result)):
        return None

    lines = content.split("\n")
    if not lines[0]:
        return None

    first_line = lines[0]
    return WriteOutput(
        file_path=file_path,
        success=True,  # If we got content, write succeeded
        message=first_line,
    )


def parse_task_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TaskOutput]:
    """Parse Task tool result into structured content.

    Task tool results contain the agent's response as markdown. For
    teammate-spawned or async-agent Tasks the tail carries a metadata
    block (``agentId:`` / ``worktreePath:`` / ``worktreeBranch:`` /
    ``<usage>``) — we extract that into ``AgentResultMetadata`` and
    strip the tail so the rendered body stays clean.

    Args:
        tool_result: The tool result content (agent's response)
        file_path: Unused for Task tool

    Returns:
        TaskOutput with the agent's response
    """
    del file_path  # Unused
    if not (content := _extract_tool_result_text(tool_result)):
        return None
    body, metadata = parse_agent_result_metadata(content)
    return TaskOutput(result=body, metadata=metadata)


def _looks_like_bash_output(content: str) -> bool:
    """Check if content looks like it's from a Bash tool based on common patterns."""
    if not content:
        return False

    # Check for ANSI escape sequences
    if "\x1b[" in content:
        return True

    # Check for common bash/terminal patterns
    bash_indicators = [
        "$ ",  # Shell prompt
        "❯ ",  # Modern shell prompt
        "> ",  # Shell continuation
        "\n+ ",  # Bash -x output
        "bash: ",  # Bash error messages
        "/bin/bash",  # Bash path
        "command not found",  # Common bash error
        "Permission denied",  # Common bash error
        "No such file or directory",  # Common bash error
    ]

    # Check for file path patterns that suggest command output
    if re.search(r"/[a-zA-Z0-9_-]+(/[a-zA-Z0-9_.-]+)*", content):  # Unix-style paths
        return True

    # Check for common command output patterns
    if any(indicator in content for indicator in bash_indicators):
        return True

    return False


def parse_bash_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[BashOutput]:
    """Parse Bash tool result into structured content.

    Detects ANSI escape sequences for terminal formatting. When the
    structured ``toolUseResult`` carries a ``backgroundTaskId`` (set
    by the harness for ``run_in_background=true`` calls), surface it
    so a later ``TaskOutput`` polling card can backlink to this
    spawning Bash call (#154).

    Args:
        tool_result: The tool result content
        file_path: Unused for Bash tool
        tool_use_result: Structured toolUseResult; ``backgroundTaskId``
            is the relevant key.

    Returns:
        BashOutput with content + ANSI flag + optional background task id.
    """
    del file_path  # Unused
    if not (content := _extract_tool_result_text(tool_result)):
        return None
    has_ansi = _looks_like_bash_output(content)
    bg_task_id: Optional[str] = None
    if isinstance(tool_use_result, dict):
        raw = tool_use_result.get("backgroundTaskId")
        if isinstance(raw, str) and raw:
            bg_task_id = raw
    return BashOutput(content=content, has_ansi=has_ansi, background_task_id=bg_task_id)


# The result summary sentence has carried a few wordings across harness
# versions: "User has answered your question:" / "...questions:" / the literal
# "...question(s):", and the current "Your questions have been answered: ..."
# (issue #180). Accept them all.
_ASKUSERQUESTION_SUMMARY_RE = re.compile(
    r"(?:User has answered your question(?:s|\(s\))?"
    r"|Your questions have been answered): "
    r"(.+)\. You can now continue",
    re.DOTALL,
)
_ASKUSERQUESTION_PAIR_RE = re.compile(r'"([^"]+)"="([^"]+)"')


def _parse_askuserquestion_structured(
    data: dict[str, Any],
) -> Optional[AskUserQuestionOutput]:
    """Build the output from the structured ``toolUseResult`` (preferred).

    ``toolUseResult`` carries an ``answers`` map (question text → chosen
    answer) plus the original ``questions`` (with headers/options), which is
    both more robust than scraping the summary sentence and rich enough to
    render the offered options with the chosen one highlighted.
    """
    raw_answers = data.get("answers")
    if not isinstance(raw_answers, dict) or not raw_answers:
        return None
    answers_map = cast(dict[Any, Any], raw_answers)

    # Index the original questions by their text for header/option lookup.
    questions_by_text: dict[str, AskUserQuestionItem] = {}
    raw_questions = data.get("questions")
    if isinstance(raw_questions, list):
        for rq in cast(list[Any], raw_questions):
            try:
                item = AskUserQuestionItem.model_validate(rq)
            except ValidationError:
                continue
            if item.question:
                questions_by_text[item.question] = item

    answers: list[AskUserQuestionAnswer] = []
    for q_text, a_text in answers_map.items():
        if not isinstance(q_text, str) or not isinstance(a_text, str):
            continue
        item = questions_by_text.get(q_text)
        answers.append(
            AskUserQuestionAnswer(
                question=q_text,
                answer=a_text,
                header=item.header if item else None,
                options=list(item.options) if item else [],
                multi_select=item.multiSelect if item else False,
            )
        )

    if not answers:
        return None
    return AskUserQuestionOutput(answers=answers, raw_message="")


def _parse_askuserquestion_text(content: str) -> Optional[AskUserQuestionOutput]:
    """Fallback: scrape the result summary sentence for Q/A pairs."""
    match = _ASKUSERQUESTION_SUMMARY_RE.match(content)
    if not match:
        return None
    pairs = _ASKUSERQUESTION_PAIR_RE.findall(match.group(1))
    if not pairs:
        return None
    answers = [AskUserQuestionAnswer(question=q, answer=a) for q, a in pairs]
    return AskUserQuestionOutput(answers=answers, raw_message=content)


_ASKUSERQUESTION_CLARIFY_Q_RE = re.compile(r'-\s+"(.*)"$')
_ASKUSERQUESTION_CLARIFY_A_RE = re.compile(r"Answer:\s*(.*)$")


def _parse_askuserquestion_clarify(content: str) -> Optional[AskUserQuestionOutput]:
    """Parse the "clarify" rejection result (issue #180).

    When the user types a free-form reply instead of picking an option, the
    tool use is rejected and the result is an ``is_error`` message of the form::

        The user doesn't want to proceed with this tool use. ...
            Questions asked:
        - "<question>"
          Answer: <free-form reply>
        - "<question>"
          (No answer provided)

    We surface it as a normal answered card: each question with the user's
    free-form reply as the chosen answer (empty when none was given). Options
    are recovered later from the paired tool_use input by the renderer.
    """
    if "Questions asked:" not in content:
        return None
    lowered = content.lower()
    if "clarify" not in lowered and "doesn't want to proceed" not in lowered:
        return None

    tail = content.split("Questions asked:", 1)[1]
    answers: list[AskUserQuestionAnswer] = []
    cur_q: Optional[str] = None
    cur_ans: list[str] = []
    no_answer = False

    def flush() -> None:
        nonlocal cur_q, cur_ans, no_answer
        if cur_q is not None:
            answer = "" if no_answer else "\n".join(cur_ans).strip()
            answers.append(AskUserQuestionAnswer(question=cur_q, answer=answer))
        cur_q, cur_ans, no_answer = None, [], False

    for raw in tail.splitlines():
        line = raw.strip()
        if q_match := _ASKUSERQUESTION_CLARIFY_Q_RE.match(line):
            flush()
            cur_q = q_match.group(1)
        elif cur_q is None:
            continue
        elif line == "(No answer provided)":
            no_answer = True
        elif a_match := _ASKUSERQUESTION_CLARIFY_A_RE.match(line):
            cur_ans.append(a_match.group(1))
        elif line:
            cur_ans.append(line)
    flush()

    if not answers:
        return None
    return AskUserQuestionOutput(answers=answers, raw_message=content)


def parse_askuserquestion_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[AskUserQuestionOutput]:
    """Parse AskUserQuestion tool result into structured content.

    Prefers the structured ``toolUseResult`` (answers map + original
    questions); falls back to parsing the summary sentence, which has used
    two wordings across harness versions (issue #180):
    'User has answered your questions: "Q1"="A1", ...' and
    'Your questions have been answered: "Q1"="A1", ...'.

    Args:
        tool_result: The tool result content (used for the text fallback)
        file_path: Unused for AskUserQuestion tool
        tool_use_result: Structured toolUseResult when available

    Returns:
        AskUserQuestionOutput with Q&A pairs, or None.
    """
    del file_path  # Unused
    if isinstance(tool_use_result, dict):
        structured = _parse_askuserquestion_structured(tool_use_result)
        if structured is not None:
            return structured
    if not (content := _extract_tool_result_text(tool_result)):
        return None
    return _parse_askuserquestion_text(content) or _parse_askuserquestion_clarify(
        content
    )


def parse_exitplanmode_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[ExitPlanModeOutput]:
    """Parse ExitPlanMode tool result into structured content.

    Truncates redundant plan echo on success.
    When a plan is approved, the result contains:
    1. A confirmation message
    2. Path to saved plan file
    3. "## Approved Plan:" followed by full plan text (redundant)

    Args:
        tool_result: The tool result content
        file_path: Unused for ExitPlanMode tool

    Returns:
        ExitPlanModeOutput with truncated message
    """
    del file_path  # Unused
    if not (content := _extract_tool_result_text(tool_result)):
        return None
    approved = "User has approved your plan" in content

    if approved:
        # Truncate at "## Approved Plan:"
        marker = "## Approved Plan:"
        marker_pos = content.find(marker)
        if marker_pos > 0:
            message = content[:marker_pos].rstrip()
        else:
            message = content
    else:
        message = content

    return ExitPlanModeOutput(message=message, approved=approved)


def _parse_websearch_from_structured(
    tool_use_result: ToolUseResult,
) -> Optional[WebSearchOutput]:
    """Parse WebSearch from structured toolUseResult data.

    The toolUseResult for WebSearch has the format:
    {
        "query": "search query",
        "results": [
            {"tool_use_id": "...", "content": [{"title": "...", "url": "..."}]},
            "Analysis text..."
        ],
        "durationSeconds": 15.7
    }

    Args:
        tool_use_result: The structured toolUseResult from the entry

    Returns:
        WebSearchOutput if parsing succeeds, None otherwise
    """
    if not isinstance(tool_use_result, dict):
        return None

    query = tool_use_result.get("query")
    if not isinstance(query, str):
        return None

    results_raw = tool_use_result.get("results")
    if not isinstance(results_raw, list):
        return None
    results = cast(list[Any], results_raw)
    if len(results) < 1:
        return None

    # Extract links from the first result element
    links: list[WebSearchLink] = []
    first_result: Any = results[0]
    if isinstance(first_result, dict):
        first_result_dict = cast(dict[str, Any], first_result)
        content_raw = first_result_dict.get("content", [])
        if isinstance(content_raw, list):
            content = cast(list[Any], content_raw)
            for item in content:
                if isinstance(item, dict):
                    link = cast(dict[str, Any], item)
                    title = link.get("title")
                    url = link.get("url")
                    if isinstance(title, str) and isinstance(url, str):
                        links.append(WebSearchLink(title=title, url=url))

    # Extract summary from the second result element (if present)
    summary: Optional[str] = None
    if len(results) > 1 and isinstance(results[1], str):
        summary = results[1].strip() or None

    return WebSearchOutput(query=query, links=links, preamble=None, summary=summary)


def _parse_websearch_from_text(text: str) -> Optional[WebSearchOutput]:
    """Parse WebSearch from plain text content (fallback for agent progress entries).

    Text format:
        Web search results for query: "..."

        Links: [{JSON array of {title, url} objects}]

        Summary text...

    Returns:
        WebSearchOutput if parsing succeeds, None otherwise
    """
    # Extract query
    query_match = re.match(r'Web search results for query:\s*"(.+?)"', text)
    if not query_match:
        return None
    query = query_match.group(1)

    # Extract links JSON array
    links: list[WebSearchLink] = []
    links_match = re.search(r"Links:\s*(\[.*?\])\s*\n", text, re.DOTALL)
    if links_match:
        try:
            raw_links = json.loads(links_match.group(1))
            if isinstance(raw_links, list):
                for item in cast(list[Any], raw_links):
                    if isinstance(item, dict):
                        link = cast(dict[str, Any], item)
                        title = link.get("title")
                        url = link.get("url")
                        if isinstance(title, str) and isinstance(url, str):
                            links.append(WebSearchLink(title=title, url=url))
        except (json.JSONDecodeError, ValueError):
            pass

    # Extract summary (everything after the Links line)
    summary: Optional[str] = None
    if links_match:
        after_links = text[links_match.end() :].strip()
        if after_links:
            summary = after_links

    return WebSearchOutput(query=query, links=links, preamble=None, summary=summary)


def parse_websearch_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[WebSearchOutput]:
    """Parse WebSearch tool result from structured toolUseResult or text content.

    Prefers structured toolUseResult when available. Falls back to parsing
    the text content for agent progress entries that lack toolUseResult.

    Args:
        tool_result: The tool result content
        file_path: Unused for WebSearch tool
        tool_use_result: Structured toolUseResult from the entry

    Returns:
        WebSearchOutput with query, links, and summary, or None if not parseable
    """
    del file_path  # Unused

    if tool_use_result is not None:
        result = _parse_websearch_from_structured(tool_use_result)
        if result is not None:
            return result

    # Fallback: parse from text content (agent progress entries)
    text = _extract_tool_result_text(tool_result)
    if text:
        return _parse_websearch_from_text(text)

    return None


def parse_webfetch_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[WebFetchOutput]:
    """Parse WebFetch tool result from structured toolUseResult.

    WebFetch results include metadata from toolUseResult:
    - bytes: Size of fetched content
    - code: HTTP status code
    - codeText: HTTP status text
    - result: The processed markdown result
    - durationMs: Time taken in milliseconds
    - url: The URL that was fetched

    Args:
        tool_result: The tool result content (used as fallback)
        file_path: Unused for WebFetch tool
        tool_use_result: Structured result containing rich metadata

    Returns:
        WebFetchOutput if parsing succeeds, None otherwise
    """
    del file_path  # Unused

    # Prefer structured toolUseResult when available
    if tool_use_result is not None and isinstance(tool_use_result, dict):
        url = tool_use_result.get("url")
        result = tool_use_result.get("result")

        # Both url and result are required
        if url and result:
            return WebFetchOutput(
                url=str(url),
                result=str(result),
                bytes=tool_use_result.get("bytes"),
                code=tool_use_result.get("code"),
                code_text=tool_use_result.get("codeText"),
                duration_ms=tool_use_result.get("durationMs"),
            )
        # Structured data present but incomplete — don't fall through
        return None

    # Fallback: use text content as result (agent progress entries lack toolUseResult).
    # URL comes from the tool_use input title, not needed here for rendering.
    content = _extract_tool_result_text(tool_result)
    if content:
        return WebFetchOutput(url="", result=content)

    return None


# Text form of a successful Artifact deploy: ``Published <path> at <url>``.
# Fallback for entries that lack the structured toolUseResult.
_ARTIFACT_PUBLISHED_RE = re.compile(
    r"^Published (?P<path>.+) at (?P<url>https?://\S+)$"
)


def parse_artifact_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[ArtifactOutput]:
    """Parse Artifact tool result from structured toolUseResult or text.

    Wire format (observed on Claude Code 2.1.198):

    - toolUseResult: ``{url, path, title}`` — ``title`` is the page's
      ``<title>`` for HTML sources, the file basename for Markdown ones.
    - text content: ``Published <path> at <url>`` on success; a plain
      error message (``is_error: true``, no toolUseResult) on denial.

    Args:
        tool_result: The tool result content (used as fallback)
        file_path: Unused for the Artifact tool
        tool_use_result: Structured result with url/path/title

    Returns:
        ArtifactOutput if parsing succeeds, None otherwise
    """
    del file_path  # Unused

    if isinstance(tool_use_result, dict):
        url = tool_use_result.get("url")
        if isinstance(url, str) and url:
            path = tool_use_result.get("path")
            title = tool_use_result.get("title")
            return ArtifactOutput(
                url=url,
                path=path if isinstance(path, str) else None,
                title=title if isinstance(title, str) else None,
            )

    # Fallback: text content — the success line, or an error/denial message.
    text = _extract_tool_result_text(tool_result)
    if text:
        match = _ARTIFACT_PUBLISHED_RE.match(text.strip())
        if match:
            return ArtifactOutput(url=match.group("url"), path=match.group("path"))
        return ArtifactOutput(raw_text=text)

    return None


# Match ``Monitor started (task <id>, …)`` so the task id is available
# to downstream consumers without re-parsing the full body. The id is
# the short alphanumeric form (e.g. ``b07h5t4ng``) the harness echoes
# back; not load-bearing for rendering today (the body text is shown
# verbatim) but useful for the task-end backlink (#142) and any future
# UI that wants to cross-reference the originating Monitor card.
_MONITOR_TASK_ID_RE = re.compile(r"Monitor started \(task ([A-Za-z0-9]+)")


def parse_monitor_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
) -> Optional[MonitorOutput]:
    """Parse Monitor tool's start-confirmation result.

    The harness emits a single paragraph confirming the monitor was
    armed and naming the task id. Capture the raw text for rendering
    and extract the task id when the format matches; both fields are
    optional from the renderer's perspective.
    """
    del file_path  # Unused — Monitor's result text is self-contained.
    text = _extract_tool_result_text(tool_result).strip()
    if not text:
        return None
    task_match = _MONITOR_TASK_ID_RE.search(text)
    return MonitorOutput(text=text, task_id=task_match.group(1) if task_match else None)


# ``Next wakeup scheduled for HH:MM:SS (in Ns).`` — captures the
# scheduled clock time and the delay-in-seconds for downstream
# consumers; renderer uses the raw text by default.
_SCHEDULE_WAKEUP_RE = re.compile(r"Next wakeup scheduled for ([\d:]+) \(in (\d+)s\)")


def parse_schedulewakeup_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
) -> Optional[ScheduleWakeupOutput]:
    """Parse ScheduleWakeup's start-confirmation paragraph."""
    del file_path
    text = _extract_tool_result_text(tool_result).strip()
    if not text:
        return None
    match = _SCHEDULE_WAKEUP_RE.search(text)
    if match:
        return ScheduleWakeupOutput(
            text=text,
            next_at=match.group(1),
            in_seconds=int(match.group(2)),
        )
    return ScheduleWakeupOutput(text=text)


# ``Scheduled <kind> job <id> (...)`` where kind is "recurring" or
# "one-shot". Real harness output captured during the #148 experiment:
#   "Scheduled recurring job 337e67de (Every 2 minutes). Session-only
#    (not written to disk, dies when Claude exits). ..."
# Char class kept tolerant ([\w-]+) per the lesson from PR #147
# (monk's nit on _MONITOR_TASK_ID_RE).
_CRON_CREATE_ID_RE = re.compile(r"Scheduled (?:recurring|one-shot) job ([\w-]+)")


def parse_croncreate_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
) -> Optional[CronCreateOutput]:
    """Parse CronCreate's start-confirmation."""
    del file_path
    text = _extract_tool_result_text(tool_result).strip()
    if not text:
        return None
    match = _CRON_CREATE_ID_RE.search(text)
    return CronCreateOutput(
        text=text,
        job_id=match.group(1) if match else None,
    )


# Real harness CronList row format (captured during the #148
# experiment):
#   "337e67de — Every 2 minutes (recurring) [session-only]: Cron tick — ..."
# Shape: ``<id> — <description> (<kind>) [<scope>]: <prompt>``
# Where:
#   - id        : short alphanumeric job id ([\w-]+)
#   - description: human-readable schedule ("Every 2 minutes", etc.)
#   - kind      : "recurring" or "one-shot" (in parentheses)
#   - scope     : "session-only" or "durable" (in brackets)
#   - prompt    : remainder of the line, often truncated with "…"
#
# The em-dash separator and parenthesised kind are stable; the
# bracketed scope is optional. Lines that don't match are quietly
# skipped (raw text always preserved as fallback).
_CRON_LIST_LINE_RE = re.compile(
    r"^\s*(?P<id>[\w-]+)\s+—\s+(?P<description>[^()]+?)\s+"
    r"\((?P<kind>recurring|one-shot)\)"
    r"(?:\s+\[(?P<scope>[^\]]+)\])?:\s+(?P<prompt>.+?)\s*$"
)


def parse_cronlist_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
) -> Optional[CronListOutput]:
    """Parse CronList's job-list output.

    Format isn't guaranteed by the harness; we capture the raw text
    and attempt structured parsing per line. The renderer falls back
    to the raw text when ``jobs`` is empty.
    """
    del file_path
    text = _extract_tool_result_text(tool_result).strip()
    if not text:
        return None
    # All-or-nothing: if any non-empty line fails to parse, bail out
    # and let the renderer fall back to raw text. This avoids the
    # partial-table failure mode where a 9-of-10-rows match would
    # silently hide the 10th unparsed line behind a structured table.
    # The renderer's ``if not output.jobs:`` branch already exists for
    # the no-rows-at-all case; the same branch fires here when partial
    # parsing happens. CR review on PR #152 (round 3).
    jobs: list[CronListItem] = []
    for line in text.splitlines():
        if not line.strip():
            continue  # Blank lines are noise, not data — skip without bailing.
        m = _CRON_LIST_LINE_RE.match(line)
        if not m:
            return CronListOutput(text=text, jobs=[])
        kind = m.group("kind")  # "recurring" or "one-shot"
        scope = (m.group("scope") or "").strip()  # "session-only" / "durable"
        jobs.append(
            CronListItem(
                id=m.group("id"),
                description=m.group("description").strip(),
                prompt=m.group("prompt").strip(),
                recurring=True if kind == "recurring" else None,
                durable=True if scope == "durable" else None,
            )
        )
    return CronListOutput(text=text, jobs=jobs)


_CRON_DELETE_ID_RE = re.compile(r"Cancelled job ([\w-]+)")


def parse_crondelete_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
) -> Optional[CronDeleteOutput]:
    """Parse CronDelete's status line.

    Captures the verbatim text plus the cancelled job id when the
    format matches (``Cancelled job <id>.``) — the id feeds the
    cross-link from the rendered status text back to the originating
    ``CronCreate`` card.
    """
    del file_path
    text = _extract_tool_result_text(tool_result).strip()
    if not text:
        return None
    match = _CRON_DELETE_ID_RE.search(text)
    return CronDeleteOutput(
        text=text,
        job_id=match.group(1) if match else None,
    )


# =============================================================================
# Teammates feature tool output parsers
# =============================================================================


def _try_load_json_text(tool_result: ToolResultContent) -> Optional[dict[str, Any]]:
    """Return the JSON object embedded in *tool_result*'s text, or None."""
    text = _extract_tool_result_text(tool_result)
    if not text:
        return None
    try:
        data: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        return cast(dict[str, Any], data)
    return None


_TASK_CREATE_RE = re.compile(
    r"^Task #(?P<id>\d+) created successfully: (?P<subject>.+)$"
)
_TASK_UPDATE_RE = re.compile(r"^Updated task #(?P<id>\d+)(?:\s+(?P<fields>.+))?$")
_TASK_LIST_LINE_RE = re.compile(
    r"^#(?P<id>\d+)\s+\[(?P<status>[^\]]+)\]\s+(?P<rest>.+)$"
)
# Owner trails in parentheses: "... subject (owner)". Owner never contains
# parens, so this match is unambiguous on the trailing segment.
_TASK_LIST_OWNER_RE = re.compile(r"^(?P<subject>.+?)\s+\((?P<owner>[^)]+)\)$")
_TEAM_DELETE_ACTIVE_RE = re.compile(
    # Trailing delimiter: a period (the usual Anthropic form) OR end of
    # string (defensive — drops nothing if the period is ever omitted).
    r"active member\(s\):\s*(?P<members>[^.]+?)(?:\.|$)",
    re.IGNORECASE,
)


def parse_teamcreate_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TeamCreateOutput]:
    """Parse TeamCreate JSON output.

    Typical shape:
        {"team_name": ..., "team_file_path": ..., "lead_agent_id": ...}
    """
    del file_path
    data = _try_load_json_text(tool_result)
    if data is None:
        return None
    team_name = data.get("team_name")
    if not isinstance(team_name, str):
        return None
    return TeamCreateOutput(
        team_name=team_name,
        team_file_path=_opt_str(data.get("team_file_path")),
        lead_agent_id=_opt_str(data.get("lead_agent_id")),
    )


def parse_teamdelete_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TeamDeleteOutput]:
    """Parse TeamDelete JSON output, extracting active members when present.

    Rejects payloads where ``success`` isn't a real bool or ``message`` isn't
    a real string so a stringified ``"false"`` can't silently render as
    success (coderabbit #117).
    """
    del file_path
    data = _try_load_json_text(tool_result)
    if data is None:
        return None
    success = data.get("success")
    message = data.get("message")
    if not isinstance(success, bool) or not isinstance(message, str):
        return None
    active_members: Optional[list[str]] = None
    active_match = _TEAM_DELETE_ACTIVE_RE.search(message)
    if active_match:
        members_raw = active_match.group("members")
        active_members = [m.strip() for m in members_raw.split(",") if m.strip()]
    return TeamDeleteOutput(
        success=success,
        message=message,
        team_name=_opt_str(data.get("team_name")),
        active_members=active_members,
    )


def parse_taskcreate_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TaskCreateOutput]:
    """Parse TaskCreate text output ``"Task #N created successfully: <subject>"``."""
    del file_path
    text = _extract_tool_result_text(tool_result)
    if not text:
        return None
    match = _TASK_CREATE_RE.match(text.strip())
    if match is None:
        return None
    return TaskCreateOutput(
        task_id=match.group("id"),
        subject=match.group("subject").strip(),
        raw_text=text,
    )


def parse_taskupdate_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TaskUpdateOutput]:
    """Parse TaskUpdate text output ``"Updated task #N <comma-list-of-fields>"``.

    The plain-text form doesn't report the old/new status explicitly;
    ``status_change`` remains None unless a richer structured form appears.
    """
    del file_path
    text = _extract_tool_result_text(tool_result)
    if not text:
        return None
    match = _TASK_UPDATE_RE.match(text.strip())
    if match is None:
        return None
    fields_raw = match.group("fields") or ""
    # Drop trailing punctuation so a stray period or semicolon at end of
    # the sentence doesn't leak into the last field key, e.g.
    # "Updated task #1 owner, status." → {"owner": True, "status": True}
    # (coderabbit #117).
    fields_raw = fields_raw.rstrip(" .;:")
    updated_fields: Optional[dict[str, Any]] = None
    if fields_raw:
        updated_fields = {
            name.strip(): True for name in fields_raw.split(",") if name.strip()
        }
    return TaskUpdateOutput(
        success=True,
        task_id=match.group("id"),
        updated_fields=updated_fields,
        status_change=None,
        raw_text=text,
    )


def parse_tasklist_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TaskListOutput]:
    """Parse TaskList text output: one ``#N [status] subject (owner)`` per line."""
    del file_path
    text = _extract_tool_result_text(tool_result)
    if not text:
        return None

    tasks: list[TaskListItem] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _TASK_LIST_LINE_RE.match(line)
        if match is None:
            # Unrecognized line — bail so the generic renderer keeps the full
            # text (defensive: avoid silently dropping data we can't explain).
            return None
        rest = match.group("rest").strip()
        owner_match = _TASK_LIST_OWNER_RE.match(rest)
        if owner_match is not None:
            subject = owner_match.group("subject").strip()
            owner: Optional[str] = owner_match.group("owner").strip()
        else:
            subject = rest
            owner = None
        tasks.append(
            TaskListItem(
                id=match.group("id"),
                subject=subject,
                status=match.group("status").strip(),
                owner=owner,
            )
        )
    if not tasks:
        return None
    return TaskListOutput(tasks=tasks, raw_text=text)


def parse_sendmessage_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[SendMessageOutput]:
    """Parse SendMessage JSON output.

    Shape:
        {"success": bool, "message": str, "request_id": str, "target": str}

    Rejects payloads where ``success`` isn't a real bool or ``message`` isn't
    a real string so a stringified ``"false"`` can't silently render as
    success (coderabbit #117).
    """
    del file_path
    data = _try_load_json_text(tool_result)
    if data is None:
        return None
    success = data.get("success")
    message = data.get("message")
    if not isinstance(success, bool) or not isinstance(message, str):
        return None
    return SendMessageOutput(
        success=success,
        message=message,
        request_id=_opt_str(data.get("request_id")),
        target=_opt_str(data.get("target")),
    )


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


# Tags we care about in TaskOutput's XML-shaped text.
_TASKOUTPUT_TAGS: tuple[str, ...] = (
    "retrieval_status",
    "task_id",
    "task_type",
    "status",
)
_TASKOUTPUT_TAG_RE = re.compile(
    r"<(?P<tag>" + "|".join(_TASKOUTPUT_TAGS) + r")>(?P<body>.*?)</(?P=tag)>",
    re.DOTALL,
)
_TASKOUTPUT_OUTPUT_RE = re.compile(
    r"<output>\s*(?P<body>.*?)\s*</output>",
    re.DOTALL,
)
_TASKOUTPUT_TRUNCATED_RE = re.compile(
    r"\[Truncated\.\s*Full output:\s*(?P<path>[^\]]+)\]",
    re.IGNORECASE,
)


def parse_taskoutput_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[TaskOutputResult]:
    """Parse the TaskOutput async-agent polling tool result.

    The payload is a sequence of single-tag XML-style fields plus an
    ``<output>`` block. We capture the metadata and discard the
    ``<output>`` body entirely — Claude Code already exposes the
    agent's full transcript inline as a sidechain in our HTML, and the
    completion result lands in the trunk via the
    ``<task-notification>`` user message. We only note whether
    ``<output>`` was truncated and the linked ``output_file`` path,
    when given.
    """
    del file_path
    text = _extract_tool_result_text(tool_result)
    if not text:
        return None

    fields: dict[str, str] = {}
    for match in _TASKOUTPUT_TAG_RE.finditer(text):
        fields[match.group("tag")] = match.group("body").strip()

    if not fields:
        return None  # not a TaskOutput-shaped payload

    truncated = False
    output_file: Optional[str] = None
    if (output_block := _TASKOUTPUT_OUTPUT_RE.search(text)) is not None:
        body = output_block.group("body")
        if (trunc := _TASKOUTPUT_TRUNCATED_RE.search(body)) is not None:
            truncated = True
            output_file = trunc.group("path").strip()

    return TaskOutputResult(
        retrieval_status=fields.get("retrieval_status", ""),
        task_id=fields.get("task_id", ""),
        task_type=fields.get("task_type", ""),
        status=fields.get("status", ""),
        output_truncated=truncated,
        output_file=output_file,
        raw_text=text,
    )


def parse_todowrite_output(
    tool_result: ToolResultContent, file_path: Optional[str]
) -> Optional[ToolResultContent]:
    """Collapse a successful Codex ``update_plan`` transport result.

    Codex persists the result of the canonicalized TodoWrite call as a short
    ``Script completed`` status item followed by an ``input_text`` item whose
    text is the empty JSON object returned by ``update_plan``. Rendering that
    structured transport array exposes implementation noise below an otherwise
    useful Todo card. Recognize only this narrow success shape; Claude results,
    errors, and future unfamiliar Codex payloads retain the generic fallback.
    """
    del file_path
    if tool_result.is_error or isinstance(tool_result.content, str):
        return None

    texts: list[str] = []
    for item in tool_result.content:
        if item.get("type") not in {"input_text", "output_text", "text"}:
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        texts.append(text)

    has_completed_status = any(text.startswith("Script completed") for text in texts)
    has_empty_result = False
    for text in texts:
        try:
            decoded: Any = json.loads(text)
        except json.JSONDecodeError:
            continue
        if decoded == {}:
            has_empty_result = True
            break
    if not (has_completed_status and has_empty_result):
        return None

    return ToolResultContent(
        type="tool_result",
        tool_use_id=tool_result.tool_use_id,
        content="Todo list updated.",
        is_error=False,
        agentId=tool_result.agentId,
    )


_TASKSTOP_SUCCESS_RE = re.compile(r"successfully stopped task", re.IGNORECASE)


def parse_taskstop_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,
) -> Optional[TaskStopOutput]:
    """Parse the TaskStop tool result (PR #158 follow-up).

    Two real-world shapes:

    - Success: ``toolUseResult = {"message": "Successfully stopped
      task: <id> (<echoed command>); ..."}`` — structured dict.
    - Error: ``toolUseResult = "Error: No task found with ID: <id>"``
      — plain string. Also the common ``is_error: true`` case.

    Falls back to text-from-content when ``toolUseResult`` is absent
    so older transcripts still produce a typed output.
    """
    del file_path
    # Prefer structured ``toolUseResult`` when available.
    if isinstance(tool_use_result, dict):
        message = str(tool_use_result.get("message", "")).strip()
        stopped = bool(message) and bool(_TASKSTOP_SUCCESS_RE.search(message))
        return TaskStopOutput(stopped=stopped, message=message)
    if isinstance(tool_use_result, str) and tool_use_result.strip():
        message = tool_use_result.strip()
        stopped = bool(_TASKSTOP_SUCCESS_RE.search(message))
        return TaskStopOutput(stopped=stopped, message=message)
    # Fallback: parse from the tool_result text directly.
    text = _extract_tool_result_text(tool_result)
    if not text:
        return TaskStopOutput(stopped=False, message="")
    stopped = bool(_TASKSTOP_SUCCESS_RE.search(text)) and not tool_result.is_error
    return TaskStopOutput(stopped=stopped, message=text.strip())


# Type alias for tool output parsers
# Standard signature: (tool_result, file_path) -> Optional[ToolOutput]
# Extended signature: (tool_result, file_path, tool_use_result) -> Optional[ToolOutput]
ToolOutputParser = Callable[..., Optional[ToolOutput]]

# Registry of tool output parsers: tool_name -> parser function
# Parsers receive the full ToolResultContent and can use _extract_tool_result_text() for text.
# Some parsers (like WebSearch, WebFetch) also accept optional tool_use_result for structured data.
TOOL_OUTPUT_PARSERS: dict[str, ToolOutputParser] = {
    "Read": parse_read_output,
    "Edit": parse_edit_output,
    "Write": parse_write_output,
    "Bash": parse_bash_output,
    "TodoWrite": parse_todowrite_output,
    "Task": parse_task_output,
    "Agent": parse_task_output,  # Teammates spawn tool — same parse shape
    "AskUserQuestion": parse_askuserquestion_output,
    "ExitPlanMode": parse_exitplanmode_output,
    "WebSearch": parse_websearch_output,
    "WebFetch": parse_webfetch_output,
    "Artifact": parse_artifact_output,
    "Monitor": parse_monitor_output,
    "ScheduleWakeup": parse_schedulewakeup_output,
    "CronCreate": parse_croncreate_output,
    "CronList": parse_cronlist_output,
    "CronDelete": parse_crondelete_output,
    # Teammates feature tools
    "TeamCreate": parse_teamcreate_output,
    "TeamDelete": parse_teamdelete_output,
    "TaskCreate": parse_taskcreate_output,
    "TaskUpdate": parse_taskupdate_output,
    "TaskList": parse_tasklist_output,
    "SendMessage": parse_sendmessage_output,
    # Async-agents (issue #90)
    "TaskOutput": parse_taskoutput_output,
    # PR #158 follow-up — typed renderer for TaskStop (was generic).
    "TaskStop": parse_taskstop_output,
}

# Parsers that accept the extended signature with tool_use_result
PARSERS_WITH_TOOL_USE_RESULT: set[str] = {
    "WebSearch",
    "WebFetch",
    "Artifact",
    "Bash",
    "TaskStop",
    "Read",
    "AskUserQuestion",
}


def create_tool_output(
    tool_name: str,
    tool_result: ToolResultContent,
    file_path: Optional[str] = None,
    tool_use_result: Optional[ToolUseResult] = None,
) -> ToolOutput:
    """Create typed tool output from raw ToolResultContent.

    Parses the raw content into specialized output types when possible,
    using the TOOL_OUTPUT_PARSERS registry. Each parser receives the full
    ToolResultContent and can use _extract_tool_result_text() if it needs text.

    For tools in PARSERS_WITH_TOOL_USE_RESULT, the structured toolUseResult
    from the transcript entry is also passed to the parser.

    Args:
        tool_name: The name of the tool (e.g., "Bash", "Read")
        tool_result: The raw tool result content
        file_path: Optional file path for file-based tools (Read, Edit, Write)
        tool_use_result: Optional structured toolUseResult from entry (for WebSearch, WebFetch)

    Returns:
        A typed output model if parsing succeeds, ToolResultContent as fallback.
    """
    # Look up parser in registry
    parser = TOOL_OUTPUT_PARSERS.get(tool_name)
    if parser:
        # Use extended signature for parsers that support tool_use_result
        if tool_name in PARSERS_WITH_TOOL_USE_RESULT:
            parsed = parser(tool_result, file_path, tool_use_result)
        else:
            parsed = parser(tool_result, file_path)
        if parsed:
            return parsed

    # Fallback to raw ToolResultContent
    return tool_result


# =============================================================================
# Tool Item Processing
# =============================================================================


@dataclass
class ToolItemResult:
    """Result of processing a single tool/thinking/image item.

    Note: Titles are computed at render time by Renderer.title_content() dispatch.
    """

    message_type: str
    content: Optional[MessageContent] = None  # Structured content for rendering
    tool_use_id: Optional[str] = None
    is_error: bool = False  # For tool_result error state


def create_tool_use_message(
    meta: MessageMeta,
    tool_use: ToolUseContent,
    tool_use_context: dict[str, ToolUseContent],
) -> ToolItemResult:
    """Create ToolItemResult from a tool_use content item.

    Args:
        meta: Message metadata
        tool_use: The tool use content item
        tool_use_context: Dict to populate with tool_use_id -> ToolUseContent mapping

    Returns:
        ToolItemResult with tool_use content model
    """
    # Parse tool input into typed model (BashInput, ReadInput, etc.)
    parsed = create_tool_input(tool_use.name, tool_use.input)

    # Populate tool_use_context for later use when processing tool results
    tool_use_context[tool_use.id] = tool_use

    # Create ToolUseMessage wrapper with parsed input for specialized formatting
    # Use ToolUseContent as fallback when no specialized parser exists
    tool_use_message: MessageContent = ToolUseMessage(
        meta,
        input=parsed if parsed is not None else tool_use,
        tool_use_id=tool_use.id,
        tool_name=tool_use.name,
    )

    # Plugin transformer pass: lets a plugin rewrite the ToolUseMessage
    # into a specialized subclass (e.g. ClmailCommunicateInputMessage)
    # carrying its own format/title methods. No-op when no plugin matches.
    tool_use_message = apply_transformers(tool_use_message, meta)

    return ToolItemResult(
        message_type="tool_use",
        content=tool_use_message,
        tool_use_id=tool_use.id,
    )


def create_tool_result_message(
    meta: MessageMeta,
    tool_result: ToolResultContent,
    tool_use_context: dict[str, ToolUseContent],
    tool_use_result: Optional[ToolUseResult] = None,
) -> ToolItemResult:
    """Create ToolItemResult from a tool_result content item.

    Args:
        meta: Message metadata
        tool_result: The tool result content item
        tool_use_context: Dict with tool_use_id -> ToolUseContent mapping
        tool_use_result: Optional structured toolUseResult from transcript entry

    Returns:
        ToolItemResult with tool_result content model
    """
    # Get file_path and tool_name from tool_use context for specialized rendering
    result_file_path: Optional[str] = None
    result_tool_name: Optional[str] = None
    if tool_result.tool_use_id in tool_use_context:
        tool_use_from_ctx = tool_use_context[tool_result.tool_use_id]
        result_tool_name = tool_use_from_ctx.name
        if (
            result_tool_name in ("Read", "Edit", "Write")
            and "file_path" in tool_use_from_ctx.input
        ):
            result_file_path = tool_use_from_ctx.input["file_path"]

    # Parse into typed output (ReadOutput, EditOutput, etc.) when possible
    parsed_output = create_tool_output(
        result_tool_name or "",
        tool_result,
        result_file_path,
        tool_use_result,
    )

    # The Artifact result line shows the favicon and version label, but the
    # wire result carries only {url, path, title} — carry both over from the
    # paired tool_use input (issue #262). Missing pairing degrades to the
    # title/url fallbacks in the formatters.
    if isinstance(parsed_output, ArtifactOutput):
        artifact_use = tool_use_context.get(tool_result.tool_use_id)
        if artifact_use is not None:
            favicon = artifact_use.input.get("favicon")
            label = artifact_use.input.get("label")
            parsed_output.favicon = favicon if isinstance(favicon, str) else None
            parsed_output.label = label if isinstance(label, str) else None

    # An AskUserQuestion "clarify" rejection arrives as an ``is_error`` result,
    # but we re-present it as a normal answered card (questions + the user's
    # free-form reply), so drop the error framing (#180).
    is_error = tool_result.is_error or False
    if is_error and isinstance(parsed_output, AskUserQuestionOutput):
        is_error = False

    # Create content model with rendering context
    content_model: MessageContent = ToolResultMessage(
        meta,
        tool_use_id=tool_result.tool_use_id,
        output=parsed_output,
        is_error=is_error,
        tool_name=result_tool_name,
        file_path=result_file_path,
    )

    # Plugin transformer pass: lets a plugin rewrite the ToolResultMessage
    # into a specialized subclass with its own format/title methods.
    content_model = apply_transformers(content_model, meta)

    return ToolItemResult(
        message_type="tool_result",
        content=content_model,
        tool_use_id=tool_result.tool_use_id,
        is_error=is_error,
    )
