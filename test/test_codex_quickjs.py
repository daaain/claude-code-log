"""Sandboxed QuickJS execution of bounded Codex JavaScript tool wrappers.

These are the spec-by-example cases for ``codex_quickjs`` (which replaced the
tree-sitter ``codex_javascript`` analyzer): each snippet is *executed* in a
sandboxed engine and the instrumented recording is mapped back to a tool batch.
"""

from pytest import MonkeyPatch

from claude_code_log.providers import codex_quickjs
from claude_code_log.providers.codex_quickjs import analyze_javascript_tools


def test_analyzer_fails_closed_on_unexpected_engine_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """Any unexpected engine/mapper exception fails closed to None.

    Analog of the old parser-error guard: the tree-sitter ``parse_javascript``
    hook is gone; the fail-closed boundary is now the engine run. Force it to
    raise and confirm the raw-script fallback still stays visible.
    """

    def fail_run(_source: str) -> None:
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(codex_quickjs, "_run_snippet", fail_run)

    assert analyze_javascript_tools("const valid = true;") is None


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


def test_analyzer_joins_static_string_array_in_tool_input() -> None:
    batch = analyze_javascript_tools(
        r"""
        const r = await tools.mcp__clmail__communicate({
          action: "send",
          actor: "/workspace/codex",
          params: {
            to: "/workspace/clmail/main",
            subject: "Lifecycle verified",
            body: [
              "Automatic delivery is confirmed.",
              "",
              "Official Codex hook contract:",
              "Conclusion: the plugin is viable."
            ].join("\n")
          }
        });
        text(JSON.stringify(r));
        """
    )

    assert batch is not None
    assert batch.calls[0].input["params"]["body"] == (
        "Automatic delivery is confirmed.\n\n"
        "Official Codex hook contract:\n"
        "Conclusion: the plugin is viable."
    )


def test_analyzer_joins_constant_string_array_variable() -> None:
    batch = analyze_javascript_tools(
        r"""
        const lines = ["first", "second"];
        const r = await tools.exec_command({cmd: lines.join(" | ")});
        text(r.output);
        """
    )

    assert batch is not None
    assert batch.calls[0].input == {"cmd": "first | second"}


def test_analyzer_recursively_joins_arrays_and_template_strings() -> None:
    batch = analyze_javascript_tools(
        r"""
        const a = "resolved";
        const body = [
          "a static string",
          `${a} template string`,
          `a template with joined string arrays: ${["a", "b", "c"].join("\n")}`,
          "should work"
        ].join("\n");
        const r = await tools.mcp__clmail__communicate({
          action: "send",
          actor: "/workspace/codex",
          params: {to: "/workspace/clmail/main", body}
        });
        text(JSON.stringify(r));
        """
    )

    assert batch is not None
    assert batch.calls[0].input["params"]["body"] == (
        "a static string\n"
        "resolved template string\n"
        "a template with joined string arrays: a\nb\nc\n"
        "should work"
    )


def test_analyzer_rejects_dynamic_or_sparse_array_join() -> None:
    tail = "; const r = await tools.exec_command({cmd: body}); text(r.output);"

    assert (
        analyze_javascript_tools('const body = ["a", value].join("\n")' + tail) is None
    )
    assert (
        analyze_javascript_tools('const body = ["a",, "b"].join("\n")' + tail) is None
    )
    assert (
        analyze_javascript_tools('const body = ["a", "b"].join(separator)' + tail)
        is None
    )


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


def test_analyzer_rejects_dynamic_loops_without_partial_results() -> None:
    """A loop over a runtime-only iterable (undefined ``ids``) throws in the
    engine and fails closed — its calls never materialize statically."""
    dynamic = """
        for (const id of ids) {
          const r = await tools.exec_command({cmd: id});
          text(r.output);
        }
    """

    assert analyze_javascript_tools(dynamic) is None


def test_analyzer_expands_static_loop_beyond_legacy_iteration_cap() -> None:
    """Sanctioned capability gain over the tree-sitter analyzer: a *static*
    loop that exceeds the old ``max_loop_iterations`` cap now CAPTURES.

    Under execution, loop bounds are enforced by the engine's time/memory
    limits rather than a fixed iteration count, so a fully-static 3-iteration
    loop materializes all three calls even when ``max_loop_iterations=2`` — a
    value the old parser would have rejected. Runaway loops are still bounded
    (they hit the time limit and fail closed); only statically-terminating
    loops are expanded.
    """
    oversized = """
        for (const id of [1, 2, 3]) {
          const r = await tools.exec_command({cmd: "echo", id});
          text(r.output);
        }
    """

    batch = analyze_javascript_tools(oversized, max_loop_iterations=2)

    assert batch is not None
    assert [call.input["id"] for call in batch.calls] == [1, 2, 3]
    assert batch.result_indexes == [0, 1, 2]


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


def test_analyzer_expands_static_array_map_with_result_spread() -> None:
    batch = analyze_javascript_tools(
        """
        const cmds = [
          ["pyright", "uv run pyright"],
          ["unit", "uv run pytest -m 'not (tui or browser)' -q"]
        ];
        const results = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({
            cmd,
            workdir: "/workspace",
            yield_time_ms: 30000,
            max_output_tokens: 30000
          });
          return {name, ...r};
        }));
        for (const r of results) text(JSON.stringify(r));
        """
    )

    assert batch is not None
    assert [call.name for call in batch.calls] == ["exec_command", "exec_command"]
    assert [call.input["cmd"] for call in batch.calls] == [
        "uv run pyright",
        "uv run pytest -m 'not (tui or browser)' -q",
    ]
    assert batch.result_indexes == [0, 1]
    assert batch.result_object_keys == ("output", "output")


def test_analyzer_expands_static_array_map_with_explicit_result_fields() -> None:
    batch = analyze_javascript_tools(
        """
        const cmds = [
          ["guide", "rg --files -g 'AGENTS.md' -g '!node_modules'"],
          ["status", "git status --short --branch"],
          ["head", "git log -1 --oneline"]
        ];
        const out = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({
            cmd,
            workdir: "/workspace/project",
            yield_time_ms: 10000,
            max_output_tokens: 12000
          });
          return {name, exit_code: r.exit_code, output: r.output};
        }));
        out.forEach(text);
        """
    )

    assert batch is not None
    assert [call.name for call in batch.calls] == ["exec_command"] * 3
    assert [call.input["cmd"] for call in batch.calls] == [
        "rg --files -g 'AGENTS.md' -g '!node_modules'",
        "git status --short --branch",
        "git log -1 --oneline",
    ]
    assert batch.result_indexes == [0, 1, 2]
    assert batch.result_object_keys == ("output", "output", "output")
    assert batch.result_prefixes == (
        '{"name":"guide"',
        '{"name":"status"',
        '{"name":"head"',
    )
    assert batch.output_count == 3


def test_single_call_aliased_projection_maps_whole_output() -> None:
    # ``cmds`` has ONE entry, so this is a single exec call. The aliased
    # projection (``output: r.exit_code``) can't correlate cleanly, but with one
    # call there is nothing to mis-attribute — the whole output is its result, so
    # it maps as the fallback rather than staying the raw ToolExecution.
    source = """
        const cmds = [["status", "git status --short"]];
        const out = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({cmd});
          return {name, output: r.exit_code};
        }));
        out.forEach(text);
    """

    batch = analyze_javascript_tools(source)
    assert batch is not None
    assert len(batch.calls) == 1 and batch.whole_output_fallback is True


def test_single_call_unrecognized_collection_callback_maps_whole_output() -> None:
    # Same single-call shape emitted via ``console.log``; an unrecognized
    # collection callback no longer blocks a lone call from mapping.
    source = """
        const cmds = [["status", "git status --short"]];
        const out = await Promise.all(cmds.map(async ([name, cmd]) => {
          const r = await tools.exec_command({cmd});
          return {name, output: r.output};
        }));
        out.forEach(console.log);
    """

    batch = analyze_javascript_tools(source)
    assert batch is not None
    assert len(batch.calls) == 1 and batch.whole_output_fallback is True


def test_analyzer_projects_sequential_calls_from_one_result_object() -> None:
    batch = analyze_javascript_tools(
        """
        const actor = "/workspace/codex";
        const p = await tools.mcp__clmail__actors({
          action: "presence", actor, params: {status: "all"}
        });
        const m = await tools.mcp__clmail__communicate({
          action: "list", actor, params: {status: "unread"}
        });
        text(JSON.stringify({presence: p, mail: m}));
        """
    )

    assert batch is not None
    assert [call.name for call in batch.calls] == [
        "mcp__clmail__actors",
        "mcp__clmail__communicate",
    ]
    assert batch.calls[0].input == {
        "action": "presence",
        "actor": "/workspace/codex",
        "params": {"status": "all"},
    }
    assert batch.calls[1].input == {
        "action": "list",
        "actor": "/workspace/codex",
        "params": {"status": "unread"},
    }
    assert batch.result_indexes == [0, 0]
    assert batch.result_object_keys == ("presence", "mail")
    assert batch.output_count == 1


def test_analyzer_projects_result_object_shorthand_properties() -> None:
    batch = analyze_javascript_tools(
        """
        const [hooks, plugins, marketplace] = await Promise.all([
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "hooks"}),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "plugins"}),
          tools.mcp__openaiDeveloperDocs__fetch_openai_doc({url: "marketplace"})
        ]);
        text(JSON.stringify({hooks, plugins, marketplace}));
        """
    )

    assert batch is not None
    assert [call.input["url"] for call in batch.calls] == [
        "hooks",
        "plugins",
        "marketplace",
    ]
    assert batch.result_indexes == [0, 0, 0]
    assert batch.result_object_keys == ("hooks", "plugins", "marketplace")
    assert batch.output_count == 1


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


# --------------------------------------------------------------------------
# Provenance-laundering relaxations: a snippet that READS a call result but
# transforms the sentinel away before text() (JSON.parse throwing on it,
# indexOf missing it) leaves the output row carrying no provenance. Two
# unambiguous shapes are recovered; everything else stays fail-closed. Fixtures
# are synthetic (no private content).
# --------------------------------------------------------------------------
def test_single_call_json_parse_launder_is_recovered() -> None:
    """JSON.parse(r.output) throws on the sentinel → catch → text('[]'). One
    call, one read output row → the whole paired output is that call's result."""
    batch = analyze_javascript_tools(
        "const r = await tools.mcp__x__list({});"
        " let d; try { d = JSON.parse(r.output); } catch (e) { d = []; }"
        " text(JSON.stringify(d));"
    )
    assert batch is not None
    assert [c.name for c in batch.calls] == ["mcp__x__list"]
    assert batch.result_indexes == [0]
    assert batch.output_count == 1


def test_single_call_indexof_launder_is_recovered() -> None:
    """indexOf on the sentinel string is false → text('(not found)')."""
    batch = analyze_javascript_tools(
        'const r = await tools.mcp__x__search({q: "hi"});'
        " const raw = String(r.output);"
        ' text(raw.indexOf("needle") >= 0 ? raw : "(not found)");'
    )
    assert batch is not None
    assert batch.result_indexes == [0]
    assert batch.output_count == 1


def test_single_call_maps_whole_output_regardless_of_emission() -> None:
    """A single (non-wait) tool call has no output-split ambiguity by
    construction — the one paired output is its result — so it maps as the
    whole-output FALLBACK no matter what the snippet emitted: an unread literal,
    a header + laundered result (multi-emission), or nothing at all."""
    for source in (
        'await tools.exec_command({cmd: "git status"}); text("result");',  # unread
        'const r = await tools.mcp__x__list({}); text("Results:");'  # multi-emission
        " let d; try { d = JSON.parse(r.output); } catch (e) { d = []; }"
        " text(String(d.length));",
        'await tools.exec_command({cmd: "ls"});',  # zero-emission
    ):
        batch = analyze_javascript_tools(source)
        assert batch is not None, source
        assert len(batch.calls) == 1
        assert batch.result_indexes == [0]
        assert batch.whole_output_fallback is True


def test_interleaving_laundered_loop_is_recovered() -> None:
    """A for-of loop that launders each iteration's result (JSON.parse-catch per
    call) emits one laundered row per call, in order — attributed positionally by
    the recorded calls-made-so-far."""
    batch = analyze_javascript_tools(
        'for (const id of ["a", "b", "c"]) {'
        "  const r = await tools.mcp__x__get({id});"
        "  let d; try { d = JSON.parse(r.output); } catch (e) { d = null; }"
        '  text(d && d.name ? d.name : "(none)");'
        "}"
    )
    assert batch is not None
    assert [c.name for c in batch.calls] == ["mcp__x__get"] * 3
    assert batch.result_indexes == [0, 1, 2]
    assert batch.output_count == 3


def test_interleaving_count_mismatch_fails_closed() -> None:
    """Three calls (all read) but only two laundered rows → the row↔call
    bijection is broken, so it fails closed rather than guess."""
    assert (
        analyze_javascript_tools(
            'const r0 = await tools.get({id: "a"});'
            ' const r1 = await tools.get({id: "b"});'
            ' const r2 = await tools.get({id: "c"});'
            " let a; try { a = JSON.parse(r0.output); } catch (e) { a = 0; }"
            " let b; try { b = JSON.parse(r1.output); } catch (e) { b = 0; }"
            " let c; try { c = JSON.parse(r2.output); } catch (e) { c = 0; }"
            " text(String(a)); text(String(b));"
        )
        is None
    )


def test_interleaving_out_of_order_fails_closed() -> None:
    """Two calls but the laundered rows are emitted in reverse order → the
    positional (monotone ``after``) check fails, so it stays fail-closed."""
    assert (
        analyze_javascript_tools(
            'const r0 = await tools.get({id: "a"});'
            ' const r1 = await tools.get({id: "b"});'
            ' const s1 = String(r1.output); text(s1.indexOf("x") >= 0 ? s1 : "(a)");'
            ' const s0 = String(r0.output); text(s0.indexOf("x") >= 0 ? s0 : "(b)");'
        )
        is None
    )


def test_interleaved_mixed_provenance_is_recovered() -> None:
    """A multi-call sequence that keeps the sentinel on one row and launders the
    next is attributed by position: the sentinel row self-identifies its call
    AND agrees with its execution slot, the laundered row is placed by slot."""
    batch = analyze_javascript_tools(
        "const r0 = await tools.getA({});"
        " text(r0.output);"
        " const r1 = await tools.getB({});"
        " let d; try { d = JSON.parse(r1.output); } catch (e) { d = null; }"
        ' text(d ? d.n : "(none)");'
    )
    assert batch is not None
    assert [c.name for c in batch.calls] == ["getA", "getB"]
    assert batch.result_indexes == [0, 1]
    assert batch.output_count == 2


def test_multi_call_with_an_unread_call_fails_closed() -> None:
    """A multi-call loop that emits a STATIC literal each iteration (never
    reading the call result) leaves every call unread — nothing attributable —
    so it fails closed rather than pair unrelated literals to calls."""
    assert (
        analyze_javascript_tools(
            'for (const id of ["a", "b", "c"]) {'
            "  await tools.get({id});"
            '  text("static");'
            "}"
        )
        is None
    )


def test_multi_call_forged_reference_fails_closed() -> None:
    """Load-bearing: with two real calls, an emission forging a ref to a
    nonexistent call index must fail closed (a forged ref could otherwise
    re-route a real output row). Only single-call snippets — where there is
    nothing to mis-attribute — recover unconditionally."""
    from claude_code_log.providers.codex_quickjs import _S

    assert (
        analyze_javascript_tools(
            "const r0 = await tools.a({x: 1});"
            " const r1 = await tools.b({y: 2});"
            f' text(r0.output); text("{_S}R9{_S}");'
        )
        is None
    )
