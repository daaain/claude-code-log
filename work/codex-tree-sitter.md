# Codex Tree-sitter handover

> Handoff date: 2026-07-17
>
> Current branch: `dev/codex-tools`
>
> Pull request: #279
>
> HEAD before this document: `da8141b` (`Correlate deferred Codex tool outputs`)

## Purpose

Finish the current Codex tool-rendering branch without adding another parser to
it. After this branch is rebased and merged, start a fresh branch from `main`
to prototype replacing the growing JavaScript recognizer with a Tree-sitter
based static analyzer.

The prototype must analyze transcript JavaScript, never execute it. Its output
should feed the existing Codex adapter boundary so that provider reconstruction
and all downstream renderers remain unchanged.

## Why this is the next experiment

Codex stores some tool activity as a `custom_tool_call` named `exec`. Its input
is a short JavaScript orchestration program rather than one directly serialized
tool invocation. Examples now covered include:

```javascript
const a = await tools.exec_command({cmd: "codex plugin list --json"});
const b = await tools.exec_command({cmd: "find /tmp/plugin -type f"});
text(a.output);
text(b.output);
```

```javascript
const results = await Promise.all([
  tools.exec_command({cmd: "codex plugin --help"}),
  tools.exec_command({cmd: "codex plugin install --help"}),
]);
for (const r of results) text(r.output);
```

The current recognizer is deliberately conservative and works for these known
forms. It has nevertheless accumulated a lexical scanner, delimiter matching,
assignment recognition, output-expression recognition, and special cases for
`Promise.all`. Extending that with more regular expressions would amount to a
partial JavaScript parser with increasingly fragile syntax handling.

Tree-sitter offers a useful middle ground:

- parse JavaScript into a concrete syntax tree without evaluating it;
- preserve exact byte ranges for extracting original arguments;
- tolerate incomplete syntax and expose parse errors explicitly;
- analyze only a small, documented JavaScript subset;
- fall back losslessly to the existing `Workflow` rendering when provenance is
  ambiguous.

This is preferable to embedding or launching a JavaScript runtime. Transcript
content is untrusted, and even Node's `vm` API is not a security boundary.

## Current implementation boundary

The relevant module is `claude_code_log/providers/codex_tools.py`.

`adapt_codex_tool_call()` handles a single direct or wrapped invocation.
`adapt_codex_tool_batch()` recognizes a statically correlatable multi-call
program and returns:

```python
@dataclass(frozen=True)
class AdaptedToolBatch:
    calls: list[AdaptedToolCall]
    output_mode: Literal["markers", "ordered"]
    result_indexes: list[int]
```

`result_indexes[call_index]` identifies the corresponding result row in the
Codex transport output. This matters when the JavaScript emits results in a
different order from the calls.

The current scanner provides:

- string- and comment-aware discovery of static `tools.<name>(...)` calls;
- matching of nested parentheses, braces, and brackets;
- static JSON-compatible object-literal decoding;
- assignment binding for `const`, `let`, and `var` results;
- recognition of `text(result)`, `text(result.field)`,
  `JSON.stringify(result)`, and simple result-derived templates;
- exact correlation of sequential calls and their emissions;
- known `Promise.all` marker and ordered-loop forms;
- conservative fallback for unknown statements, dynamic calls, mismatched
  output counts, or unclear provenance.

Canonicalization after recognition should remain shared. It includes such
mappings as `exec_command` to `Bash`, `apply_patch` to `Edit`/`MultiEdit`, plan
updates to `TodoWrite`, and preservation of direct MCP names for plugin
transformers.

## Proposed architecture

Keep parsing, analysis, and canonicalization separate:

```text
JavaScript source
    -> Tree-sitter JavaScript syntax tree
    -> invocation/binding extraction
    -> symbolic provenance analysis
    -> output-emission analysis
    -> AdaptedToolBatch
    -> existing canonicalization and renderers
```

The analyzer should use a small intermediate representation rather than expose
Tree-sitter nodes to the provider. A useful conceptual form is:

```text
calls: [
  {
    call_index: 0,
    tool_name: "exec_command",
    tool_params: {"cmd": "..."},
    emissions: [
      {output_index: 1, result_path: ["output"]}
    ]
  }
]
```

The first prototype can translate this immediately into the existing
`AdaptedToolBatch`. Do not redesign the renderer-facing API until the AST
experiment demonstrates a missing capability.

## Supported static subset

Start narrowly and expand only with regression examples. Reasonable initial
support is:

- `const result = await tools.name({...})`;
- direct `tools.name({...})` elements inside a static `Promise.all([...])`;
- JSON-compatible object and array literals, including strings, numbers,
  booleans, and `null`;
- straight-line bindings and emissions;
- `text(result)`, `text(result.output)`, and longer static property paths;
- `text(JSON.stringify(result))`;
- template literals whose substitutions all trace to one result;
- known `for...of` or `forEach` output loops over a statically known result
  collection.

Use a tiny provenance lattice during analysis:

```text
Unknown
ToolResult(call_id)
ResultField(call_id, property_path)
Collection([provenance, ...])
```

Bindings copy provenance. Static member access appends a property. A `text()`
call records an emission only when its argument resolves unambiguously to one
tool result. Mutation, computed properties, unknown function calls, branches
with differing provenance, and dynamic iteration should yield `Unknown`.

## Fallback contract

False negatives are acceptable; false reconstructions are not. Preserve the
raw program as `Workflow` whenever any of these occur:

- the syntax tree contains relevant `ERROR` or missing nodes;
- a tool name or argument cannot be extracted statically;
- an argument is not in the supported literal subset;
- a result is reassigned or mutated;
- an emitted value could belong to more than one call;
- a call has no correlatable transport result;
- an output row is consumed inconsistently;
- unsupported control flow affects calls or emissions;
- extra executable statements remain after recognized statements are removed.

The fallback must retain the original JavaScript and all result rows. Never
silently discard an unsupported call or output.

## Dependency spike

Evaluate the Python packages `tree-sitter` and `tree-sitter-javascript` first.
Both expose Python bindings and prebuilt wheels on the main platforms, and the
JavaScript grammar package supports the project's Python 3.10 minimum.

Questions for the fresh branch:

1. Do wheels install cleanly for every supported Python and OS matrix entry?
2. Are node types and field names stable enough for the supported subset?
3. How does the parser report truncated snippets and recovery nodes?
4. What input-size cap avoids pathological parse cost while retaining real
   transcript snippets?
5. Does adding the native parser materially affect package size or startup?

Do not use Esprima as the default alternative without new evidence: the Python
port is old and its published language coverage predates modern JavaScript.

## Migration plan

1. Create a new branch from updated `main` only after #279 is merged.
2. Add a minimal parser wrapper and one dependency-install smoke test.
3. Implement AST-to-IR analysis behind the existing
   `adapt_codex_tool_batch()` boundary.
4. Run the AST analyzer first and temporarily retain the current recognizer as
   a compatibility fallback.
5. Port the accumulated examples into analyzer contract tests.
6. Add malformed, ambiguous, dynamic, and oversized inputs that must remain
   `Workflow`.
7. Compare rendered HTML for the real copied Codex session as well as unit
   results.
8. Remove the lexical recognizer only after AST parity and the full cross-
   platform suite are established.

Keep commits narrow: dependency/wrapper, IR and analysis, one syntax family at
a time, provider integration, then removal of the legacy scanner.

## Regression corpus to preserve

The current expectations live mainly in:

- `test/test_codex_tools.py`;
- `test/test_codex_adversarial.py`;
- provider expansion tests that pair adapted calls with transport result rows.

The Tree-sitter prototype must retain coverage for:

- a single static call with one or several result-derived `text()` emissions;
- adjacent sequential calls and calls declared first then emitted later;
- reversed emission order, with results still attached to invocation order;
- heterogeneous multi-tool programs, not only repeated `exec_command` calls;
- `Promise.all` marker output and ordered `for...of` output;
- the full two-call plugin-list/find example followed by
  `text(a.output); text(b.output);`;
- mismatched result counts;
- unrelated or literal `text()` output that forces `Workflow` fallback;
- tool-like source inside strings, comments, and template literal text;
- nested object/array literals and escaped strings;
- dynamic tool names, computed result access, reassignment, and ambiguous
  control flow.

Tests should assert both the reconstructed calls and exact `result_indexes`.
Renderer tests should remain responsible for the final visual presentation.

## Security and robustness

- Never execute transcript JavaScript.
- Do not invoke Node, a browser, or an embedded JS engine during export.
- Treat the parser's native library as an untrusted-input boundary: cap source
  size and add malformed-input tests.
- Avoid AST query patterns that accidentally accept recovered syntax without
  checking errors.
- Keep analysis deterministic and free of filesystem or network access.
- On every uncertainty, retain the raw `Workflow` representation.

If runtime emulation is reconsidered later, two prototype lessons still apply:
an awaited proxy must not expose a callable `.then`, and property access must
create immutable tagged result references rather than mutate a shared proxy.
Those details do not make execution safe and are not a recommendation to use
it.

## Current branch completion state

The latest local commit, `da8141b`, improves correlation for deferred and
reordered emissions. The last full local `just ci` completed successfully
before this documentation-only handoff. Do not push this branch without running
`just ci` again.

GitHub Actions run `29594805523` exposed one unresolved Windows-only failure in
`test_readable_image_paths_become_ordered_base64_content`: Python's platform
MIME lookup does not identify `.webp`, so the readable WebP fixture is omitted.
The likely narrow fix is a deterministic image-extension fallback (at least
`.webp` to `image/webp`) in `claude_code_log/providers/codex.py::_read_image`,
plus a regression that mocks `mimetypes.guess_type()` returning `None`. This is
not a Tree-sitter issue and is not fixed by this handoff commit.

The generated file
`session-019f4cc3-922e-7cc2-9975-cd529e8871af.html` is user-owned, untracked
evidence. Do not stage, modify, or delete it.

## Branch and merge sequence

PRs #242 and #243 correspond to the first commits in this branch's original
stack and are now on `main` as `bbdba6c` and `b4b254c`. Before rewriting this
branch:

1. fetch current `main`;
2. rebase `dev/codex-tools` onto it with `--update-refs`, allowing Git to drop
   patch-equivalent commits already supplied by #242 and #243;
3. resolve only genuine conflicts and rerun full `just ci`;
4. update PR #279 only when explicitly requested;
5. merge #279 after Windows CI and review are green;
6. create the Tree-sitter experiment as a new branch from the resulting
   `main`.

Do not add Tree-sitter dependencies or experimental analyzer code to
`dev/codex-tools`.
