"""Canonicalize Codex tool calls for the shared renderer pipeline.

Codex persists many calls inside a ``custom_tool_call`` named ``exec`` whose
input is a small JavaScript orchestration program.  This module unwraps static
``tools.<name>({...})`` invocations with JSON-compatible object literals when
their emitted outputs can be correlated unambiguously.  Dynamic programs
remain visible as ``Workflow`` tools, and anything unknown retains its original
name and input for the generic renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Literal, Optional, cast

from .codex_javascript import analyze_javascript_tools


@dataclass(frozen=True)
class AdaptedToolCall:
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class AdaptedToolBatch:
    calls: list[AdaptedToolCall]
    output_mode: Literal["markers", "ordered"]
    result_indexes: list[int]
    session_markers: bool = False
    result_prefixes: tuple[Optional[str], ...] = ()
    synthetic_results: tuple[Optional[str], ...] = ()
    output_count: Optional[int] = None


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
        analyzed = analyze_javascript_tools(raw_input)
        if analyzed is not None and len(analyzed.calls) == 1:
            call = analyzed.calls[0]
            return _canonicalize(call.name, call.input)
        return _workflow(raw_input)
    return _canonicalize(name, input_data)


def adapt_codex_tool_batch(source: str) -> Optional[AdaptedToolBatch]:
    """Decode static multi-tool programs whose outputs remain correlatable."""
    analyzed = analyze_javascript_tools(source)
    if analyzed is not None and len(analyzed.calls) >= 2:
        adapted = [_canonicalize(call.name, call.input) for call in analyzed.calls]
        if all(call.name != "Workflow" for call in adapted):
            return AdaptedToolBatch(
                adapted,
                analyzed.output_mode,
                analyzed.result_indexes,
                analyzed.session_markers,
                analyzed.result_prefixes,
                analyzed.synthetic_results,
                analyzed.output_count,
            )
    return None


def adapt_codex_tool_call_legacy(
    name: str,
    input_data: dict[str, Any],
    *,
    raw_input: Any = None,
) -> AdaptedToolCall:
    """Run the pre-Tree-sitter recognizer explicitly as a comparison baseline."""
    if name == "exec" and isinstance(raw_input, str):
        calls = _find_static_tool_calls(raw_input)
        if len(calls) != 1:
            return _workflow(raw_input)
        if calls[0].name == "apply_patch":
            patch = _decode_apply_patch_exec(raw_input, calls[0])
            adapted = _canonicalize_patch(patch) if patch is not None else None
            return adapted if adapted is not None else _workflow(raw_input)
        if not _is_simple_result_forwarder(raw_input, calls[0]):
            return _workflow(raw_input)
        decoded = _decode_object_literal(calls[0].argument)
        if decoded is None:
            return _workflow(raw_input)
        return _canonicalize(calls[0].name, decoded)
    return _canonicalize(name, input_data)


def adapt_codex_tool_batch_legacy(source: str) -> Optional[AdaptedToolBatch]:
    """Run the pre-Tree-sitter batch recognizer explicitly for comparison."""
    calls = _find_static_tool_calls(source)
    if len(calls) < 2:
        return None
    promise = _adapt_promise_batch(source, calls)
    if promise is not None:
        return promise
    return _adapt_sequential_batch(source, calls)


def _adapt_promise_batch(
    source: str, calls: list[_StaticCall]
) -> Optional[AdaptedToolBatch]:
    code = _code_projection(source)
    assignment = re.search(
        r"\bconst\s+(?P<results>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"await\s+Promise\s*\.\s*all\s*\(",
        code,
    )
    if assignment is None:
        return None
    open_paren = assignment.end() - 1
    close_paren = _matching_delimiter(source, open_paren, "(", ")")
    array_start = _skip_space(source, open_paren + 1)
    if close_paren is None or array_start >= len(source) or source[array_start] != "[":
        return None
    array_end = _matching_delimiter(source, array_start, "[", "]")
    if array_end is None or _skip_space(source, array_end + 1) != close_paren:
        return None
    if any(call.start < array_start or call.end > array_end for call in calls):
        return None

    array_remainder = list(code[array_start + 1 : array_end])
    for call in calls:
        tool_start = source.rfind("tools.", array_start + 1, call.start)
        if tool_start < 0:
            return None
        start = tool_start - array_start - 1
        end = call.end + 1 - array_start - 1
        array_remainder[start:end] = " " * (end - start)
    if re.fullmatch(r"[\s,]*", "".join(array_remainder)) is None:
        return None

    statement_end = _statement_end(code, close_paren + 1)
    if statement_end is None:
        return None
    tail = re.sub(r"\s+", "", source[statement_end:])
    results = assignment.group("results")
    marker_tails = {
        f"{results}.forEach((r,i)=>{{text(`RESULT_${{i+1}}`);text(r.output)}});",
        f"{results}.forEach((r,i)=>{{text(`RESULT_${{i+1}}`);text(r.output);"
        "if(r.session_id)text(`SESSION_ID=${r.session_id}`)});",
    }
    ordered_tails = {
        f"for(constrof{results})text(r.output);",
        f"for(constrof{results}){{text(r.output);}}",
    }
    if tail in marker_tails:
        output_mode: Literal["markers", "ordered"] = "markers"
    elif tail in ordered_tails:
        output_mode = "ordered"
    else:
        return None

    adapted: list[AdaptedToolCall] = []
    for call in calls:
        decoded = _decode_object_literal(call.argument)
        if decoded is None:
            return None
        item = _canonicalize(call.name, decoded)
        if item.name == "Workflow":
            return None
        adapted.append(item)
    return AdaptedToolBatch(adapted, output_mode, list(range(len(adapted))))


def _adapt_sequential_batch(
    source: str, calls: list[_StaticCall]
) -> Optional[AdaptedToolBatch]:
    """Decode static calls whose emitted result variables are unambiguous."""
    code = _code_projection(source)
    remainder = list(code)
    adapted: list[AdaptedToolCall] = []
    emissions = _find_output_emissions(source)
    if len(emissions) != len(calls):
        return None

    assignments: list[tuple[int, int, str]] = []
    for call in calls:
        assignment = _call_assignment(code, call)
        if assignment is None:
            return None
        assignments.append(assignment)
        assignment_start, assignment_end, _ = assignment
        decoded = _decode_object_literal(call.argument)
        if decoded is None:
            return None
        item = _canonicalize(call.name, decoded)
        if item.name == "Workflow":
            return None
        adapted.append(item)
        remainder[assignment_start:assignment_end] = " " * (
            assignment_end - assignment_start
        )

    result_indexes = [-1] * len(calls)
    result_names = [assignment[2] for assignment in assignments]
    for emission_index, emission in enumerate(emissions):
        owners = [
            index
            for index, result_name in enumerate(result_names)
            if _is_result_expression(emission.expression, result_name)
        ]
        if len(owners) != 1 or result_indexes[owners[0]] != -1:
            return None
        result_indexes[owners[0]] = emission_index
        remainder[emission.start : emission.end] = " " * (emission.end - emission.start)

    if -1 in result_indexes or "".join(remainder).strip():
        return None
    return AdaptedToolBatch(adapted, "ordered", result_indexes)


def _is_simple_result_forwarder(source: str, call: _StaticCall) -> bool:
    """Reject compound exec programs even when they contain one tools.* call."""
    code = _code_projection(source)
    if re.search(r"\bALL_TOOLS\b", code):
        return False
    assignment = _call_assignment(code, call)
    if assignment is None:
        return False
    assignment_start, assignment_end, result_name = assignment
    emissions = _find_output_emissions(source)
    if not emissions:
        return False
    if any(
        not _is_result_expression(item.expression, result_name) for item in emissions
    ):
        return False
    remainder = list(code)
    for start, end in [(assignment_start, assignment_end)] + [
        (item.start, item.end) for item in emissions
    ]:
        remainder[start:end] = " " * (end - start)
    return not "".join(remainder).strip()


def _call_assignment(code: str, call: _StaticCall) -> Optional[tuple[int, int, str]]:
    pattern = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
        r"await\s+tools\." + re.escape(call.name) + r"\s*\("
    )
    assignment = next(
        (match for match in pattern.finditer(code) if match.end() - 1 == call.start),
        None,
    )
    if assignment is None:
        return None
    assignment_end = _statement_end(code, call.end + 1)
    if assignment_end is None:
        return None
    return assignment.start(), assignment_end, assignment.group(1)


def _is_result_expression(expression: str, result_name: str) -> bool:
    code = _code_projection(expression).strip()
    direct = re.fullmatch(
        re.escape(result_name) + r"(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)?",
        code,
    )
    if direct is not None:
        return True
    if re.fullmatch(
        r"JSON\s*\.\s*stringify\s*\(\s*" + re.escape(result_name) + r"\s*\)",
        code,
    ):
        return True
    value = expression.strip()
    if len(value) < 2 or value[0] != "`" or value[-1] != "`":
        return False
    interpolation = re.compile(
        r"\$\{\s*" + re.escape(result_name) + r"\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\}"
    )
    body = value[1:-1]
    return interpolation.search(body) is not None and "${" not in interpolation.sub(
        "", body
    )


def _workflow(source: str) -> AdaptedToolCall:
    return AdaptedToolCall("Workflow", {"script": _scrub_opaque_literals(source)})


def _canonicalize(name: str, input_data: dict[str, Any]) -> AdaptedToolCall:
    if name == "apply_patch":
        raw_patch = input_data.get("patch", input_data.get("raw"))
        if isinstance(raw_patch, str):
            patch_call = _canonicalize_patch(raw_patch)
            if patch_call is not None:
                return patch_call

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


def _decode_apply_patch_exec(source: str, call: _StaticCall) -> Optional[str]:
    """Decode Codex's ``const patch = "..."; text(await apply_patch(patch))``."""
    argument = call.argument.strip()
    if _IDENTIFIER.fullmatch(argument) is None:
        return None
    code = _code_projection(source)
    assignment = re.search(
        r"\b(?:const|let|var)\s+" + re.escape(argument) + r"\s*=", code
    )
    if assignment is None:
        return None
    literal_start = _skip_space(source, assignment.end())
    if literal_start >= len(source) or source[literal_start] != '"':
        return None
    literal_end = _skip_string(source, literal_start, '"')
    if literal_end > len(source) or source[literal_end - 1 : literal_end] != '"':
        return None
    assignment_end = _statement_end(code, literal_end)
    if assignment_end is None:
        return None

    emissions = _find_output_emissions(source)
    if len(emissions) != 1:
        return None
    expression = _code_projection(emissions[0].expression).strip()
    expected = (
        r"await\s+tools\s*\.\s*apply_patch\s*\(\s*" + re.escape(argument) + r"\s*\)"
    )
    if re.fullmatch(expected, expression) is None:
        return None

    remainder = list(code)
    for start, end in (
        (assignment.start(), assignment_end),
        (emissions[0].start, emissions[0].end),
    ):
        remainder[start:end] = " " * (end - start)
    if "".join(remainder).strip():
        return None
    try:
        decoded: Any = json.loads(source[literal_start:literal_end])
    except (ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, str) else None


def _canonicalize_patch(patch: str) -> Optional[AdaptedToolCall]:
    """Represent apply_patch operations with the Edit/MultiEdit renderers."""
    lines = patch.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return None
    headers = [
        (index, match)
        for index, line in enumerate(lines)
        if (
            match := re.fullmatch(
                r"\*\*\* (Add|Delete|Update) File: (?P<path>.+)", line
            )
        )
    ]
    if not headers or headers[0][0] != 1:
        return None
    edits: list[dict[str, str]] = []
    for position, (header_index, header) in enumerate(headers):
        body_end = (
            headers[position + 1][0] if position + 1 < len(headers) else len(lines) - 1
        )
        edit = _patch_edit(
            header.group(1),
            header.group("path"),
            lines[header_index + 1 : body_end],
        )
        if edit is None:
            return None
        edits.append(edit)
    if len(edits) == 1:
        return AdaptedToolCall("Edit", edits[0])
    return AdaptedToolCall(
        "MultiEdit",
        {
            "file_path": f"{len({edit['file_path'] for edit in edits})} files",
            "edits": edits,
        },
    )


def _patch_edit(operation: str, path: str, body: list[str]) -> Optional[dict[str, str]]:
    if any(line.startswith("*** Move to:") for line in body):
        return None

    if operation == "Add":
        if any(not line.startswith("+") for line in body):
            return None
        old_string = ""
        new_string = _patch_text([line[1:] for line in body])
    elif operation == "Delete":
        if body and any(not line.startswith("-") for line in body):
            return None
        old_string = (
            _patch_text([line[1:] for line in body]) if body else "[deleted file]\n"
        )
        new_string = ""
    else:
        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in body:
            if line.startswith("@@") or line == "*** End of File":
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                return None
            if line[0] != "+":
                old_lines.append(line[1:])
            if line[0] != "-":
                new_lines.append(line[1:])
        old_string = _patch_text(old_lines)
        new_string = _patch_text(new_lines)

    return {"file_path": path, "old_string": old_string, "new_string": new_string}


def _patch_text(lines: list[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


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
