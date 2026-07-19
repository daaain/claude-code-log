"""Bounded JavaScript parsing for Codex ``exec`` tool wrappers.

Transcript JavaScript is untrusted input.  This module only builds a concrete
syntax tree; it never evaluates source code.  Callers receive no tree when the
source is malformed or exceeds the configured parsing limits.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from typing import Any, Literal, Optional, TypeAlias, cast

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
    output_mode: Literal["markers", "ordered"] = "ordered"
    session_markers: bool = False
    result_prefixes: tuple[Optional[str], ...] = ()
    synthetic_results: tuple[Optional[str], ...] = ()
    output_count: int = 0
    result_object_keys: tuple[Optional[str], ...] = ()


@dataclass(frozen=True)
class _Constant:
    value: Any


@dataclass(frozen=True)
class _ToolResult:
    call_index: int
    path: tuple[str, ...] = ()
    object_key: Optional[str] = None
    output_prefix: Optional[str] = None


@dataclass(frozen=True)
class _Collection:
    values: tuple[_ToolResult, ...]


_AbstractValue: TypeAlias = _Collection | _Constant | _ToolResult
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
    """Materialize a safe, bounded subset, failing closed on parser surprises."""
    try:
        syntax = parse_javascript(source)
        if syntax is None:
            return None
        evaluator = _StaticEvaluator(
            syntax,
            max_loop_iterations=max_loop_iterations,
            max_expanded_calls=max_expanded_calls,
        )
        return evaluator.evaluate()
    except Exception:
        # Transcript JavaScript is untrusted, and decoding it is best-effort.
        # An unforeseen tree-sitter node shape or dependency API change must
        # leave the original ToolExecution visible rather than abort rendering.
        return None


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
        self.synthetic_results: list[Optional[str]] = []
        self.emission_groups: list[tuple[tuple[_ToolResult, Optional[str]], ...]] = []
        self.emission_prefixes: list[Optional[str]] = []
        self.loop_iterations = 0
        self.output_mode: Literal["markers", "ordered"] = "ordered"
        self.session_markers = False

    def evaluate(self) -> Optional[JavaScriptToolBatch]:
        environment: _Environment = {}
        if not self._statements(self.syntax.root.named_children, environment):
            return None
        if not self.calls or not self.emission_groups:
            return None

        if len(self.calls) == 1 and all(
            len(group) == 1 and group[0][0].call_index == 0
            for group in self.emission_groups
        ):
            return JavaScriptToolBatch(
                self.calls,
                [0],
                self.output_mode,
                self.session_markers,
                tuple(self.emission_prefixes),
                tuple(self.synthetic_results),
                len(self.emission_groups),
                (self.emission_groups[0][0][1],),
            )
        if len(self.calls) != sum(map(len, self.emission_groups)) + sum(
            result is not None for result in self.synthetic_results
        ):
            return None

        result_indexes = [-1] * len(self.calls)
        result_object_keys: list[Optional[str]] = [None] * len(self.calls)
        for output_index, group in enumerate(self.emission_groups):
            for result, object_key in group:
                if result_indexes[result.call_index] != -1:
                    return None
                result_indexes[result.call_index] = output_index
                result_object_keys[result.call_index] = object_key
        if any(
            result_index == -1 and synthetic is None
            for result_index, synthetic in zip(result_indexes, self.synthetic_results)
        ):
            return None
        return JavaScriptToolBatch(
            self.calls,
            result_indexes,
            self.output_mode,
            self.session_markers,
            tuple(self.emission_prefixes),
            tuple(self.synthetic_results),
            len(self.emission_groups),
            tuple(result_object_keys),
        )

    def _statements(self, statements: list[Node], environment: _Environment) -> bool:
        for statement in statements:
            if statement.type == "comment":
                continue
            if statement.type == "lexical_declaration":
                if not self._declaration(statement, environment):
                    return False
                continue
            if statement.type == "expression_statement":
                if (
                    not self._delay(statement)
                    and not self._emission(statement, environment)
                    and not self._collection_for_each(statement, environment)
                ):
                    return False
                continue
            if statement.type == "for_in_statement":
                if not self._for_of(statement, environment):
                    return False
                continue
            if statement.type == "if_statement":
                if self._session_marker(statement, environment) is None:
                    return False
                self.session_markers = True
                continue
            return False
        return True

    def _delay(self, statement: Node) -> bool:
        """Recognize Codex's static Promise/setTimeout delay wrapper."""
        expressions = statement.named_children
        if len(expressions) != 1 or expressions[0].type != "await_expression":
            return False
        awaited = expressions[0].named_children
        if len(awaited) != 1 or awaited[0].type != "new_expression":
            return False
        promise = awaited[0]
        constructor = promise.child_by_field_name("constructor")
        arguments = promise.child_by_field_name("arguments")
        if (
            constructor is None
            or constructor.type != "identifier"
            or self.syntax.text(constructor) != "Promise"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return False
        callback = arguments.named_children[0]
        parameter = callback.child_by_field_name("parameter")
        body = callback.child_by_field_name("body")
        if (
            callback.type != "arrow_function"
            or parameter is None
            or parameter.type != "identifier"
            or body is None
            or body.type != "call_expression"
        ):
            return False
        function = body.child_by_field_name("function")
        timeout_arguments = body.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "identifier"
            or self.syntax.text(function) != "setTimeout"
            or timeout_arguments is None
            or len(timeout_arguments.named_children) != 2
        ):
            return False
        resolve, delay_node = timeout_arguments.named_children
        delay = self._constant(delay_node, {})
        valid = (
            resolve.type == "identifier"
            and self.syntax.text(resolve) == self.syntax.text(parameter)
            and isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and delay >= 0
            and delay != float("inf")
        )
        if not valid:
            return False
        self.calls.append(JavaScriptToolCall("wait", {"delay_ms": delay}))
        self.synthetic_results.append(f"Waited {delay} ms")
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
            if name is None or value is None:
                return False
            abstract = self._declaration_value(value, environment)
            if abstract is None or not self._bind_declaration(
                name, abstract, environment
            ):
                return False
        return True

    def _bind_declaration(
        self, name: Node, value: _AbstractValue, environment: _Environment
    ) -> bool:
        if name.type == "identifier":
            identifier = self.syntax.text(name)
            if identifier in environment:
                return False
            environment[identifier] = value
            return True

        if name.type != "array_pattern":
            return False
        if isinstance(value, _Collection):
            values: tuple[_AbstractValue, ...] = value.values
        elif isinstance(value, _Constant):
            raw_values: Any = value.value
            if not isinstance(raw_values, list):
                return False
            values = tuple(_Constant(item) for item in cast(list[Any], raw_values))
        else:
            return False
        bindings = name.named_children
        if len(bindings) != len(values) or not all(
            binding.type == "identifier" for binding in bindings
        ):
            return False

        # Accept only the plain ``[a, b]`` form.  Tree-sitter omits elisions
        # from named_children, so checking the punctuation sequence prevents
        # accidentally rebinding ``[a, , b]`` to adjacent results.
        children = name.children
        if len(children) != 2 * len(bindings) + 1:
            return False
        if children[0].type != "[" or children[-1].type != "]":
            return False
        if any(children[index].type != "," for index in range(2, len(children) - 1, 2)):
            return False
        if any(
            children[index] != binding
            for index, binding in zip(range(1, len(children) - 1, 2), bindings)
        ):
            return False

        identifiers = [self.syntax.text(binding) for binding in bindings]
        if len(set(identifiers)) != len(identifiers) or any(
            identifier in environment for identifier in identifiers
        ):
            return False
        environment.update(zip(identifiers, values))
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
        collection = self._promise_all(expressions[0], environment)
        if collection is not None:
            return collection
        call = self._tool_call(expressions[0], environment)
        if call is None or len(self.calls) >= self.max_expanded_calls:
            return None
        call_index = len(self.calls)
        self.calls.append(call)
        self.synthetic_results.append(None)
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
        if len(values) != 1:
            return None
        tool_name = self.syntax.text(property_node)
        input_data = self._constant(values[0], environment)
        if tool_name == "apply_patch" and isinstance(input_data, str):
            input_data = {"patch": input_data}
        if input_data is _UNKNOWN or not isinstance(input_data, dict):
            return None
        return JavaScriptToolCall(
            name=tool_name,
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
        argument = arguments.named_children[0]
        if argument.type == "await_expression":
            awaited = argument.named_children
            if len(awaited) != 1 or len(self.calls) >= self.max_expanded_calls:
                return False
            tool_call = self._tool_call(awaited[0], environment)
            if tool_call is None:
                return False
            result = _ToolResult(len(self.calls))
            self.calls.append(tool_call)
            self.synthetic_results.append(None)
        else:
            object_results = self._result_object_emission(argument, environment)
            if object_results is not None:
                self.emission_groups.append(object_results)
                self.emission_prefixes.append(None)
                return len(self.emission_groups) <= self.max_expanded_calls
            result = self._result_reference(argument, environment)
        if result is None:
            return False
        self.emission_groups.append(((result, result.object_key),))
        self.emission_prefixes.append(
            self._result_prefix(argument, environment, result)
        )
        return len(self.emission_groups) <= self.max_expanded_calls

    def _result_object_emission(
        self, node: Node, environment: _Environment
    ) -> Optional[tuple[tuple[_ToolResult, Optional[str]], ...]]:
        """Resolve ``JSON.stringify({key: result, shorthand})`` provenance."""
        if node.type != "call_expression" or not self._is_json_stringify(node):
            return None
        arguments = node.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) != 1:
            return None
        object_node = arguments.named_children[0]
        if object_node.type != "object" or not object_node.named_children:
            return None

        results: list[tuple[_ToolResult, Optional[str]]] = []
        keys: set[str] = set()
        for child in object_node.named_children:
            if child.type == "shorthand_property_identifier":
                key = self.syntax.text(child)
                value = environment.get(key)
                result = value if isinstance(value, _ToolResult) else None
            elif child.type == "pair":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                if key_node is None or value_node is None:
                    return None
                if key_node.type in {"property_identifier", "identifier"}:
                    key = self.syntax.text(key_node)
                elif key_node.type == "string":
                    decoded_key = _decode_string(self.syntax.text(key_node))
                    if not isinstance(decoded_key, str):
                        return None
                    key = decoded_key
                else:
                    return None
                result = self._result_reference(value_node, environment)
            else:
                return None
            if result is None or key in keys:
                return None
            keys.add(key)
            results.append((result, key))
        return tuple(results)

    def _result_prefix(
        self, node: Node, environment: _Environment, result: _ToolResult
    ) -> Optional[str]:
        """Return the static text before a result embedded in a template."""
        if node.type != "template_string":
            return None
        parts: list[str] = []
        for child in node.named_children:
            if child.type == "string_fragment":
                parts.append(self.syntax.text(child))
                continue
            if child.type == "escape_sequence":
                escape = _decode_template_escape(self.syntax.text(child))
                if escape is _UNKNOWN:
                    return None
                parts.append(cast(str, escape))
                continue
            if child.type != "template_substitution" or len(child.named_children) != 1:
                return None
            expression = child.named_children[0]
            reference = self._result_reference(expression, environment)
            if reference is not None:
                return (
                    "".join(parts)
                    if reference.call_index == result.call_index
                    else None
                )
            rendered = _template_primitive(self._constant(expression, environment))
            if rendered is _UNKNOWN:
                return None
            parts.append(cast(str, rendered))
        return None

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
                result.call_index,
                result.path + (self.syntax.text(property_node),),
                result.object_key,
                result.output_prefix,
            )
        if node.type == "call_expression" and self._is_json_stringify(node):
            arguments = node.child_by_field_name("arguments")
            if arguments is None or len(arguments.named_children) != 1:
                return None
            return self._result_reference(arguments.named_children[0], environment)
        if node.type == "template_string":
            results: list[_ToolResult] = []
            for child in node.named_children:
                if child.type in {"string_fragment", "escape_sequence"}:
                    continue
                if (
                    child.type != "template_substitution"
                    or len(child.named_children) != 1
                ):
                    return None
                expression = child.named_children[0]
                result = self._result_reference(expression, environment)
                if result is not None:
                    results.append(result)
                    continue
                constant = self._constant(expression, environment)
                if constant is _UNKNOWN or not isinstance(
                    constant, (str, int, float, bool, type(None))
                ):
                    return None
            if results and all(
                item.call_index == results[0].call_index for item in results
            ):
                return results[0]
        return None

    def _promise_all(
        self, node: Node, environment: _Environment
    ) -> Optional[_Collection]:
        if node.type != "call_expression":
            return None
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "member_expression"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return None
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        if (
            owner is None
            or owner.type != "identifier"
            or self.syntax.text(owner) != "Promise"
            or property_node is None
            or property_node.type != "property_identifier"
            or self.syntax.text(property_node) != "all"
        ):
            return None
        source = arguments.named_children[0]
        if source.type == "call_expression":
            return self._promise_all_map(source, environment)
        if source.type != "array":
            return None

        results: list[_ToolResult] = []
        for element in source.named_children:
            if len(self.calls) >= self.max_expanded_calls:
                return None
            call = self._tool_call(element, environment)
            if call is None:
                return None
            results.append(_ToolResult(len(self.calls)))
            self.calls.append(call)
            self.synthetic_results.append(None)
        return _Collection(tuple(results))

    def _promise_all_map(
        self, node: Node, environment: _Environment
    ) -> Optional[_Collection]:
        """Expand ``Promise.all(staticArray.map(async (...) => ...))``."""
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "member_expression"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return None
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        if (
            owner is None
            or owner.type != "identifier"
            or property_node is None
            or property_node.type != "property_identifier"
            or self.syntax.text(property_node) != "map"
        ):
            return None
        collection = environment.get(self.syntax.text(owner))
        if not isinstance(collection, _Constant) or not isinstance(
            collection.value, list
        ):
            return None
        values = cast(
            list[Any],
            collection.value,  # pyright: ignore[reportUnknownMemberType]
        )
        arrow = arguments.named_children[0]
        parameters = arrow.child_by_field_name("parameters")
        body = arrow.child_by_field_name("body")
        if (
            arrow.type != "arrow_function"
            or parameters is None
            or len(parameters.named_children) != 1
            or body is None
            or body.type != "statement_block"
            or len(body.named_children) != 2
        ):
            return None
        parameter = parameters.named_children[0]
        declaration, return_statement = body.named_children
        if (
            parameter.type not in {"identifier", "array_pattern"}
            or declaration.type != "lexical_declaration"
            or return_statement.type != "return_statement"
            or len(return_statement.named_children) != 1
        ):
            return None
        if self.loop_iterations + len(values) > self.max_loop_iterations:
            return None

        self.loop_iterations += len(values)
        results: list[_ToolResult] = []
        for value in values:
            if len(self.calls) >= self.max_expanded_calls:
                return None
            iteration_environment = dict(environment)
            if not self._bind_declaration(
                parameter, _Constant(value), iteration_environment
            ):
                return None
            call_count = len(self.calls)
            if not self._declaration(declaration, iteration_environment):
                return None
            result = self._mapped_result_object(
                return_statement.named_children[0], iteration_environment
            )
            if (
                result is None
                or len(self.calls) != call_count + 1
                or result.call_index != call_count
                or self.calls[result.call_index].name != "exec_command"
            ):
                return None
            results.append(result)
        return _Collection(tuple(results))

    def _mapped_result_object(
        self, node: Node, environment: _Environment
    ) -> Optional[_ToolResult]:
        """Resolve a static metadata object exposing one tool's ``output``.

        Codex emits both ``{name, ...result}`` and explicit projections such
        as ``{name, exit_code: result.exit_code, output: result.output}``.
        Only direct, same-named top-level fields from one result are accepted;
        the ``output`` projection is required because that is the value the
        shared Bash renderer consumes.
        """
        if node.type != "object":
            return None
        call_index: Optional[int] = None
        exposes_output = False
        output_prefix: Optional[str] = None
        keys: set[str] = set()
        for child in node.named_children:
            static_value: Any = _UNKNOWN
            if child.type == "spread_element":
                if call_index is not None or len(child.named_children) != 1:
                    return None
                reference = self._result_reference(child.named_children[0], environment)
                if reference is None or reference.path:
                    return None
                call_index = reference.call_index
                exposes_output = True
                continue
            if child.type == "shorthand_property_identifier":
                key = self.syntax.text(child)
                item = environment.get(key)
                if not isinstance(item, _Constant):
                    return None
                static_value = item.value
            elif child.type == "pair":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                if key_node is None or value_node is None:
                    return None
                key = self._object_key(key_node)
                reference = self._result_reference(value_node, environment)
                if reference is not None:
                    if (
                        key is None
                        or reference.path != (key,)
                        or (
                            call_index is not None
                            and call_index != reference.call_index
                        )
                    ):
                        return None
                    call_index = reference.call_index
                    exposes_output = exposes_output or key == "output"
                else:
                    static_value = self._constant(value_node, environment)
                    if static_value is _UNKNOWN:
                        return None
            else:
                return None
            if key is None or key in keys:
                return None
            if not keys and static_value is not _UNKNOWN:
                try:
                    output_prefix = json.dumps(
                        {key: static_value},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )[:-1]
                except (TypeError, ValueError):
                    return None
            keys.add(key)
        if call_index is None or not exposes_output:
            return None
        return _ToolResult(
            call_index,
            object_key="output",
            output_prefix=output_prefix,
        )

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
            or left.type not in {"identifier", "array_pattern"}
            or operator is None
            or self.syntax.text(operator) != "of"
            or right is None
            or body is None
        ):
            return False
        right_value = (
            environment.get(self.syntax.text(right))
            if right.type == "identifier"
            else None
        )
        if isinstance(right_value, _Collection):
            loop_values: list[Any] = list(right_value.values)
        else:
            values = self._constant(right, environment)
            if values is _UNKNOWN or not isinstance(values, list):
                return False
            loop_values = cast(list[Any], values)
        if self.loop_iterations + len(loop_values) > self.max_loop_iterations:
            return False

        self.loop_iterations += len(loop_values)
        for value in loop_values:
            iteration_environment = dict(environment)
            abstract = value if isinstance(value, _ToolResult) else _Constant(value)
            if not self._bind_declaration(left, abstract, iteration_environment):
                return False
            statements = (
                body.named_children if body.type == "statement_block" else [body]
            )
            if not self._statements(statements, iteration_environment):
                return False
        return True

    def _collection_for_each(self, statement: Node, environment: _Environment) -> bool:
        expressions = statement.named_children
        if len(expressions) != 1 or expressions[0].type != "call_expression":
            return False
        call = expressions[0]
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "member_expression"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return False
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        if (
            owner is None
            or owner.type != "identifier"
            or property_node is None
            or property_node.type != "property_identifier"
            or self.syntax.text(property_node) != "forEach"
        ):
            return False
        collection = environment.get(self.syntax.text(owner))
        callback = arguments.named_children[0]
        if not isinstance(collection, _Collection):
            return False
        if callback.type == "identifier" and self.syntax.text(callback) == "text":
            if (
                len(self.emission_groups) + len(collection.values)
                > self.max_expanded_calls
            ):
                return False
            for result in collection.values:
                self.emission_groups.append(((result, result.object_key),))
                self.emission_prefixes.append(result.output_prefix)
            self.output_mode = "ordered"
            return True
        if callback.type != "arrow_function":
            return False
        arrow = callback
        parameters = arrow.child_by_field_name("parameters")
        body = arrow.child_by_field_name("body")
        if (
            parameters is None
            or body is None
            or body.type != "statement_block"
            or len(parameters.named_children) != 2
            or len(body.named_children) not in {2, 3}
        ):
            return False
        result_name, index_name = parameters.named_children
        if result_name.type != "identifier" or index_name.type != "identifier":
            return False
        marker, emission, *tail = body.named_children
        if not self._is_result_marker(marker, self.syntax.text(index_name)):
            return False

        for result in collection.values:
            iteration_environment = dict(environment)
            iteration_environment[self.syntax.text(result_name)] = result
            if not self._emission(emission, iteration_environment):
                return False
            if tail and self._session_marker(tail[0], iteration_environment) != result:
                return False
        self.session_markers = bool(tail)
        self.output_mode = "markers"
        return True

    def _session_marker(
        self, statement: Node, environment: _Environment
    ) -> Optional[_ToolResult]:
        if statement.type != "if_statement":
            return None
        condition = statement.child_by_field_name("condition")
        consequence = statement.child_by_field_name("consequence")
        if condition is None or consequence is None:
            return None
        if condition.type == "parenthesized_expression":
            if len(condition.named_children) != 1:
                return None
            condition = condition.named_children[0]
        condition_result = self._result_reference(condition, environment)
        if condition_result is None or condition_result.path != ("session_id",):
            return None

        expressions = consequence.named_children
        if len(expressions) != 1 or expressions[0].type != "call_expression":
            return None
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
            return None
        template = arguments.named_children[0]
        if template.type != "template_string" or len(template.named_children) != 2:
            return None
        fragment, substitution = template.named_children
        if (
            fragment.type != "string_fragment"
            or self.syntax.text(fragment) != "SESSION_ID="
            or substitution.type != "template_substitution"
            or len(substitution.named_children) != 1
        ):
            return None
        emitted_result = self._result_reference(
            substitution.named_children[0], environment
        )
        return (
            _ToolResult(condition_result.call_index)
            if emitted_result == condition_result
            else None
        )

    def _is_result_marker(self, statement: Node, index_name: str) -> bool:
        if (
            statement.type != "expression_statement"
            or len(statement.named_children) != 1
        ):
            return False
        call = statement.named_children[0]
        if call.type != "call_expression":
            return False
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        return (
            function is not None
            and function.type == "identifier"
            and self.syntax.text(function) == "text"
            and arguments is not None
            and len(arguments.named_children) == 1
            and arguments.named_children[0].type == "template_string"
            and self.syntax.text(arguments.named_children[0])
            == f"`RESULT_${{{index_name}+1}}`"
        )

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
            children = node.children
            if not node.named_children:
                return [] if len(children) == 2 else _UNKNOWN
            if (
                len(children) != 2 * len(node.named_children) + 1
                or children[0].type != "["
                or children[-1].type != "]"
                or any(
                    children[index].type != ","
                    for index in range(2, len(children) - 1, 2)
                )
            ):
                return _UNKNOWN
            values: list[Any] = []
            for child in node.named_children:
                value = self._constant(child, environment)
                if value is _UNKNOWN:
                    return _UNKNOWN
                values.append(value)
            return values
        if node.type == "object":
            return self._object(node, environment)
        if node.type == "template_string":
            return self._template_constant(node, environment)
        if node.type == "call_expression":
            return self._array_join(node, environment)
        return _UNKNOWN

    def _array_join(self, node: Node, environment: _Environment) -> Any:
        """Evaluate a static string array's ``join`` call."""
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or function.type != "member_expression"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            return _UNKNOWN
        owner = function.child_by_field_name("object")
        property_node = function.child_by_field_name("property")
        if (
            owner is None
            or property_node is None
            or property_node.type != "property_identifier"
            or self.syntax.text(property_node) != "join"
        ):
            return _UNKNOWN
        values = self._constant(owner, environment)
        separator = self._constant(arguments.named_children[0], environment)
        if not isinstance(values, list) or not isinstance(separator, str):
            return _UNKNOWN
        items = cast(list[Any], values)
        if not all(isinstance(value, str) for value in items):
            return _UNKNOWN
        return separator.join(cast(list[str], items))

    def _template_constant(self, node: Node, environment: _Environment) -> Any:
        parts: list[str] = []
        for child in node.named_children:
            if child.type == "string_fragment":
                fragment = self.syntax.text(child)
                parts.append(fragment)
                continue
            if child.type == "escape_sequence":
                escape = _decode_template_escape(self.syntax.text(child))
                if escape is _UNKNOWN:
                    return _UNKNOWN
                parts.append(cast(str, escape))
                continue
            if child.type != "template_substitution" or len(child.named_children) != 1:
                return _UNKNOWN
            value = self._constant(child.named_children[0], environment)
            rendered = _template_primitive(value)
            if rendered is _UNKNOWN:
                return _UNKNOWN
            parts.append(cast(str, rendered))
        return "".join(parts)

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


def _template_primitive(value: Any) -> Any:
    if value is _UNKNOWN or isinstance(value, (dict, list)):
        return _UNKNOWN
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, ensure_ascii=False)
    return _UNKNOWN


def _decode_template_escape(source: str) -> Any:
    escapes = {
        r"\\": "\\",
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\b": "\b",
        r"\f": "\f",
        r"\v": "\v",
        r"\0": "\0",
        r"\`": "`",
        r"\$": "$",
        r"\'": "'",
        r"\"": '"',
    }
    return escapes.get(source, _UNKNOWN)
