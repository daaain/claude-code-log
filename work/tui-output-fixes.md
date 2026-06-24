# TUI / output fixes — #220, #221, #222, #223

Consolidated plan for the `--output` / `--format` / regeneration + TUI
tetralogy. External contributor (samestep) reports; all claims verified
against current code (line numbers below are current, not the v1.4.0
ones in the issues).

## Verified findings (state *before* this work — what each PR then fixes)

| Issue | Claim | Verdict | Current location |
|------|-------|---------|------------------|
| #221 | Existing `-o` file w/ matching version marker → write skipped, stale content kept, CLI still prints success | TRUE | skip `converter.py:1940-1962`; `is_outdated` only gate (no source identity); "Successfully converted" at `cli.py:1143` |
| #222 | `-o foo.md` w/o `-f` emits HTML | TRUE | `output_path_is_file` (`utils.py:338`) uses suffix for routing only; never feeds `output_format` (default `html`, `cli.py:636`) |
| #223-1 | `-o /dev/stdout` hangs | TRUE | `is_outdated` opens path read-only + `readline()`, **no `is_file()` guard** (`markdown/renderer.py:2292`, `html/renderer.py:208`, `json/renderer.py:216`); deadlocks on a pipe |
| #223-2 | progress/status pollutes stdout | TRUE | ~30 bare `print()` in `converter.py` + ~45 `click.echo()` w/o `err=True` in `cli.py` |
| #220 | `--tui` ignores `-o`/`-f`, no export-to-path | TRUE | `run_session_browser` (`tui.py:2136`) never gets them; `_ensure_session_file` (`tui.py:1827`) hardcodes `project_path/session-{id}.{ext}` |

### Refinement to the shared-root-cause hypothesis
"Bypass the regeneration-skip for explicit `-o` → fixes #221 AND the
#223 hang" is only partly right:
- #221 (correctness): force-regenerate on explicit `-o` is the right fix.
- #223 hang: bypass alone is NOT reliable — a *second* `is_outdated`
  call exists in directory mode (`converter.py:1805`), and even the
  single-file path only avoids it if the force-term short-circuits ahead
  of `is_outdated` in the `or`. **The robust hang fix is a `Path.is_file()`
  guard inside `is_outdated`** (char device like `/dev/stdout` isn't a
  regular file → "outdated" without opening). Issue called this
  "hardening"; it's actually the primary fix.

### Extra findings (not in the issues)
- `-o -` is misrouted: `Path("-").suffix == ""` → `output_path_is_file`
  treats `-` as a **directory**. `-o -` support needs explicit dash handling.
- `generate_single_session_file` (`--session-id`, `converter.py:2444`)
  **already always overwrites** — only `convert_jsonl_to`'s single-file
  branch has the stale bug. The #221 fix aligns the two.

## Plan — 3 PRs (seam-aligned)

### PR 1 — "Explicit `-o` correctness" (#221 + #222 + #223-part1)
Cohesive concern: make explicit `--output` behave correctly. Seam =
regeneration/`is_outdated` machinery + CLI `-o`/`-f` resolution.
- `Path.is_file()` guard in all three `is_outdated` / `check_html_version` → fixes hang at every call site (#223-1).
- `force_regenerate` param on `convert_jsonl_to`, set when `output is not None`, **first** in the `should_regenerate` `or` → fixes stale-skip (#221).
- Infer `output_format` from `-o` suffix when `-f` source is `DEFAULT` (Click `ctx.get_parameter_source`); error on explicit conflict (`-o foo.md -f html`) (#222).
- Separate commits per issue for clean `Closes #`. Smallest, highest-value, no deps → ship first.

### PR 2 — "stdout streaming + stderr hygiene" (#223-part2 + `-o -`) — IMPLEMENTED (branch dev/output-stdout-stream, awaiting #237 merge → rebase → PR)
Stacks on PR 1.
- `-o -` and `-o /dev/stdout` → stream-to-stdout. Implemented via a
  throwaway temp file (`_render_to_stdout`): render with `use_cache=False`
  (→ cache_manager None → no pagination, single document), `force_regenerate`,
  `generate_individual_sessions=False`, embedded images (temp dir discarded),
  then copy the file's bytes to `sys.stdout`. Reuses the whole pipeline
  verbatim — no risk of divergence. Supported for the main convert path AND
  `--session-id`; `--all-projects` + `-o -` is a clear UsageError.
- Status hygiene (behavior-preserving, no ~75-site sweep): in stream mode the
  converter runs `silent=True` (no progress on stdout) and the CLI prints its
  one-line confirmation to **stderr**. Every non-stream invocation keeps status
  on stdout exactly as today — so no PowerShell red-text concern for normal runs.
  stdout therefore carries only the rendered document.
- `--output` help updated to document `-`.

### PR 3 — "TUI honors `-o`/`-f`" (#220) — minimal only
Isolated (no shared code w/ PR 1/2).
- Warn that `-o`/`-f` are no-ops under `--tui`, mirroring `cli.py:825-843`. ~3 lines.
- Export-to-path action DEFERRED — decide with daaain after minimal lands + CR.

### Ordering / deps
PR 1 first (foundational). PR 2 stacks on PR 1. PR 3 independent, any time.

## Process
Single implementer (no delegation). Each PR: implement → tests →
`just ci` (fresh venv) → review → PR vs main → merge. PRs land
progressively (PR 1 to main first, then PR 2 branched off updated main)
to avoid base≠main CI/review gating.
