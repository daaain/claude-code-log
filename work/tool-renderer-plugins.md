# Plugin system — v2 backlog

## Status: v1 shipped (PR #169); v2 work captured below

The v1 implementation landed in PR #169. As-built reference lives at
[`dev-docs/plugins.md`](../dev-docs/plugins.md). This file retains
the items the RFC explicitly deferred to v2.

## Open questions (deferred to v2)

- **Plugin caching.** Entry-point discovery costs ~10ms on first call. If startup profiling shows it, cache the resolved transformer list to disk keyed by installed plugin versions.
- **Plugin enable/disable flag.** `--no-plugin <name>` or env var to mask a plugin without uninstalling. Deferred until requested.
- **Plugin version pinning.** No machine-readable "requires claude-code-log >= X.Y" yet. Use pyproject `requires`; cross that bridge when a breaking Protocol change happens.
- **MCP namespace sugar.** Match `clmail__communicate` against any `mcp__*__clmail__communicate`. Declined for v1; plugins declare exact verbatim tool names. Revisit once we have two MCP servers exposing the same tool name.
- **Icon centralization.** Follow-up could migrate scattered icon literals (`html/renderer.py:843–930`) into a registry populated by plugin classes' icon declarations. v1 keeps icons in title methods.
- **Built-in migration to class-method pattern.** Mechanical follow-up after v1 lands. Reduces the renderer classes' surface area and unifies dispatch. Not blocking.
- **Built-in migration from `_HIGH_EXCLUDE_CLASSES` to `detail_visibility`.** Same: mechanical follow-up.
- **Transformer chaining.** First non-None wins in v1; no chaining. Revisit only with a concrete use case.
- **Interleaved dispatch.** Today plugins run as a post-classification pass. Letting plugins run *between* built-in detectors (e.g. before the generic `TextFallback` classifier) would let a plugin claim a `UserTextMessage` before the built-in chain has decided. Needs a redesign of the factory loop to call into the plugin chain at each detector boundary.
- **Namespace-collision diagnosis.** No `--list-plugins` CLI in v1. Startup warning logs cover the worst case (two transformers with same priority and `applies_to`). Follow-up if needed.

## Future extensions (post-v2)

The same entry-point machinery extends cleanly to:

1. **Pluggable formatters.** A new group `claude_code_log.formatters` discovers full output formats (RTF, JATS, etc.). Discovery, priority, and detail-level vocabulary all carry over. A formatter plugin walks the `TemplateMessage` tree; classes contribute `format_<output_format>` methods for any format they wish to support, falling back to "derive from Markdown" for the rest.
2. **Pluggable factories.** Plugins introducing entirely new top-level dispatch chains (rather than transforming inside an existing one) — e.g. a new entry type the harness might emit in future. Much larger surface; not on the near-term roadmap.
3. **Renderer-side plugin extension.** Today only `MessageContent` subclasses participate; a future plugin could contribute renderer-side `format_<X>` methods for an existing core class without subclassing. Lower priority — class-side dispatch already covers 90 % of the use cases.
4. **Priority namespacing.** A `priority: ClassVar[int]` is global; large plugin ecosystems may want per-plugin priority namespaces with explicit ordering hints (e.g. `before=other_plugin`). Not needed at current scale.
