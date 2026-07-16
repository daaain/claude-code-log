---
name: update-tools-coverage
description: Refresh dev-docs/tools-coverage.md against the upstream Claude Code tools reference. Use when checkpointing tool-renderer coverage, after adding/removing a tool renderer, or when the upstream tools list may have changed.
---

# Update Tool Coverage

Keeps [`dev-docs/tools-coverage.md`](../../../dev-docs/tools-coverage.md) in
sync with two sources of truth:

1. **Upstream** — the documented tool list at
   <https://code.claude.com/docs/en/tools-reference>.
2. **Us** — `TOOL_INPUT_MODELS` / `TOOL_OUTPUT_PARSERS` in
   [`claude_code_log/factories/tool_factory.py`](../../../claude_code_log/factories/tool_factory.py).

The doc grades each documented tool **Full** (typed input + typed output),
**Input only** (typed input, generic output), or **Generic** (no registry
entry → params-table + raw-`<pre>` fallback), and separately lists tools we
support that upstream no longer documents (renames like `Task`→`Agent`,
supersessions like `MultiEdit`, legacy aliases, undocumented features).

## Procedure

1. **Fetch the upstream tool names.** WebFetch the reference and ask only
   for the tool-table names (the page is ~80 KB; a targeted prompt keeps it
   manageable):

   > WebFetch `https://code.claude.com/docs/en/tools-reference` —
   > "List the exact tool names in the main tools table, one per line.
   > Names only, no descriptions."

   Note any tool the page marks deprecated/renamed — that's a candidate for
   the second table.

2. **Compute the truth mechanically.** Feed those names to the bundled
   helper (run from the repo root; `uv run` is required so `pydantic`
   resolves — bare `python3` fails with `ModuleNotFoundError: pydantic`):

   ```bash
   printf 'Agent Artifact AskUserQuestion ... Write' \
     | uv run python .claude/skills/update-tools-coverage/check_coverage.py
   ```

   It prints each tool's support level, the totals, the "we register but
   upstream doesn't document" set, and — if the doc exists — a **drift
   report** (missing / mismatched / stale rows). If it says `in sync [OK]` and
   the upstream name set is unchanged, there is nothing to do.

3. **Reconcile the doc.** Apply the helper's output to
   `dev-docs/tools-coverage.md`:
   - Move rows between the two tables as tools enter/leave the upstream list —
     **never delete** a row for a tool we still register. A tool leaving the
     reference moves to the "no longer documented" table (it's history a
     transcript viewer must still render); it does not lose support.
   - Fix any support-level the drift report flags.
   - Update the **totals line** and the **snapshot date** in the intro
     (`snapshot YYYY-MM-DD`).
   - The **Notes column is hand-maintained** — carry notes forward; only the
     support level is machine-derived.

4. **Re-run the helper** to confirm `in sync [OK]`.

## Guardrails

- **Generic is a feature, not a gap.** Unknown / `mcp__*` / plugin tools
  *should* fall back to generic rendering. Only type a tool when it's common
  or carries structure worth surfacing — see
  [`implementing-a-tool-renderer.md`](../../../dev-docs/implementing-a-tool-renderer.md)
  (or the `tool-renderer` skill) to actually add one.
- **Undocumented ≠ obsolete.** The reference table isn't a full census of
  what lands in a JSONL file (e.g. `TeamCreate`/`TeamDelete` are typed both
  sides but never appeared upstream). Keep those under "no longer /never
  documented" with a note, not removed.
- **Dead models.** `GlobOutput` / `GrepOutput` exist in `models.py` but no
  parser constructs them, so `Glob`/`Grep` are **Input only**. If a future
  change wires up a parser, they graduate to Full automatically — the helper
  will show the flip.
