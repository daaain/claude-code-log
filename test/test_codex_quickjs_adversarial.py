"""Adversarial / engine-class tests for the sandboxed QuickJS analyzer.

The tree-sitter analyzer never executed anything; ``codex_quickjs`` runs
untrusted transcript JavaScript in a sandboxed engine, so its threat surface
is different: runaway loops, allocation bombs, deep recursion, host escapes,
and — because provenance rides on an in-band sentinel — sentinel *forgery*.

Contract pinned here (per the design memo): the worst case for any hostile
snippet is *fail closed to None* so the raw-script ``ToolExecution`` fallback
stays visible. Never a crash, never a hang, and never fabricated display text
(a forged sentinel can at most re-route real output rows, never inject text —
displayed content is always sourced from the real tool output downstream).
"""

from pytest import MonkeyPatch

from claude_code_log.providers import codex_quickjs
from claude_code_log.providers.codex_quickjs import analyze_javascript_tools

# The private-use provenance delimiter (U+E000); a hostile snippet can emit it
# verbatim, so every forged construction below splices the *real* char. Bound
# from the module (not a literal) so a change to the sentinel can't silently
# render these forgery guards vacuous.
_S: str = getattr(codex_quickjs, "_S")
_R0 = f"{_S}R0{_S}"


# --------------------------------------------------------------------------
# Engine bounds — no hang, no crash, no host process impact.
# --------------------------------------------------------------------------
def test_infinite_loop_is_bounded_and_fails_closed() -> None:
    # Bounded by the engine time limit (~2s), not a Python-level hang.
    assert (
        analyze_javascript_tools(
            "while (true) {} const r = await tools.a({x: 1}); text(r);"
        )
        is None
    )


def test_allocation_bomb_fails_closed() -> None:
    assert (
        analyze_javascript_tools(
            "const a = []; while (true) { a.push(new Array(100000).fill(7)); } "
            "text('x');"
        )
        is None
    )


def test_unbounded_recursion_fails_closed_without_python_recursionerror() -> None:
    # A host RecursionError would escape as a crash; the engine stack limit
    # must contain it and the top-level guard maps it to None.
    assert (
        analyze_javascript_tools("function f(n) { return f(n + 1); } f(0); text('x');")
        is None
    )


# --------------------------------------------------------------------------
# No host escape — the sandbox exposes no Node/host globals.
# --------------------------------------------------------------------------
def test_host_globals_are_absent() -> None:
    batch = analyze_javascript_tools(
        "const r = await tools.probe({"
        "  proc: typeof process,"
        "  req: typeof require,"
        "  fetch: typeof fetch,"
        "  glob: typeof globalThis.process"
        "}); text(r);"
    )
    assert batch is not None
    assert batch.calls[0].input == {
        "proc": "undefined",
        "req": "undefined",
        "fetch": "undefined",
        "glob": "undefined",
    }


def test_snippet_using_a_host_global_fails_closed() -> None:
    assert (
        analyze_javascript_tools(
            "const data = require('fs').readFileSync('/etc/passwd');"
            "const r = await tools.a({data}); text(r);"
        )
        is None
    )


# --------------------------------------------------------------------------
# Sandbox isolation — one snippet's mutations never leak into the next.
# --------------------------------------------------------------------------
def test_global_pollution_does_not_leak_across_snippets() -> None:
    analyze_javascript_tools(
        'globalThis.__LEAK = "polluted"; const r = await tools.a({x: 1}); text(r);'
    )
    batch = analyze_javascript_tools(
        "const r = await tools.probe({leak: typeof globalThis.__LEAK}); text(r);"
    )
    assert batch is not None
    assert batch.calls[0].input == {"leak": "undefined"}


# --------------------------------------------------------------------------
# Sentinel forgery — a hostile snippet emitting the provenance char itself.
# --------------------------------------------------------------------------
def test_single_call_forged_reference_maps_whole_output() -> None:
    # One real call whose emission forges a ref to call index 9. The forged ref
    # makes correlation fail, but with a SINGLE call there is nothing to
    # mis-attribute — the whole paired output is that call's result — so it maps
    # as the fallback. (The MULTI-call forged-ref case, where a forged ref could
    # re-route a real output row, still fails closed — see
    # test_codex_quickjs::test_multi_call_forged_reference_fails_closed.)
    batch = analyze_javascript_tools(
        f'const r = await tools.a({{x: 1}}); text("{_S}R9{_S}");'
    )
    assert batch is not None
    assert len(batch.calls) == 1 and batch.whole_output_fallback is True


def test_forged_sentinel_in_a_tool_argument_fails_closed() -> None:
    # An arg may only legitimately hold a sentinel via an inter-call data
    # dependency (itself rejected); a forged literal one must fail closed so it
    # is never used as a real argument value.
    assert (
        analyze_javascript_tools(
            f'await tools.a({{cmd: "{_R0}"}}); await tools.b({{y: 1}}); text("z");'
        )
        is None
    )


def test_literal_provenance_char_in_source_string_fails_closed() -> None:
    # U+E000 cannot appear in legit JSON-embedded source but a hostile author
    # can splice it; treated as a forged arg sentinel → fail closed.
    assert (
        analyze_javascript_tools(
            f'const r = await tools.a({{cmd: "run{_R0}now"}}); text(r);'
        )
        is None
    )


def test_forged_sentinel_via_hostile_tojson_does_not_crash() -> None:
    # A crafted toJSON returns the sentinel string during serialization. Worst
    # case is a re-routed/duplicated ref → still no crash and no host escape.
    result = analyze_javascript_tools(
        "const r = await tools.a({x: 1}); await tools.b({y: 2}); "
        f'text(JSON.stringify({{o: {{toJSON() {{ return "{_R0}"; }}}}}}));'
    )
    # Whatever it decides, it must never surface the raw sentinel in an input.
    if result is not None:
        assert not any(
            _S in str(v) for call in result.calls for v in call.input.values()
        )


def test_hostile_getter_throwing_fails_closed() -> None:
    assert (
        analyze_javascript_tools(
            'const o = { get output() { throw new Error("boom"); } };'
            "const r = await tools.a({x: 1}); text(o.output);"
        )
        is None
    )


# --------------------------------------------------------------------------
# Non-fabrication — sentinels route real output, they are never displayed.
# --------------------------------------------------------------------------
def test_no_recorded_input_ever_contains_the_raw_sentinel() -> None:
    # Across a spread of forged/benign snippets, no materialized call input may
    # carry the provenance char: it is stripped at emission or rejected.
    snippets = [
        'const r = await tools.a({cmd: "real"}); text(r.output);',
        f'const r = await tools.a({{cmd: "real"}}); text("{_R0}");',
        'const a = await tools.a({cmd: "one"}); '
        'const b = await tools.b({cmd: "two"}); text(a.output); text(b.output);',
    ]
    for source in snippets:
        batch = analyze_javascript_tools(source)
        if batch is None:
            continue
        assert not any(
            _S in str(value) for call in batch.calls for value in call.input.values()
        )


def test_run_snippet_is_the_fail_closed_boundary(monkeypatch: MonkeyPatch) -> None:
    # Belt-and-suspenders: even a bug that lets a raw report through is bounded
    # by the top-level guard. Force _build_batch to explode.
    def boom(_report: object) -> None:
        raise RuntimeError("synthetic mapper failure")

    monkeypatch.setattr(codex_quickjs, "_build_batch", boom)
    assert analyze_javascript_tools("const r = await tools.a({x: 1}); text(r);") is None


# --------------------------------------------------------------------------
# Correlation / consistency guards — pin the fail-closed legs that hold even
# when a snippet's recording is internally self-consistent (a forged wait ref,
# a throw after clean emissions, or a run that never resolves). Each of these
# would materialize a WRONG batch if its guard were neutralized.
# --------------------------------------------------------------------------
def test_forged_reference_to_a_wait_record_fails_closed() -> None:
    # A setTimeout registers a synthetic "wait" record at index 0; a forged
    # emission references it. Without the is-wait leg of the bounds guard this
    # cross-attributes the wait to an output row and builds a bogus batch.
    assert (
        analyze_javascript_tools(
            "setTimeout(() => {}, 100);"
            "const r = await tools.a({x: 1});"
            f'text(r.output); text("{_R0}");'
        )
        is None
    )


def test_throw_after_consistent_emissions_fails_closed() -> None:
    # Records and texts are internally consistent, but the run throws before
    # completing. The errors leg of the done/errors guard must still reject it.
    assert (
        analyze_javascript_tools(
            'const r = await tools.a({x: 1}); text(r.output);throw new Error("boom");'
        )
        is None
    )


def test_never_resolving_run_fails_closed() -> None:
    # A run that never settles leaves __done false (no hang: the pending-job
    # pump drains and stops). The done leg of the guard must reject it.
    assert (
        analyze_javascript_tools(
            "const r = await tools.a({x: 1}); text(r.output);"
            "await new Promise(() => {});"
        )
        is None
    )


# --------------------------------------------------------------------------
# Static caps — source-byte and expanded-call bounds (spec: keep these red).
# --------------------------------------------------------------------------
def test_oversized_source_fails_closed() -> None:
    # Source larger than MAX_SOURCE_BYTES (64 KB) is rejected before execution.
    padding = "x" * (65 * 1024)
    source = (
        f'const pad = "{padding}"; const r = await tools.a({{x: 1}}); text(r.output);'
    )
    assert len(source.encode("utf-8")) > 64 * 1024
    assert analyze_javascript_tools(source) is None


def test_batch_exceeding_expanded_call_cap_fails_closed() -> None:
    # A fully-static 129-iteration loop materializes 129 real calls, over the
    # 128 MAX_EXPANDED_CALLS bound → fail closed (pins the >128 contract; the
    # records-level and param-level caps are redundant defense in depth).
    ids = ", ".join(str(i) for i in range(129))
    source = (
        f"for (const i of [{ids}]) {{"
        "  const r = await tools.a({i}); text(r.output);"
        "}"
    )
    assert analyze_javascript_tools(source) is None
