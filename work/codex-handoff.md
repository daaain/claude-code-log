# Codex provider handoff

> Handoff date: 2026-07-14  
> Worktree: `/home/cboos/Workspace/github/daain/claude-code-log/codex`  
> Branch: `dev/codex-tools`  
> HEAD at handoff: `6e5989f` (`feat: expand Codex user shell commands`)

## Mission

Continue the Codex provider/tool work through the QA and hardening round. The
provider itself was developed on `dev/codex`; `dev/codex-tools` adds
conservative adaptation of Codex calls to the existing Claude-oriented typed
tools and renderers.

The next session should treat `work/codex-qa-review.md` as the prioritized QA
backlog. It was untracked at handoff, so confirm its ownership/state before
staging it.

## Worktree ownership and commit discipline

At handoff, `git status --short --branch` showed only:

```text
## dev/codex-tools
?? session-019f4cc3-922e-7cc2-9975-cd529e8871af.html
?? work/codex-qa-review.md
```

- The generated session HTML is user-owned evidence. Do not edit, delete, or
  commit it.
- The QA review was produced separately and is the next-round specification.
  Do not silently claim or rewrite it; coordinate with the user if its commit
  status is unclear.
- Preserve unrelated changes from the user or other actors.
- The user requested narrow commits: commit every independent fix separately
  and stage only the files/hunks belonging to that fix.
- New auxiliary worktrees, if needed, must use the `codex-` prefix. This
  worktree is itself already linked, and the user has other Claude-managed
  worktrees.

## Implemented tool adaptations

The boundary is `claude_code_log/providers/codex_tools.py`: raw Codex calls are
adapted conservatively and then passed through the existing typed factories,
plugin transformers, and renderers.

| Codex shape | Canonical rendering | Current behavior |
|---|---|---|
| `exec_command` | `Bash` | `cmd` becomes `command`; direct result envelopes become literal code output |
| async `exec_command` followed by `wait`/`write_stdin` | one `Bash` | recognized polling chains are coalesced |
| `update_plan` | `TodoWrite` | typed todo rows; successful transport acknowledgement becomes `Todo list updated.` |
| `spawn_agent` | `Task` | task metadata retained; direct Fernet-shaped prompt hidden |
| `send_message` / `followup_task` | `SendMessage` | recipient/type retained; direct Fernet-shaped content hidden |
| `list_agents` | `TaskList` | agents and statuses become typed task rows |
| search-only `web__run` | `WebSearch` | query appears once; result is rendered as Markdown |
| open-only `web__run` | synthetic `WebFetch` pairs | one pair per ref when the combined result can be split exactly |
| single static `mcp__*` call | original MCP name | deliberately preserved for plugin transformers, notably ClMail |
| compound/dynamic `exec` | `Workflow` | orchestration JavaScript stays visible and lossless |
| unknown calls | generic | no invented semantic mapping |

Intentional generic fallbacks include `apply_patch`, standalone `wait`,
`write_stdin`, `wait_agent`, mixed/non-search web operations, and tools without
an honest shared equivalent.

## Implemented message/result improvements

- Codex `<environment_context>` user messages become Markdown tables for cwd,
  shell, date, timezone, workspace roots, and permissions.
- `<user_shell_command>` becomes the existing user-side Bash input/output pair.
- Bash `input_text` transport is rendered literally rather than as Markdown.
- redundant Task result JSON containing only `task_name` has no body;
  additional keys are preserved;
- opaque Fernet-shaped Task/SendMessage payloads are hidden on recognized
  direct mappings;
- WebSearch titles do not duplicate their query in the body;
- open-only batched web calls expand into adjacent WebFetch use/result pairs.

The isolated exploration copy was under:

```text
/tmp/codex-tools-home/sessions/2026/07/10/
rollout-2026-07-10T18-01-53-019f4cc3-922e-7cc2-9975-cd529e8871af.jsonl
```

`/tmp` is ephemeral. If it is gone, make a fresh copy of session
`019f4cc3-922e-7cc2-9975-cd529e8871af` before experimenting. Never render or
modify the original rollout in place; use `CODEX_HOME=/tmp/codex-tools-home`.

## Verification at handoff

The following focused suite passed immediately before handoff:

```bash
uv run pytest -q \
  test/test_codex_tools.py \
  test/test_codex_bash_results.py \
  test/test_codex_websearch_results.py \
  test/test_codex_task_results.py \
  test/test_codex_web_open.py \
  test/test_codex_list_agents.py \
  test/test_codex_user_shell.py \
  test/test_codex_messages.py \
  test/test_codex_provider.py \
  test/test_codex_cli.py
```

Result: `61 passed`.

The QA review records earlier evidence of clean full pyright and high focused
coverage, but those checks should be rerun after fixes. Use the repository's
supported suite split from `justfile`, not one giant configuration chosen by
guesswork. Before handoff to GitHub also run `git diff --check`, relevant Ruff,
full pyright, the full supported pytest split, synthetic HTML/Markdown CLI
exports, and a real copied-session smoke render.

## QA priorities

The untracked `work/codex-qa-review.md` describes seven release-blocking
findings. Start by pinning adversarial regressions, then fix them in narrow
commits:

1. Fernet payloads can leak when an exec wrapper falls back to `Workflow`.
2. visible-message deduplication is session-global/content-only and can remove
   a legitimate repeated message;
3. async Bash folding can cross visible activity or hide an unfinished live
   handle;
4. inherited parent-prefix stripping accepts weak/coincidental matches;
5. exec-wrapper recognition and object-literal rewriting are not sufficiently
   string/comment aware;
6. Codex-specific Task/WebSearch/Todo transport rules currently leak into the
   shared tool factory;
7. tolerant JSON decoding must also handle oversized integers and excessive
   nesting without exposing payloads in warnings.

After those, add the provider-to-renderer integration contract, cross-provider
contract/regression coverage, ambiguity/fallback cases, and a sanitized schema
variant corpus. The QA document contains the proposed commit order and full
acceptance gates.

Important secondary issue: `<user_shell_command>` parsing records exit code and
duration internally, but the current shared Bash message models do not render
them. Do not make failed commands look successful; either preserve this
metadata in an appropriate model or retain a lossless fallback for non-zero
exits.

## ClMail and agent coordination

This restart is also intended to pick up a clean ClMail reinstall. On the new
session:

- verify that the installed ClMail MCP/plugin tools are actually available;
- verify actor registration and a round trip before relying on automatic
  notifications;
- introduce the resumed actor to `ClMail/main` (the original coordinator) and
  check unread mail once during bootstrap;
- automatic notifications had previously been repaired, but do not assume a
  stale installation or `/tmp` marketplace is still authoritative;
- keep ClMail-specific rendering out of the Codex provider. Preserve raw
  `mcp__clmail__communicate` names and exercise the existing plugin transformer
  path instead.

The principal coordination contacts during the previous session were
`ClMail/main`, `alice`, and an alterego Codex actor in this worktree. Other
actors share the filesystem, so always inspect status/diffs immediately before
editing and committing.

## Useful orientation

- `work/codex-provider.md` — original provider plan, storage research, lineage
  model, and first implementation split.
- `work/codex-tools.md` — tool-adapter design and conservative fallback policy.
- `work/codex-qa-review.md` — detailed QA findings and acceptance plan.
- `claude_code_log/providers/codex.py` — rollout reconstruction and
  normalization; currently large and correlation-sensitive.
- `claude_code_log/providers/codex_tools.py` — exec-wrapper decoding and
  canonical input mappings.
- `claude_code_log/providers/codex_messages.py` — structured user-message
  parsing.
- `claude_code_log/factories/tool_factory.py` — shared result factories;
  currently contains Codex-specific behavior that QA says to move outward.

Do not begin with a broad refactor. First encode the unsafe alternatives as
failing tests, make normalization conservative, and preserve the generic
renderer whenever evidence is ambiguous.
