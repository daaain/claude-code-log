"""Newly-decodable snippet classes under QuickJS execution (spec step 5).

The tree-sitter analyzer only recognized a whitelisted, statically-resolvable
subset: literal arguments, constant folding, simple template joins. Because
``codex_quickjs`` *executes* the snippet, the argument values it records are
whatever the snippet's own JavaScript computed — so string concatenation,
ternaries, template literals, array joins, ``reduce``/``repeat``, computed
numerics, and conditional loop bodies all decode for free.

Every snippet here is synthetic (no private transcript content); each asserts
the *materialized* tool input, which is the whole point — the value is correct
only if the engine actually ran the expression.
"""

from claude_code_log.providers.codex_quickjs import analyze_javascript_tools


def _one(source: str) -> dict[str, object]:
    batch = analyze_javascript_tools(source)
    assert batch is not None, "expected the snippet to decode"
    assert len(batch.calls) == 1
    return batch.calls[0].input


def test_concatenated_command_is_resolved() -> None:
    assert _one(
        'const dir = "/workspace";'
        'const r = await tools.exec_command({cmd: "ls " + dir + "/src"});'
        "text(r.output);"
    ) == {"cmd": "ls /workspace/src"}


def test_ternary_flag_selection_is_resolved() -> None:
    assert _one(
        "const verbose = true;"
        'const flags = verbose ? "-v" : "";'
        'const r = await tools.exec_command({cmd: ("git status " + flags).trim()});'
        "text(r.output);"
    ) == {"cmd": "git status -v"}


def test_template_literal_command_is_resolved() -> None:
    assert _one(
        'const branch = "main";'
        "const r = await tools.exec_command({cmd: `git log ${branch}`});"
        "text(r.output);"
    ) == {"cmd": "git log main"}


def test_array_join_command_is_resolved() -> None:
    assert _one(
        "const ids = [1, 2, 3];"
        'const r = await tools.exec_command({cmd: "process " + ids.join(",")});'
        "text(r.output);"
    ) == {"cmd": "process 1,2,3"}


def test_reduce_built_command_is_resolved() -> None:
    assert _one(
        'const parts = ["a", "b", "c"];'
        'const cmd = parts.reduce((acc, p) => acc + "/" + p);'
        "const r = await tools.exec_command({cmd});"
        "text(r.output);"
    ) == {"cmd": "a/b/c"}


def test_string_repeat_command_is_resolved() -> None:
    assert _one(
        'const bar = "=".repeat(5);'
        'const r = await tools.exec_command({cmd: "echo " + bar});'
        "text(r.output);"
    ) == {"cmd": "echo ====="}


def test_computed_numeric_argument_is_resolved() -> None:
    assert _one(
        "const n = 3;"
        'const r = await tools.exec_command({cmd: "head", lines: n * 10});'
        "text(r.output);"
    ) == {"cmd": "head", "lines": 30}


def test_loop_concatenated_commands_expand_to_a_batch() -> None:
    batch = analyze_javascript_tools(
        'const files = ["a.py", "b.py"];'
        "for (const f of files) {"
        '  const r = await tools.exec_command({cmd: "cat " + f});'
        "  text(r.output);"
        "}"
    )
    assert batch is not None
    assert [call.input for call in batch.calls] == [
        {"cmd": "cat a.py"},
        {"cmd": "cat b.py"},
    ]
    assert batch.result_indexes == [0, 1]


def test_conditional_loop_body_records_only_taken_branches() -> None:
    # A guard inside the loop means only the selected iterations make calls —
    # the recording reflects control flow the static analyzer could not follow.
    batch = analyze_javascript_tools(
        'const suites = ["unit", "tui", "browser"];'
        'const enabled = ["unit", "browser"];'
        "for (const s of suites) {"
        "  if (enabled.includes(s)) {"
        '    const r = await tools.exec_command({cmd: "just test-" + s});'
        "    text(r.output);"
        "  }"
        "}"
    )
    assert batch is not None
    assert [call.input["cmd"] for call in batch.calls] == [
        "just test-unit",
        "just test-browser",
    ]
    assert batch.result_indexes == [0, 1]


def test_all_tools_pipeline_still_fails_closed() -> None:
    # Boundary marker: iterating the ALL_TOOLS registry has no statically-known
    # membership (a real registry view is deferred future work), so a pipeline
    # driven off it materializes no calls and fails closed — documented here so
    # a future capability change is a deliberate, visible flip.
    assert (
        analyze_javascript_tools(
            "const names = ALL_TOOLS.filter((t) => t.name);"
            "for (const t of names) { text(t); }"
        )
        is None
    )
