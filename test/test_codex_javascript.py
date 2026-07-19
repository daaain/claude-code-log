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


def test_analyzer_materializes_static_promise_delay_before_tool_call() -> None:
    batch = analyze_javascript_tools(
        """
        await new Promise(resolve => setTimeout(resolve, 5000));
        const r = await tools.mcp__clmail__communicate({
          action: "list",
          actor: "/workspace/codex",
          params: {status: "unread"}
        });
        text(JSON.stringify(r));
        """
    )

    assert batch is not None
    assert [(call.name, call.input) for call in batch.calls] == [
        ("wait", {"delay_ms": 5000}),
        (
            "mcp__clmail__communicate",
            {
                "action": "list",
                "actor": "/workspace/codex",
                "params": {"status": "unread"},
            },
        ),
    ]
    assert batch.result_indexes == [-1, 0]
    assert batch.synthetic_results == ("Waited 5000 ms", None)
    assert batch.output_count == 1


def test_analyzer_rejects_dynamic_or_mismatched_promise_delay() -> None:
    tail = 'const r = await tools.exec_command({cmd: "status"}); text(r.output);'

    assert (
        analyze_javascript_tools(
            "await new Promise(resolve => setTimeout(resolve, delay));" + tail
        )
        is None
    )
    assert (
        analyze_javascript_tools(
            "await new Promise(resolve => setTimeout(done, 5000));" + tail
        )
        is None
    )


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


def test_analyzer_unrolls_template_commands_and_mixed_template_results() -> None:
    batch = analyze_javascript_tools(
        r"""
        const paths = ["/tmp/one/SKILL.md", "/tmp/two/SKILL.md"];
        for (const path of paths) {
          const r = await tools.exec_command({
            cmd: `sed -n '1,260p' '${path}'`,
            workdir: "/workspace"
          });
          text(`FILE ${path}\n${r.output}`);
        }
        """
    )

    assert batch is not None
    assert [call.input["cmd"] for call in batch.calls] == [
        "sed -n '1,260p' '/tmp/one/SKILL.md'",
        "sed -n '1,260p' '/tmp/two/SKILL.md'",
    ]
    assert batch.result_indexes == [0, 1]
    assert batch.result_prefixes == (
        "FILE /tmp/one/SKILL.md\n",
        "FILE /tmp/two/SKILL.md\n",
    )


def test_analyzer_unrolls_destructured_rows_in_static_for_of_loop() -> None:
    batch = analyze_javascript_tools(
        r"""
        const ranges = [
          ["claude_code_log/cli.py", 570, 850],
          ["claude_code_log/models.py", 1, 240]
        ];
        for (const [f, a, b] of ranges) {
          const r = await tools.exec_command({
            cmd: `sed -n '${a},${b}p' '${f}'`,
            workdir: "/workspace"
          });
          text(`=== ${f} @ ${a} ===\n${r.output}`);
        }
        """
    )

    assert batch is not None
    assert [call.input["cmd"] for call in batch.calls] == [
        "sed -n '570,850p' 'claude_code_log/cli.py'",
        "sed -n '1,240p' 'claude_code_log/models.py'",
    ]
    assert batch.result_indexes == [0, 1]
    assert batch.result_prefixes == (
        "=== claude_code_log/cli.py @ 570 ===\n",
        "=== claude_code_log/models.py @ 1 ===\n",
    )


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


def test_analyzer_rejects_dynamic_tool_arguments_without_legacy_fallback() -> None:
    batch = analyze_javascript_tools(
        "const args = getArgs(); "
        "const result = await tools.exec_command(args); text(result);"
    )

    assert batch is None


def test_analyzer_supports_inline_awaited_tool_emission() -> None:
    batch = analyze_javascript_tools(
        'const patch = "*** Begin Patch\\n*** End Patch"; '
        "text(await tools.apply_patch(patch));"
    )

    assert batch is not None
    assert [(call.name, call.input) for call in batch.calls] == [
        ("apply_patch", {"patch": "*** Begin Patch\n*** End Patch"})
    ]


def test_analyzer_allows_multiple_emissions_for_one_direct_call() -> None:
    batch = analyze_javascript_tools(
        'const result = await tools.exec_command({cmd: "git status"}); '
        "text(result.output); text(`exit_code=${result.exit_code}`);"
    )

    assert batch is not None
    assert len(batch.calls) == 1
    assert batch.result_indexes == [0]


def test_analyzer_recovers_promise_all_marker_outputs() -> None:
    batch = analyze_javascript_tools(
        "const results = await Promise.all(["
        'tools.exec_command({cmd: "one"}), '
        'tools.exec_command({cmd: "two"})]); '
        "results.forEach((r,i)=>{text(`RESULT_${i+1}`);text(r.output)});"
    )

    assert batch is not None
    assert [call.input["cmd"] for call in batch.calls] == ["one", "two"]
    assert batch.result_indexes == [0, 1]
    assert batch.output_mode == "markers"


def test_analyzer_destructures_mixed_promise_all_results() -> None:
    batch = analyze_javascript_tools(
        """
        const [pr, auth, refs] = await Promise.all([
          tools.mcp__codex_apps__github_get_pr_info({
            repository_full_name: "daaain/claude-code-log", pr_number: 243
          }),
          tools.exec_command({cmd: "gh auth status"}),
          tools.exec_command({cmd: "git log --oneline"})
        ]);
        text(JSON.stringify(pr));
        text(auth.output);
        text(refs.output);
        """
    )

    assert batch is not None
    assert [call.name for call in batch.calls] == [
        "mcp__codex_apps__github_get_pr_info",
        "exec_command",
        "exec_command",
    ]
    assert batch.calls[0].input == {
        "repository_full_name": "daaain/claude-code-log",
        "pr_number": 243,
    }
    assert batch.result_indexes == [0, 1, 2]


def test_analyzer_recognizes_parallel_session_markers() -> None:
    batch = analyze_javascript_tools(
        """
        const results = await Promise.all([
          tools.exec_command({cmd: "pytest"}),
          tools.exec_command({cmd: "pyright"})
        ]);
        results.forEach((r, i) => {
          text(`RESULT_${i+1}`);
          text(r.output);
          if (r.session_id) text(`SESSION_ID=${r.session_id}`)
        });
        """
    )

    assert batch is not None
    assert batch.output_mode == "markers"
    assert batch.session_markers is True
    assert batch.result_indexes == [0, 1]


def test_analyzer_recognizes_single_session_marker() -> None:
    batch = analyze_javascript_tools(
        "const r = await tools.write_stdin({session_id: 73978}); "
        "text(r.output); if (r.session_id) text(`SESSION_ID=${r.session_id}`);"
    )

    assert batch is not None
    assert batch.session_markers is True
    assert batch.result_indexes == [0]
