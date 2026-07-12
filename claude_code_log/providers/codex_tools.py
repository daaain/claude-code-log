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


_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_OBJECT_KEY = re.compile(r'([,{]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)' )
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_FERNET_TOKEN = re.compile(r"\AgAAAAA[A-Za-z0-9_-]{80,}={0,2}\Z")
_OUTPUT_EMISSION = re.compile(
    r"\b(?:text|image|generatedImage)\s*\((.*?)\)\s*;", re.DOTALL
)


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
            return AdaptedToolCall("Workflow", {"script": raw_input})
        if not _is_simple_result_forwarder(raw_input, calls[0]):
            return AdaptedToolCall("Workflow", {"script": raw_input})
        decoded = _decode_object_literal(calls[0].argument)
        if decoded is None:
            return AdaptedToolCall("Workflow", {"script": raw_input})
        return _canonicalize(calls[0].name, decoded)
    return _canonicalize(name, input_data)


def _is_simple_result_forwarder(source: str, call: _StaticCall) -> bool:
    """Reject compound exec programs even when they contain one tools.* call."""
    if re.search(r"\bALL_TOOLS\b", source):
        return False
    assignment = re.search(
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"await\s+tools\."
        + re.escape(call.name)
        + r"\s*\(",
        source,
    )
    if assignment is None:
        return False
    emissions = _OUTPUT_EMISSION.findall(source)
    if len(emissions) != 1:
        return False
    result_name = assignment.group(1)
    return re.search(r"\b" + re.escape(result_name) + r"\b", emissions[0]) is not None


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
        prompt = input_data.get("message")
        task_name = input_data.get("task_name")
        if isinstance(prompt, str) and isinstance(task_name, str):
            visible_prompt = "" if _FERNET_TOKEN.fullmatch(prompt) else prompt
            return AdaptedToolCall(
                "Task",
                {
                    "prompt": visible_prompt,
                    "subagent_type": "codex",
                    "description": task_name,
                    "name": task_name,
                },
            )

    if name in {"send_message", "followup_task"}:
        target = input_data.get("target")
        message = input_data.get("message")
        if isinstance(target, str) and isinstance(message, str):
            return AdaptedToolCall(
                "SendMessage",
                {
                    "type": "followup" if name == "followup_task" else "message",
                    "recipient": target,
                    "content": message,
                },
            )

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
                todos.append(
                    {"content": step, "activeForm": step, "status": status}
                )
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
            _StaticCall(name=name_match.group(0), argument=source[cursor + 1 : end])
        )
        index = end + 1
    return calls


def _decode_object_literal(argument: str) -> Optional[dict[str, Any]]:
    value = argument.strip()
    if not value.startswith("{") or not value.endswith("}"):
        return None
    # Codex-generated wrappers use JSON values with JavaScript identifier keys.
    # Quote those keys and remove trailing commas; unsupported expressions fail
    # closed and keep the original Workflow rendering.
    json_like = _OBJECT_KEY.sub(r'\1"\2"\3', value)
    json_like = _TRAILING_COMMA.sub(r"\1", json_like)
    try:
        decoded: Any = json.loads(json_like)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], decoded) if isinstance(decoded, dict) else None


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
