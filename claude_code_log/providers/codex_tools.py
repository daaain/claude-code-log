"""Canonicalize Codex tool calls for the shared renderer pipeline.

Codex persists many calls inside a ``custom_tool_call`` named ``exec`` whose
input is a small JavaScript orchestration program.  This module unwraps only
the safe, common case: exactly one static ``tools.<name>({...})`` invocation
with a JSON-compatible object literal.  Dynamic or multi-call programs remain
visible as ``Workflow`` tools, and anything unknown retains its original name
and input for the generic renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Optional, cast


@dataclass(frozen=True)
class AdaptedToolCall:
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _StaticCall:
    name: str
    argument: str
    start: int
    end: int


@dataclass(frozen=True)
class _Emission:
    expression: str
    start: int
    end: int


_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_FERNET_TOKEN = re.compile(r"\AgAAAAA[A-Za-z0-9_-]{80,}={0,2}\Z")
_REDACTED_PAYLOAD = "[opaque payload redacted]"


def adapt_codex_tool_call(
    name: str,
    input_data: dict[str, Any],
    *,
    raw_input: Any = None,
) -> AdaptedToolCall:
    """Map a raw Codex tool call to a shared canonical tool when safe."""
    if name == "exec" and isinstance(raw_input, str):
        calls = _find_static_tool_calls(raw_input)
        if len(calls) != 1:
            return _workflow(raw_input)
        if not _is_simple_result_forwarder(raw_input, calls[0]):
            return _workflow(raw_input)
        decoded = _decode_object_literal(calls[0].argument)
        if decoded is None:
            return _workflow(raw_input)
        return _canonicalize(calls[0].name, decoded)
    return _canonicalize(name, input_data)


def _is_simple_result_forwarder(source: str, call: _StaticCall) -> bool:
    """Reject compound exec programs even when they contain one tools.* call."""
    code = _code_projection(source)
    if re.search(r"\bALL_TOOLS\b", code):
        return False
    assignment = re.search(
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"await\s+tools\." + re.escape(call.name) + r"\s*\(",
        code,
    )
    if assignment is None or assignment.end() - 1 != call.start:
        return False
    assignment_end = _statement_end(code, call.end + 1)
    if assignment_end is None:
        return False
    emissions = _find_output_emissions(source)
    if len(emissions) != 1:
        return False
    result_name = assignment.group(1)
    expression_code = _code_projection(emissions[0].expression).strip()
    if expression_code not in {result_name, f"{result_name}.output"}:
        return False
    remainder = list(code)
    for start, end in (
        (assignment.start(), assignment_end),
        (emissions[0].start, emissions[0].end),
    ):
        remainder[start:end] = " " * (end - start)
    return not "".join(remainder).strip()


def _workflow(source: str) -> AdaptedToolCall:
    return AdaptedToolCall("Workflow", {"script": _scrub_opaque_literals(source)})


def _canonicalize(name: str, input_data: dict[str, Any]) -> AdaptedToolCall:
    if name == "exec_command":
        command = input_data.get("cmd")
        if isinstance(command, str):
            adapted: dict[str, Any] = {"command": command}
            justification = input_data.get("justification")
            if isinstance(justification, str):
                adapted["description"] = justification
            return AdaptedToolCall("Bash", adapted)

    if name == "spawn_agent":
        safe_input = _scrub_opaque_field(input_data, "message")
        prompt = safe_input.get("message")
        task_name = safe_input.get("task_name")
        if isinstance(prompt, str) and isinstance(task_name, str):
            return AdaptedToolCall(
                "Task",
                {
                    "prompt": "" if prompt == _REDACTED_PAYLOAD else prompt,
                    "subagent_type": "codex",
                    "description": task_name,
                    "name": task_name,
                },
            )
        return AdaptedToolCall(name, safe_input)

    if name in {"send_message", "followup_task"}:
        safe_input = _scrub_opaque_field(input_data, "message")
        target = safe_input.get("target")
        message = safe_input.get("message")
        if isinstance(target, str) and isinstance(message, str):
            return AdaptedToolCall(
                "SendMessage",
                {
                    "type": "followup" if name == "followup_task" else "message",
                    "recipient": target,
                    "content": "" if message == _REDACTED_PAYLOAD else message,
                },
            )
        return AdaptedToolCall(name, safe_input)

    if name == "update_plan":
        plan = input_data.get("plan")
        if isinstance(plan, list):
            todos: list[dict[str, Any]] = []
            for raw_item in cast(list[Any], plan):
                if not isinstance(raw_item, dict):
                    return AdaptedToolCall(name, input_data)
                item = cast(dict[str, Any], raw_item)
                step = item.get("step")
                status = item.get("status", "pending")
                if not isinstance(step, str) or not isinstance(status, str):
                    return AdaptedToolCall(name, input_data)
                todos.append({"content": step, "activeForm": step, "status": status})
            return AdaptedToolCall("TodoWrite", {"todos": todos})

    if name == "list_agents":
        return AdaptedToolCall("TaskList", input_data)

    if name == "web__run":
        queries = input_data.get("search_query")
        other_actions = set(input_data) - {"search_query", "response_length"}
        if isinstance(queries, list) and not other_actions:
            query_items = cast(list[Any], queries)
            text_queries: list[str] = []
            for raw_query in query_items:
                if not isinstance(raw_query, dict):
                    break
                query = cast(dict[str, Any], raw_query).get("q")
                if not isinstance(query, str):
                    break
                text_queries.append(query)
            if text_queries and len(text_queries) == len(query_items):
                return AdaptedToolCall("WebSearch", {"query": " • ".join(text_queries)})

    return AdaptedToolCall(name, input_data)


def _scrub_opaque_field(input_data: dict[str, Any], field: str) -> dict[str, Any]:
    value = input_data.get(field)
    if not isinstance(value, str) or _FERNET_TOKEN.fullmatch(value) is None:
        return input_data
    scrubbed = dict(input_data)
    scrubbed[field] = _REDACTED_PAYLOAD
    return scrubbed


def _find_static_tool_calls(source: str) -> list[_StaticCall]:
    calls: list[_StaticCall] = []
    index = 0
    while index < len(source):
        skipped = _skip_literal_or_comment(source, index)
        if skipped is not None:
            index = skipped
            continue
        if not source.startswith("tools.", index):
            index += 1
            continue
        name_match = _IDENTIFIER.match(source, index + len("tools."))
        if name_match is None:
            index += 1
            continue
        cursor = _skip_space(source, name_match.end())
        if cursor >= len(source) or source[cursor] != "(":
            index += 1
            continue
        end = _matching_delimiter(source, cursor, "(", ")")
        if end is None:
            return []
        calls.append(
            _StaticCall(
                name=name_match.group(0),
                argument=source[cursor + 1 : end],
                start=cursor,
                end=end,
            )
        )
        index = end + 1
    return calls


def _decode_object_literal(argument: str) -> Optional[dict[str, Any]]:
    value = argument.strip()
    if not value.startswith("{") or not value.endswith("}"):
        return None
    # Codex-generated wrappers use JSON values with JavaScript identifier keys.
    # Rewrite only code positions; quoted commands and comments are never
    # interpreted as object syntax.
    json_like = _json_compatible_object(value)
    try:
        decoded: Any = json.loads(json_like)
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else None


def _json_compatible_object(source: str) -> str:
    output: list[str] = []
    index = 0
    expect_key = False
    while index < len(source):
        skipped = _skip_literal_or_comment(source, index)
        if skipped is not None:
            output.append(source[index:skipped])
            index = skipped
            continue
        char = source[index]
        if char in "{,":
            next_index = _skip_space(source, index + 1)
            if char == "," and next_index < len(source) and source[next_index] in "}]":
                index += 1
                continue
            expect_key = True
            output.append(char)
            index += 1
            continue
        if expect_key and char.isspace():
            output.append(char)
            index += 1
            continue
        if expect_key:
            match = _IDENTIFIER.match(source, index)
            if match is not None:
                colon = _skip_space(source, match.end())
                if colon < len(source) and source[colon] == ":":
                    output.append(json.dumps(match.group(0)))
                    index = match.end()
                    expect_key = False
                    continue
            expect_key = False
        output.append(char)
        index += 1
    return "".join(output)


def _find_output_emissions(source: str) -> list[_Emission]:
    emissions: list[_Emission] = []
    index = 0
    while index < len(source):
        skipped = _skip_literal_or_comment(source, index)
        if skipped is not None:
            index = skipped
            continue
        name = _IDENTIFIER.match(source, index)
        if name is None or name.group(0) not in {"text", "image", "generatedImage"}:
            index += 1
            continue
        cursor = _skip_space(source, name.end())
        if cursor >= len(source) or source[cursor] != "(":
            index = name.end()
            continue
        closing = _matching_delimiter(source, cursor, "(", ")")
        if closing is None:
            return []
        end = _statement_end(_code_projection(source), closing + 1)
        if end is None:
            return []
        emissions.append(
            _Emission(expression=source[cursor + 1 : closing], start=index, end=end)
        )
        index = end
    return emissions


def _statement_end(code: str, index: int) -> Optional[int]:
    cursor = _skip_space(code, index)
    return cursor + 1 if cursor < len(code) and code[cursor] == ";" else None


def _code_projection(source: str) -> str:
    """Blank literals/comments while retaining code offsets and delimiters."""
    projected = list(source)
    index = 0
    while index < len(source):
        skipped = _skip_literal_or_comment(source, index)
        if skipped is None:
            index += 1
            continue
        for offset in range(index, skipped):
            if projected[offset] not in "\r\n":
                projected[offset] = " "
        index = skipped
    return "".join(projected)


def _scrub_opaque_literals(source: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char not in {'"', "'", "`"}:
            output.append(char)
            index += 1
            continue
        end = _skip_string(source, index, char)
        content_end = (
            end - 1 if end <= len(source) and source[end - 1 : end] == char else end
        )
        content = source[index + 1 : content_end]
        output.append(char)
        output.append(
            _REDACTED_PAYLOAD if _FERNET_TOKEN.fullmatch(content) else content
        )
        if content_end < end:
            output.append(char)
        index = end
    return "".join(output)


def _skip_space(source: str, index: int) -> int:
    while index < len(source) and source[index].isspace():
        index += 1
    return index


def _skip_literal_or_comment(source: str, index: int) -> Optional[int]:
    char = source[index]
    if char in {'"', "'", "`"}:
        return _skip_string(source, index, char)
    if source.startswith("//", index):
        newline = source.find("\n", index + 2)
        return len(source) if newline < 0 else newline + 1
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        return len(source) if end < 0 else end + 2
    return None


def _skip_string(source: str, index: int, quote: str) -> int:
    index += 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
        elif source[index] == quote:
            return index + 1
        else:
            index += 1
    return len(source)


def _matching_delimiter(
    source: str, start: int, opening: str, closing: str
) -> Optional[int]:
    depth = 1
    index = start + 1
    while index < len(source):
        skipped = _skip_literal_or_comment(source, index)
        if skipped is not None:
            index = skipped
            continue
        if source[index] == opening:
            depth += 1
        elif source[index] == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None
