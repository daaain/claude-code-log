# #213: Support hierarchies of agents — investigation & design

> Status: design approved 2026-06-12 (see §7 decisions) — implementing.
> Branch: `dev/agent-hierarchies`. Builds on the #174 nested DOM /
> splice work (all merged through #217).

Claude Code 2.1.172 added "Sub-agents can now spawn their own
sub-agents (up to 5 levels deep)". This doc records what the on-disk
data actually looks like (verified with generated sessions on CC
2.1.173), what breaks in the current pipeline at depth ≥ 2, and the
proposed design.

## 1. Ground truth (CC 2.1.173, generated test sessions)

Two scenarios were generated and inspected: a 2×2 fan-out (trunk → two
mid-agents → two leaves each) and a self-replicating linear recursion
chain.

**On-disk layout — flat, at every depth:**

```
<project>/<sid>.jsonl                      # trunk
<project>/<sid>/subagents/
    agent-<id>.jsonl                       # one per agent, ANY depth
    agent-<id>.meta.json                   # {agentType, description, toolUseId}
```

There is no nested directory structure: a depth-5 agent's transcript
sits next to a depth-1 agent's in the same `subagents/` dir.

**Entry shape inside agent files (all depths):** every entry carries
the *trunk* `sessionId`, `isSidechain: true`, and `agentId` = the
agent's own id. The root entry has `parentUuid: null`. Tool_result
entries inside agent files do NOT carry a top-level `toolUseResult`
(that enrichment exists only in the trunk file).

**Parent → child linkage:** only the in-band metadata tail on the
spawning tool_result's content — `agentId: <id> (use SendMessage with
to: '<id>' to continue this agent)` + `<usage>` block (the format
`parse_agent_result_metadata` already understands). At trunk level the
structured `toolUseResult.agentId` additionally exists.

**Child → parent linkage:** `agent-<id>.meta.json` has `toolUseId` =
the id of the spawning `Agent` tool_use. Crucially this exists even
when the spawn never returned (see next point).

**Interrupted spawns:** when the user interrupts, the spawning
tool_result arrives with `is_error: true` and the generic "The user
doesn't want to proceed…" text — no agentId tail, no `toolUseResult`.
The child transcripts are on disk but only `meta.json` links them.

**The 5-level cap is NOT enforced.** A prompt instructing each agent
to spawn exactly one child reached depth **79** (one transcript per
level, ~17 KB each) before being externally interrupted; no cap error
ever surfaced at any level. Two consequences: (a) worth reporting
upstream; (b) the renderer must treat nesting depth as unbounded —
nothing may assume ≤ 5.

**Sub-agents cannot start dynamic workflows.** A probe sub-agent
confirmed the `Workflow` tool is absent from a sub-agent's tool
surface (both direct and deferred/ToolSearch). So "a sub-agent
starting its own workflow" is impossible today via `Agent`-spawned
sub-agents; nested-workflow composition is de-scoped (§6). Whether a
*workflow* sub-agent can spawn `Agent` children is untested (follow-up
probe; the probe agent itself did have `Agent`).

**Models flatten relayed spawn instructions.** A headless trunk asked
to "launch an agent that launches an agent" inlined the leaf task
directly (single level, fabricated nesting in its answer). Test
fixtures must come from real spawns, not from trusting the narrative.

## 2. What already copes with depth (no work needed)

- **Loading is recursive**: `load_transcript` follows agent references
  in loaded agent files; the flat dir means uniform path resolution.
- **DAG layer**: `_integrate_agent_entries` has an explicit
  nested-anchor path (cross-agent-boundary guard); flat synthetic sids
  (`{trunk}#agent-{id}`) stay collision-free at any depth;
  `_build_uuid_to_render_sid` maps a nested agent's uuids to its
  *immediate* parent's sid by design.
- **CSS indentation**: structural (`.children` nesting); the #215
  comment explicitly anticipated arbitrary depth.
- **Fold machinery**: per-node fold bars + descendant counts computed
  on the tree — depth-agnostic.
- **Result-tail parsing**: the `(use SendMessage …)` suffix is already
  handled (`models.py` tail contract).
- **Detail levels**: sidechain stripping at LOW keys on the boolean
  flag, which all depths carry.

## 3. What breaks at depth ≥ 2 (verified)

0. **Discovery** — only 2 of the 86 agent transcripts in the test
   session load. Nested refs are collected from `toolUseResult.agentId`
   which nested entries don't have; the interrupted chain head has no
   ref at all. Everything below is moot until this is fixed.
1. **`_get_message_hierarchy_level`** — a boolean-sidechain model:
   sidechain user/assistant → level 4, sidechain tools → level 5,
   regardless of depth. A nested agent's entries flatten to its
   parent agent's level (the level-stack can't tell them apart).
2. **`_relocate_subagent_blocks`** — an anchor must be a tool_result
   *outside* any agent block (`"#agent-" not in sid`). A nested
   agent's anchor lives inside its parent's block, so the nested block
   falls into the defensive tail-append: wrong position AND flattened.
3. **Anchor identification** — in the flat-file format no raw entry
   carries a cross-agent reference (the existing sidechain-anchor path
   in `_integrate_agent_entries` predates it). The spawning tool_result
   inside agent A's file has `agentId = A` (membership), and the
   reference to child B exists only in the text tail / meta.json — the
   single `agentId` field can't carry both meanings.
4. **CSS group borders** — the sidechain-group rule's
   `:not(.sidechain)` parent filter deliberately suppresses group
   lines below depth 1; there is no depth-differentiated color scheme
   ("rethink the CSS levels" from the issue).
5. **Timeline** — all sidechain content lands in the single
   `sidechain` lane regardless of depth (acceptable initially).
6. **Workflow splice** — `_graft_agent_sidechannel` re-renders agent
   transcripts without passing workflow links down, so a workflow
   started by a sub-agent would never splice (moot today, see §1).

## 4. Proposed design

### Phase A — loader: discovery + linking

- Scan `<sid>/subagents/*.meta.json` once per session load into
  `{toolUseId → (agentId, agentType, description)}`.
- Collect agent ids from BOTH `toolUseResult.agentId` (trunk, as
  today) and meta-map hits on tool_use ids seen in loaded entries —
  applied recursively as agent files load. This also recovers
  interrupted chains (meta.json needs no tool_result).
- Introduce a dedicated **`spawned_agent_id`** field (synthetic, ours)
  set on the spawning tool_result entry (or the tool_use when no
  result exists) — keeping the entry's own `agentId` membership-only.
- `_integrate_agent_entries`: the anchor scan reads
  `spawned_agent_id`; stamping (`{trunk}#agent-{id}`) is unchanged.

### Phase B — hierarchy: depth-aware levels + nested relocation

- Build `{sid → agent_depth}` (trunk 0, agent line = 1 + parent's).
- `_get_message_hierarchy_level` becomes depth-parameterized: each
  depth gets a block of 3 levels — user/teammate `3d+1`,
  assistant/thinking `3d+2`, tools/system-info `3d+3` (the current
  4/5 sidechain rules are the d=1 case, slightly compressed; the
  special cases — task_notification, hooks — shift by `3d` likewise).
- `_relocate_subagent_blocks` becomes nested-aware: pre-build the
  anchor→block map, emit blocks depth-first so a block's members are
  themselves scanned as anchors for deeper blocks. The defensive
  tail-append stays as the orphan fallback.
- `_cleanup_sidechain_duplicates` already recurses per spawn node and
  should work once the tree is right — pin with tests.

### Phase C — CSS levels & visuals (steering welcome)

- Emit an `agent-depth-N` class on sidechain cards (N = the sid's
  agent depth; styles defined for a 5-color cycle, deeper depths wrap
  via `((N-1) mod 5) + 1` — no unbounded CSS).
- Generalize the sidechain group-border rule: key on the child's depth
  class rather than `:not(.sidechain)`, with per-depth line colors
  continuing the parent card's border color (the #215 color-pairing
  principle). Depth 1 keeps today's tool-green; depths 2–5 need a
  palette decision.
- Timeline: keep the single sidechain lane for now.

### Phase D — fixture + tests

- New `test/test_data/nested_agents/` distilled from the generated
  sessions: the 2×2 fan-out, a 4-deep linear chain, and one
  interrupted spawn (meta.json-only link). Sanitized ids/paths.
- Tests: discovery (incl. meta-only), DAG parentage, depth invariants
  (each agent's entries strictly under its spawn pair), relocation
  order, HTML structural assertions (depth classes + group borders),
  detail-level behavior, markdown parity, style-guide/snapshot deltas.

## 5. Suggested PR slicing

1. **PR1**: Phases A+B+D — loader, hierarchy, fixtures, tests (the
   structural meat; renders nested agents correctly with today's flat
   sidechain styling).
2. **PR2**: Phase C — depth classes + border/color scheme + any
   interactive polish round (mirrors the #215 pattern of a visual
   round on real data).

## 6. De-scoped / watching

- **Nested dynamic workflows** — impossible today (no `Workflow` tool
  on sub-agents). The splice's session-wide monotonic allocator and
  the `workflow_links` map are compatible with a future recursive
  graft (pass links into `_graft_agent_sidechannel`'s sub-render) if
  this lands upstream. Follow-up probe: can a *workflow* agent spawn
  `Agent` children, and where do those files go?
- **Timeline depth lanes / shading.**
- **Pagination**: a nested block split across page boundaries (the
  depth-1 variant of this is pre-existing behavior).

## 7. Decisions (2026-06-12)

1. **Color ramp for depths 2–5**: not strictly needed (line counting +
   indentation are clues enough), but include it if it's low-effort and
   looks nicer — keep it simple, it's polish not structure (PR2).
2. **Depth badge on nested agent cards**: yes — useful (PR2; e.g.
   nothing at d1, a small "d3" chip deeper).
3. **No upstream report** on the unenforced 5-level cap. Position: at
   this tool's level, better to support anything that comes in — depth
   is treated as unbounded throughout.
4. **PR slicing per §5 approved**: PR1 structural (Phases A+B+D),
   PR2 visual (Phase C).

## 8. PR2 scope (visual layer — in progress)

Branch `dev/agent-hierarchies-visuals`, stacked on PR1 (#218):

- [ ] Depth badge on nested agent spawn cards (nothing at depth 1).
- [ ] Optional per-depth group-line color ramp (5-color cycle, d1 keeps
      tool-green) — only if it stays cheap and looks better.
- [ ] "transcript ≡ result" marker for fully-collapsed spawns (a
      verbatim-deduped transcript is currently indistinguishable from a
      never-rendered one).
- [ ] Deep-chain indent ergonomics: cumulative 2em steps push very deep
      chains (observed: 79 levels) off-screen — consider capping or
      compressing the step at high depth once the badge carries the
      depth information.
- [ ] Interactive polish round on real nested-session data.
