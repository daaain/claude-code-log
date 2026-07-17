"""Bounded JavaScript parsing for Codex ``exec`` tool wrappers.

Transcript JavaScript is untrusted input.  This module only builds a concrete
syntax tree; it never evaluates source code.  Callers receive no tree when the
source is malformed or exceeds the configured parsing limits.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from typing import Any, Optional, TypeAlias, cast

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript


MAX_SOURCE_BYTES = 64 * 1024
MAX_SYNTAX_NODES = 8_192
MAX_LOOP_ITERATIONS = 64
MAX_EXPANDED_CALLS = 128

_JAVASCRIPT = Language(tree_sitter_javascript.language())


@dataclass(frozen=True)
class JavaScriptSyntax:
    """A JavaScript tree paired with the UTF-8 bytes that define its ranges."""

    source: bytes
    root: Node

    def text(self, node: Node) -> str:
        """Return the exact source represented by *node*."""
        return self.source[node.start_byte : node.end_byte].decode("utf-8")


@dataclass(frozen=True)
class JavaScriptToolCall:
    """One statically materialized ``tools.<name>`` invocation."""

    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class JavaScriptToolBatch:
    """Ordered calls and the output-row index belonging to each call."""

    calls: list[JavaScriptToolCall]
    result_indexes: list[int]


@dataclass(frozen=True)
class _Constant:
    value: Any


@dataclass(frozen=True)
class _ToolResult:
    call_index: int
    path: tuple[str, ...] = ()


_AbstractValue: TypeAlias = _Constant | _ToolResult
_Environment: TypeAlias = dict[str, _AbstractValue]


def parse_javascript(
    source: str,
    *,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_syntax_nodes: int = MAX_SYNTAX_NODES,
) -> Optional[JavaScriptSyntax]:
    """Parse a bounded, syntactically complete JavaScript program."""
    encoded = source.encode("utf-8")
    if len(encoded) > max_source_bytes:
        return None

    root = Parser(_JAVASCRIPT).parse(encoded).root_node
    if root.has_error or _node_count_exceeds(root, max_syntax_nodes):
        return None
    return JavaScriptSyntax(source=encoded, root=root)


def analyze_javascript_tools(
    source: str,
    *,
    max_loop_iterations: int = MAX_LOOP_ITERATIONS,
    max_expanded_calls: int = MAX_EXPANDED_CALLS,
) -> Optional[JavaScriptToolBatch]:
    """Materialize a safe, bounded subset of a Codex JavaScript wrapper."""
    syntax = parse_javascript(source)
    if syntax is None:
        return None
    evaluator = _StaticEvaluator(
        syntax,
        max_loop_iterations=max_loop_iterations,
        max_expanded_calls=max_expanded_calls,
    )
    return evaluator.evaluate()


def _node_count_exceeds(root: Node, limit: int) -> bool:
    remaining = [root]
    count = 0
    while remaining:
        node = remaining.pop()
        count += 1
        if count > limit:
            return True
        remaining.extend(node.children)
    return False


class _StaticEvaluator:
    """Abstract interpreter for the small Codex-generated wrapper subset."""

    def __init__(
        self,
        syntax: JavaScriptSyntax,
        *,
        max_loop_iterations: int,
        max_expanded_calls: int,
    ) -> None:
        self.syntax = syntax
        self.max_loop_iterations = max_loop_iterations
        self.max_expanded_calls = max_expanded_calls
        self.calls: list[JavaScriptToolCall] = []
        self.emissions: list[int] = []
        self.loop_iterations = 0

    def evaluate(self) -> Optional[JavaScriptToolBatch]:
        environment: _Environment = {}
        if not self._statements(self.syntax.root.named_children, environment):
            return None
        if not self.calls or len(self.calls) != len(self.emissions):
            return None

        result_indexes = [-1] * len(self.calls)
        for output_index, call_index in enumerate(self.emissions):
            if result_indexes[call_index] != -1:
                return None
            result_indexes[call_index] = output_index
        if -1 in result_indexes:
            return None
        return JavaScriptToolBatch(self.calls, result_indexes)

    def _statements(self, statements: list[Node], environment: _Environment) -> bool:
        for statement in statements:
            if statement.type == "comment":
                continue
            if statement.type == "lexical_declaration":
                if not self._declaration(statement, environment):
                    return False
                continue
            if statement.type == "expression_statement":
                if not self._emission(statement, environment):
                    return False
                continue
            if statement.type == "for_in_statement":
                if not self._for_of(statement, environment):
                    return False
                continue
            return False
        return True

    def _declaration(self, node: Node, environment: _Environment) -> bool:
        kind = node.child_by_field_name("kind")
        if kind is None or self.syntax.text(kind) != "const":
            return False
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                return False
            name = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if (
                name is None
                or name.type != "identifier"
                or value is None
                or self.syntax.text(name) in environment
            ):
                return False
            abstract = self._declaration_value(value, environment)
            if abstract is None:
                return False
            environment[self.syntax.text(name)] = abstract
        return True

    def _declaration_value(
        self, node: Node, environment: _Environment
    ) -> Optional[_AbstractValue]:
        if node.type != "await_expression":
            constant = self._constant(node, environment)
            return _Constant(constant) if constant is not _UNKNOWN else None
        expressions = node.named_children
        if len(expressions) != 1:
            return None
        call = self._tool_call(expressions[0], environment)
        if call is None or len(self.calls) >= self.max_expanded_calls:
            return None
        call_index = len(self.calls)
        self.calls.append(call)
        return _ToolResult(call_index)

    def _tool_call(
        self, node: Node, environment: _Environment
    ) -> Optional[JavaScriptToolCall]:
        if node.type != "call_expression":
            return None
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or arguments is None
            or function.type != "member_expression"
        ):
            return None
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        if (
            owner is None
            or owner.type != "identifier"
            or self.syntax.text(owner) != "tools"
            or property_node is None
            or property_node.type != "property_identifier"
        ):
            return None
        values = arguments.named_children
        if len(values) != 1 or values[0].type != "object":
            return None
        input_data = self._constant(values[0], environment)
        if input_data is _UNKNOWN or not isinstance(input_data, dict):
            return None
        return JavaScriptToolCall(
            name=self.syntax.text(property_node),
            input=cast(dict[str, Any], input_data),
        )

    def _emission(self, statement: Node, environment: _Environment) -> bool:
        expressions = statement.named_children
        if len(expressions) != 1 or expressions[0].type != "call_expression":
            return False
        call = expressions[0]
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "identifier"
            or self.syntax.text(function) != "text"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return False
        result = self._result_reference(arguments.named_children[0], environment)
        if result is None:
            return False
        self.emissions.append(result.call_index)
        return len(self.emissions) <= self.max_expanded_calls

    def _result_reference(
        self, node: Node, environment: _Environment
    ) -> Optional[_ToolResult]:
        if node.type == "identifier":
            value = environment.get(self.syntax.text(node))
            return value if isinstance(value, _ToolResult) else None
        if node.type == "member_expression":
            owner = node.child_by_field_name("object")
            property_node = node.child_by_field_name("property")
            if owner is None or property_node is None:
                return None
            result = self._result_reference(owner, environment)
            if result is None or property_node.type != "property_identifier":
                return None
            return _ToolResult(
                result.call_index, result.path + (self.syntax.text(property_node),)
            )
        if node.type == "call_expression" and self._is_json_stringify(node):
            arguments = node.child_by_field_name("arguments")
            if arguments is None or len(arguments.named_children) != 1:
                return None
            return self._result_reference(arguments.named_children[0], environment)
        return None

    def _is_json_stringify(self, node: Node) -> bool:
        function = node.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            return False
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        return (
            owner is not None
            and owner.type == "identifier"
            and self.syntax.text(owner) == "JSON"
            and property_node is not None
            and property_node.type == "property_identifier"
            and self.syntax.text(property_node) == "stringify"
        )

    def _for_of(self, node: Node, environment: _Environment) -> bool:
        kind = node.child_by_field_name("kind")
        left = node.child_by_field_name("left")
        operator = node.child_by_field_name("operator")
        right = node.child_by_field_name("right")
        body = node.child_by_field_name("body")
        if (
            kind is None
            or self.syntax.text(kind) != "const"
            or left is None
            or left.type != "identifier"
            or operator is None
            or self.syntax.text(operator) != "of"
            or right is None
            or body is None
            or body.type != "statement_block"
        ):
            return False
        values = self._constant(right, environment)
        if values is _UNKNOWN or not isinstance(values, list):
            return False
        loop_values = cast(list[Any], values)
        if self.loop_iterations + len(loop_values) > self.max_loop_iterations:
            return False

        self.loop_iterations += len(loop_values)
        name = self.syntax.text(left)
        for value in loop_values:
            iteration_environment = dict(environment)
            iteration_environment[name] = _Constant(value)
            if not self._statements(body.named_children, iteration_environment):
                return False
        return True

    def _constant(self, node: Node, environment: _Environment) -> Any:
        if node.type == "identifier":
            value = environment.get(self.syntax.text(node))
            return value.value if isinstance(value, _Constant) else _UNKNOWN
        if node.type == "string":
            return _decode_string(self.syntax.text(node))
        if node.type == "number":
            return _decode_number(self.syntax.text(node))
        if node.type == "true":
            return True
        if node.type == "false":
            return False
        if node.type == "null":
            return None
        if node.type == "array":
            values: list[Any] = []
            for child in node.named_children:
                value = self._constant(child, environment)
                if value is _UNKNOWN:
                    return _UNKNOWN
                values.append(value)
            return values
        if node.type == "object":
            return self._object(node, environment)
        return _UNKNOWN

    def _object(self, node: Node, environment: _Environment) -> Any:
        value: dict[str, Any] = {}
        for child in node.named_children:
            if child.type == "shorthand_property_identifier":
                key = self.syntax.text(child)
                item = environment.get(key)
                if not isinstance(item, _Constant) or key in value:
                    return _UNKNOWN
                value[key] = item.value
                continue
            if child.type != "pair":
                return _UNKNOWN
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")
            if key_node is None or value_node is None:
                return _UNKNOWN
            key = self._object_key(key_node)
            item = self._constant(value_node, environment)
            if key is None or item is _UNKNOWN or key in value:
                return _UNKNOWN
            value[key] = item
        return value

    def _object_key(self, node: Node) -> Optional[str]:
        if node.type in {"property_identifier", "number"}:
            return self.syntax.text(node)
        if node.type == "string":
            value = _decode_string(self.syntax.text(node))
            return value if isinstance(value, str) else None
        return None


_UNKNOWN = object()


def _decode_string(source: str) -> Any:
    try:
        if source.startswith('"'):
            value: Any = json.loads(source)
        else:
            value = ast.literal_eval(source)
    except (RecursionError, SyntaxError, ValueError):
        return _UNKNOWN
    return value if isinstance(value, str) else _UNKNOWN


def _decode_number(source: str) -> Any:
    try:
        value: Any = json.loads(source)
    except (ValueError, RecursionError):
        return _UNKNOWN
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return _UNKNOWN
