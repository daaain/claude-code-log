"""Tree-sitter parsing and bounded Codex JavaScript analysis."""

from claude_code_log.providers.codex_javascript import (
    analyze_javascript_tools,
    parse_javascript,
)


def test_parse_javascript_retains_exact_utf8_node_ranges() -> None:
    syntax = parse_javascript('const subject = "Ready 😎";')

    assert syntax is not None
    declaration = syntax.root.named_children[0]
    declarator = declaration.named_children[0]
    value = declarator.child_by_field_name("value")
    assert value is not None
    assert syntax.text(value) == '"Ready 😎"'


def test_parse_javascript_rejects_recovered_syntax() -> None:
    assert parse_javascript("const result = await tools.exec_command({") is None


def test_parse_javascript_enforces_source_and_node_limits() -> None:
    assert parse_javascript("const value = 1;", max_source_bytes=5) is None
    assert parse_javascript("const value = 1;", max_syntax_nodes=2) is None


def test_analyzer_resolves_constant_shorthand_tool_input() -> None:
    batch = analyze_javascript_tools(
        """
        const actor = "/workspace/codex";
        const to = "/workspace/clmail/alice";
        const r = await tools.mcp__clmail__communicate({
          action: "send",
          actor,
          params: {to, subject: "Ready"}
        });
        text(r);
        """
    )

    assert batch is not None
    assert [(call.name, call.input) for call in batch.calls] == [
        (
            "mcp__clmail__communicate",
            {
                "action": "send",
                "actor": "/workspace/codex",
                "params": {"to": "/workspace/clmail/alice", "subject": "Ready"},
            },
        )
    ]
    assert batch.result_indexes == [0]


def test_analyzer_unrolls_static_for_of_tool_calls() -> None:
    batch = analyze_javascript_tools(
        """
        for (const id of [4745, 4746, 4756]) {
          const r = await tools.mcp__clmail__communicate({
            action: "read",
            actor: "/workspace/codex",
            params: {id}
          });
          text(JSON.stringify(r));
        }
        """
    )

    assert batch is not None
    assert [call.input["params"] for call in batch.calls] == [
        {"id": 4745},
        {"id": 4746},
        {"id": 4756},
    ]
    assert batch.result_indexes == [0, 1, 2]


def test_analyzer_rejects_dynamic_or_oversized_loops_without_partial_results() -> None:
    dynamic = """
        for (const id of ids) {
          const r = await tools.exec_command({cmd: id});
          text(r.output);
        }
    """
    oversized = """
        for (const id of [1, 2, 3]) {
          const r = await tools.exec_command({cmd: "echo", id});
          text(r.output);
        }
    """

    assert analyze_javascript_tools(dynamic) is None
    assert analyze_javascript_tools(oversized, max_loop_iterations=2) is None
