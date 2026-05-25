# Plugin System

`claude-code-log` exposes a plugin system that lets third-party
packages rewrite parsed message content with their own typed
subclasses and render them through their own format/title methods,
without modifying core. This page is the as-built reference for
**plugin authors** writing a new plugin, and for maintainers of the
plugin machinery itself.

For the design discussion that led here, see the RFC at
[`work/tool-renderer-plugins.md`](../work/tool-renderer-plugins.md);
that doc captures the alternatives considered. This page documents
what shipped.

---

## 1. What a plugin does

The pipeline (full overview in
[application_model.md](application_model.md)) reads JSONL transcript
entries, passes them through the [`factories/`](../claude_code_log/factories/)
layer to build typed `MessageContent` instances, then dispatches to a
renderer that emits HTML, Markdown, or JSON.

A plugin inserts itself **between the factory output and the renderer
dispatch**. It can:

1. Match a candidate `MessageContent` by its class (an `applies_to`
   MRO filter) — e.g. *every* `ToolUseMessage`, or *every*
   `UserTextMessage`.
2. Inspect the candidate (e.g. check `tool_name`, regex the text).
3. Return a replacement `MessageContent` — typically a plugin-defined
   subclass — that carries its own `format_markdown` /
   `format_html` / `title` methods.

Two motivating use cases drove the design:

- **MCP tool rendering.** A specific MCP tool name (e.g.
  `mcp__plugin_clmail_clmail__communicate`) deserves prettier output
  than the generic JSON-dump fallback. A plugin specializes the
  generic `ToolUseMessage` into a plugin-defined subclass with
  bespoke `format_markdown`.
- **Hook-style demotion.** A `UserTextMessage` whose body matches a
  marker (e.g. `[hook] ...`) gets reclassified into a typed wrapper
  so it can render compactly or be hidden at low detail levels.

Plugins are **discovered through entry points**, so just `pip install`
ing a plugin package wires it in — no edit to `claude-code-log`
itself.

---

## 2. Quick start: write your first plugin

The fastest path is to copy the **reference plugin** at
[`test/_plugins/clmail/`](../test/_plugins/clmail/) and edit it. That
package is the layer-4 test fixture for the plugin-system test suite
AND the canonical author example — the two roles are intentionally
combined so the doc cannot drift from working code.

Steps:

1. **Copy the layout.** A plugin is a normal Python package with one
   declarative addition in `pyproject.toml`. Minimum tree:

   ```
   my_plugin/
   ├── pyproject.toml
   └── src/my_plugin/
       ├── __init__.py
       └── transformers/
           ├── __init__.py
           └── <one file per transformer>.py
   ```

2. **Declare the entry points.** In `pyproject.toml`:

   ```toml
   [project.entry-points."claude_code_log.plugins"]
   my_thing = "my_plugin.transformers.thing:MyTransformer"
   ```

   The key on the left is a stable identifier the loader logs at
   startup; the value on the right is `module:ClassName`. The class
   must satisfy the [`MessageTransformer`](#3-the-messagetransformer-protocol)
   Protocol (next section).

3. **Write the transformer.** A `MessageTransformer` declares three
   `ClassVar`s plus a `transform` method:

   ```python
   from typing import ClassVar, Optional
   from claude_code_log.factories.priorities import TOOL_INPUT_GENERIC
   from claude_code_log.models import (
       MessageContent, MessageMeta, ToolUseMessage,
   )

   class MyTransformer:
       name: ClassVar[str] = "my-plugin.my-thing"
       priority: ClassVar[int] = TOOL_INPUT_GENERIC - 500
       applies_to: ClassVar[tuple[type[MessageContent], ...]] = (
           ToolUseMessage,
       )

       def transform(
           self,
           content: MessageContent,
           meta: MessageMeta,
       ) -> Optional[MessageContent]:
           if not isinstance(content, ToolUseMessage):
               return None  # defensive narrowing
           if content.tool_name != "mcp__my_server__my_tool":
               return None
           return MyToolMessage(  # plugin-defined subclass; see §4
               meta=content.meta,
               input=content.input,
               tool_use_id=content.tool_use_id,
               tool_name=content.tool_name,
               skill_body=content.skill_body,
           )
   ```

4. **Write the message subclass.** Inherit from the matched type so
   the [runtime contract](#7-runtime-contract-enforcement) accepts
   your return value, then add `format_markdown` / `format_html` /
   `title` methods. See [§4](#4-class-side-format--title-methods).

   Field-copy patterns for the common parent classes (constructor
   signatures spelled out so the keyword-only-with-`None`-default
   fields aren't easy to miss):

   ```python
   # ToolUseMessage — copying every parent field including the
   # optional `skill_body` (kw-only, defaults to None; carries the
   # Skill-tool slash-command body when present per issue #93).
   return MyToolUseSubclass(
       meta=content.meta,
       input=content.input,
       tool_use_id=content.tool_use_id,
       tool_name=content.tool_name,
       skill_body=content.skill_body,  # keep — None is the common case
   )

   # ToolResultMessage — `is_error` / `tool_name` / `file_path` all
   # default to False/None but must be copied to preserve the
   # surrounding context (error styling, downstream tool grouping,
   # Read/Edit/Write file backlinks).
   return MyToolResultSubclass(
       meta=content.meta,
       tool_use_id=content.tool_use_id,
       output=content.output,
       is_error=content.is_error,
       tool_name=content.tool_name,
       file_path=content.file_path,
   )
   ```

5. **Install and run.** `pip install -e .` against your plugin
   package; the next `claude-code-log` invocation discovers it.

6. **Test it.** See [§9](#9-testing-your-plugin) for layer-by-layer
   coverage suggestions.

The reference plugin demonstrates three branches of the contract:

- [`hook_demotion.py`](../test/_plugins/clmail/src/claude_code_log_clmail_test/transformers/hook_demotion.py)
  — rewrite a `UserTextMessage` based on text-prefix match.
- [`tool_communicate.py`](../test/_plugins/clmail/src/claude_code_log_clmail_test/transformers/tool_communicate.py)
  — rewrite a `ToolUseMessage` based on `tool_name`.
- [`tool_communicate_result.py`](../test/_plugins/clmail/src/claude_code_log_clmail_test/transformers/tool_communicate_result.py)
  — rewrite a `ToolResultMessage`, demonstrating the long-Markdown-body
  collapsible-rendering pattern via the public
  [`render_markdown_collapsible`](#41-plugin-facing-helpers) helper.

Read all three before writing your own; together they cover ~95 % of
the shapes a real plugin needs.

---

## 3. The `MessageTransformer` Protocol

Defined in [`claude_code_log/plugins.py`](../claude_code_log/plugins.py).
Three required `ClassVar` attributes plus one method:

| Attribute / method | Type | Purpose |
|---|---|---|
| `name` | `ClassVar[str]` | Stable identifier surfaced in startup logs and collision warnings. Convention: `"<package>.<thing>"`. |
| `priority` | `ClassVar[int]` | Sort key for the transformer chain. Lower runs first. Use the constants in [`factories/priorities.py`](../claude_code_log/factories/priorities.py) to position yourself relative to other plugins. |
| `applies_to` | `ClassVar[tuple[type[MessageContent], ...]]` | The MRO filter: this transformer is asked only about candidates that are instances (via `isinstance`) of one of these classes. |
| `transform(content, meta)` | `(MessageContent, MessageMeta) -> Optional[MessageContent]` | Inspect `content`; return a replacement, or `None` to pass through. |

The Protocol is `runtime_checkable`, but `runtime_checkable` only
verifies *methods*. The loader explicitly validates the three
`ClassVar`s — missing or malformed metadata triggers a `WARNING` log
and the plugin is silently dropped (the rest of `claude-code-log`
keeps working).

The class does NOT need to inherit from `MessageTransformer`. Any
class matching the structural shape is accepted, which keeps plugins
free of an import-time dependency on the Protocol object.

---

## 4. Class-side `format` / `title` methods

Plugin-defined `MessageContent` subclasses carry their own render
methods on the class itself (rather than on the renderer). The
renderer's dispatcher consults them after the renderer's own
`format_<ClassName>` methods (see [§5](#5-dispatch-resolution-order)).

```python
from dataclasses import dataclass
from typing import ClassVar, Optional
from claude_code_log.models import DetailLevel, ToolUseMessage

@dataclass
class MyToolMessage(ToolUseMessage):
    """Plugin-defined subclass; carries its own render methods."""

    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    def format_markdown(self, _renderer, _message) -> str:
        action = (self.input.input or {}).get("action", "?")
        return f"_(my plugin) action={action}_"

    def format_html(self, _renderer, _message) -> Optional[str]:
        return None  # fall back to mistune(format_markdown)

    def title(self, _renderer, _message) -> Optional[str]:
        return "✉ my plugin"
```

Signature contract for each method:

| Method | Signature | Return | Notes |
|---|---|---|---|
| `format_markdown` | `(self, renderer, message) -> str` | Markdown source string. | Define this whenever your class produces meaningful Markdown. Drives both Markdown output AND HTML output (via mistune) unless `format_html` is also defined. |
| `format_html` | `(self, renderer, message) -> str` | Raw HTML string (real string — no None sentinel). | Define this ONLY when you need HTML different from mistune-of-`format_markdown`. The dispatcher synthesizes that fallback automatically when `format_html` is absent. |
| `title` | `(self, renderer, message) -> Optional[str]` | Heading text or `None`. | Return `None` for "headless" (inline) messages. Return `""` (empty string, not None) to suppress the heading explicitly — the dispatcher distinguishes the two. |

**`format_html` is opt-in.** If your plugin class defines only
`format_markdown`, the HtmlRenderer dispatcher automatically
synthesizes HTML by running the Markdown through mistune and
wrapping the result in `<div class="markdown">…</div>`. You do NOT
need to write a `render_markdown(self.format_markdown(...))` shim
— that's the dispatcher's job.

Define `format_html` only when you need HTML that differs from the
synthesized fallback (e.g. a collapsible `<details>` block for long
bodies, custom DOM structure, embedded SVG). When you do, return a
real string. There is no `None`-as-sentinel: returning `None` would
render as the literal string `"None"` in the card body (and may
raise a type error). The reference plugin's `tool_communicate_result.py`
shows the explicit-`format_html` pattern for a collapsible long-body
case; `tool_communicate.py` and `hook_demotion.py` show the absent-
`format_html`-let-the-synthesizer-handle-it pattern.

**Error-shaped results.** Set `is_error=True` on a `ToolResultMessage`
subclass replacement to inherit the host's standard error chrome
(🚨 emoji, red `.tool_result.error` CSS class). The mechanism is
wired in `html/utils.py`: `isinstance(content, ToolResultMessage)
and content.is_error` triggers both. Bash errors use the same
primitive — no custom plugin styling needed.

The dispatcher looks up these methods on each MRO node's `__dict__`
explicitly (not via `getattr`/inheritance). That means: **a class
opts in by defining the method ON the class itself**. Inheriting
`format_markdown` from a parent does NOT auto-enable dispatch for
the subclass; the subclass must define its own or the MRO walk
moves to the next ancestor.

### 4.1 Plugin-facing helpers

Two helpers are re-exported from `claude_code_log.plugins` for use
in `format_html` / `format_markdown` methods. The re-export is the
stable plugin API; the underlying implementation in
`claude_code_log/html/utils.py` may move or be renamed.

```python
from claude_code_log.plugins import (
    render_markdown,
    render_markdown_collapsible,
)
```

| Helper | Signature | Use when |
|---|---|---|
| `render_markdown(text)` | `(str) -> str` | You need Markdown→HTML inside a custom `format_html` (e.g. embedding a Markdown fragment in a richer HTML scaffold). |
| `render_markdown_collapsible(raw_content, css_class, *, line_threshold=20, preview_line_count=5)` | `(str, str, int, int) -> str` | Long Markdown bodies (mail bodies, agent responses, multi-paragraph result text). Returns inline `<div class="{css_class} markdown">…</div>` for short content, a collapsible `<details>` with preview + full body for content exceeding `line_threshold`. |

The reference plugin's
[`tool_communicate_result.py`](../test/_plugins/clmail/src/claude_code_log_clmail_test/transformers/tool_communicate_result.py)
shows the collapsible helper in use; the inline-vs-collapsed
threshold + preview length are both tunable per call.

Add to `claude_code_log.plugins.__all__` only on concrete plugin-author
demand — every entry is an API commitment. Open an issue if a helper
you need isn't exposed.

---

## 5. Dispatch resolution order

`Renderer._dispatch_format` and `_dispatch_title` (both in
[`renderer.py`](../claude_code_log/renderer.py)) walk
`type(obj).__mro__`, asking two questions at each node:

| Strategy | Lookup | Caller signature |
|---|---|---|
| **1. Renderer-side** | `getattr(self, f"format_{cls.__name__}", None)` | `method(obj, message)` |
| **2. Class-side** | `cls.__dict__.get(method_attr)` where `method_attr = f"format_{self._class_dispatch_format}"` | `method(obj, self, message)` |

Strategy 1 wins per MRO node — the existing `format_BashInput`,
`format_ToolUseMessage`, etc. on `HtmlRenderer`/`MarkdownRenderer`
keep working unchanged. Strategy 2 is what plugins use.

`_class_dispatch_format` is `"markdown"` on the base `Renderer` and
overridden to `"html"` on `HtmlRenderer`. That's how the HTML
renderer picks up your class-side `format_html` while the Markdown
renderer ignores it and picks up `format_markdown`.

**To shadow a built-in renderer method from a plugin**, define the
class-side method on the *plugin subclass* — the MRO walk visits
the plugin subclass before the built-in's renderer-side method, so
Strategy 1 at the plugin subclass's name (which the renderer
doesn't have) fails, Strategy 2 on the plugin subclass hits, and
the dispatcher never reaches the parent's renderer-side method.

`title_content` (the entry point for message headings) delegates to
`_dispatch_title` for the same reason — without delegation, a
`title_ToolUseMessage` on the base renderer would shadow your
class-side `title()` at the top level.

### 5.1 HtmlRenderer extension: actual-class precedence + Markdown synthesis

`HtmlRenderer._dispatch_format` overrides the base walk with two
additional rules, applied to the actual class (`type(obj)`) before
the standard MRO walk runs:

1. **`format_html` on the actual class wins outright.** If
   `type(obj).__dict__["format_html"]` exists, use it. Skip the MRO
   walk entirely — a plugin author who wrote `format_html` on their
   subclass owns the HTML rendering.
2. **`format_markdown` on the actual class triggers synthesis.** If
   `format_html` is absent but `format_markdown` is defined on the
   actual class, the dispatcher renders the Markdown through mistune
   and wraps the result in `<div class="markdown">…</div>`. Skip the
   MRO walk — the synthesized output is the answer.
3. **Otherwise, defer to the base walk.** This finds renderer-side
   `format_<ClassName>` methods for built-in content classes and
   class-side methods on ancestors via the normal MRO.

The actual-class precedence is the key behavioural difference from
the base dispatcher: a plugin subclass of `UserTextMessage` that
defines `format_markdown` (but not `format_html`) gets its Markdown
promoted to HTML via synthesis even though the base renderer has
`format_UserTextMessage` that would normally win the MRO walk. The
intent: when a plugin author wrote `format_markdown` on a subclass,
they meant their Markdown to drive rendering, not for the parent
class's built-in renderer behaviour to take over.

---

## 6. `detail_visibility`

`claude-code-log` filters messages per the `--detail` flag. Levels
in order of decreasing verbosity:

```
FULL > HIGH > LOW > MINIMAL > USER_ONLY
```

Your plugin class declares a `ClassVar[DetailLevel]` to opt into
class-based visibility:

```python
detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW
```

**Semantics: monotone-down.** The message is visible iff the
current detail level is *at least as verbose as* the declared
minimum. With the ordering above:

| Declared | Visible at |
|---|---|
| `FULL` | `FULL` only |
| `HIGH` | `FULL`, `HIGH` |
| `LOW` | `FULL`, `HIGH`, `LOW` |
| `MINIMAL` | `FULL`, `HIGH`, `LOW`, `MINIMAL` |
| `USER_ONLY` | all levels |

The order is pinned in a `_DETAIL_ORDER` map in `renderer.py` (so a
future reorder of the enum can't silently flip semantics), guarded
by a module-load assertion that every `DetailLevel` value is mapped.

**Opt-in nature.** Plugin classes that declare `detail_visibility`
bypass the legacy `_HIGH_EXCLUDE_CLASSES` / `_LOW_KEEP_TOOLS`
registries entirely — your declaration is authoritative. A plugin
class that inherits from a built-in (e.g. `MyToolMessage(ToolUseMessage)`)
but does NOT declare `detail_visibility` inherits the built-in's
filter membership through the bridge.

**Practical guide.** Pick based on user-perceived value:

- `FULL` only — debug/dev signal that clutters normal viewing.
- `HIGH` — interesting but optional; user has opted into detail.
- `LOW` — should appear in the default summary view (the typical
  choice for tool-rendering plugins; bypasses the `_LOW_KEEP_TOOLS`
  allowlist that core would otherwise check).
- `MINIMAL` — essential context (sparingly).
- `USER_ONLY` — visible even in user-only views (almost never the
  right choice for a tool/hook plugin; reserved for user-originated
  content).

**`HIGH` vs `FULL` for hook-style content** — the two reference
plugins make different choices here, deliberately:

- `hook_demotion.py` (this repo's test plugin) uses `FULL` —
  surfaces only in the most-verbose view. Right when the hook
  notification is pure noise reduction for typical reviewers.
- A real-world plugin (e.g. for clmail-style hook notifications a
  reviewer wants to *see when they fired*) typically picks `HIGH`
  — surfaces in `HIGH` *and* `FULL`, hidden at `LOW` and below.
  Right when the hook firing itself is signal worth keeping in the
  detail view.

The rule of thumb: ask "would a reviewer skimming at `HIGH` want
to know this happened?" If yes, pick `HIGH`. If only at the
debug-the-transcript level, pick `FULL`.

---

## 7. Runtime contract enforcement

`apply_transformers` (in `plugins.py`) enforces two contracts at
runtime; both surface as `WARNING` logs and pass-through:

1. **Exception safety.** If `transform()` raises, the exception is
   logged and the candidate falls through to the next transformer.
   A buggy plugin cannot crash the whole conversion.

2. **Return-type enforcement.** The replacement must satisfy
   `isinstance(replacement, transformer.applies_to)`. A
   `UserTextMessage`-targeting transformer returning a
   `SystemMessage` (or worse, a string / dict) is rejected with a
   warning. **In practice, this means your replacement class must
   subclass one of the `applies_to` types** — not sit as a sibling.

   The reference `TestHookNotificationMessage` is a `UserTextMessage`
   subclass (not a bare `MessageContent` sibling) for exactly this
   reason. The inherited `items` field stays empty if your class
   carries the parsed data in dedicated fields.

`transform()` returning `None` means "not my case"; the dispatcher
moves to the next matching transformer. This is the right return
value for a "specific tool name" filter pattern (see
`tool_communicate.py`).

---

## 8. Discovery and ordering

### 8.1 Entry-point group

Plugins are discovered via the entry-point group:

```toml
[project.entry-points."claude_code_log.plugins"]
my_thing = "my_plugin.transformers.thing:MyTransformer"
```

The loader (`load_transformers` in `plugins.py`) is process-scoped
and cached. Tests call `reset_cache()` to force re-discovery.

### 8.2 Priority ordering

Transformers are sorted by `(priority, __module__, __qualname__)`:

- **Primary key: `priority` (int).** Lower runs earlier. The
  built-in priority constants in
  [`factories/priorities.py`](../claude_code_log/factories/priorities.py)
  describe notional positions on a numeric scale. Plugins position
  themselves relative to these without core renumbering:

  ```
  COMMAND_MESSAGE        = 100
  LOCAL_COMMAND_OUTPUT   = 200
  BASH_INPUT_OUTPUT      = 300
  TEAMMATE_MESSAGE       = 400
  TASK_NOTIFICATION      = 500
  HOOK_NOTIFICATION      = 600
  SLASH_COMMAND_ISMETA   = 700
  TEXT_FALLBACK          = 1000
  TOOL_INPUT_GENERIC     = 5000
  TOOL_OUTPUT_GENERIC    = 5100
  ```

  Gaps of 100 leave room for plugin insertion. Use the constant
  (`TOOL_INPUT_GENERIC - 500`) rather than a literal so a future
  core renumber stays consistent.

- **Tie-breakers: `__module__`, `__qualname__`.** Deterministic
  cross-environment ordering when two plugins land at the same
  priority but in different packages. A `(priority, applies_to)`
  collision still triggers a `WARNING` so you can detect overlap.

**Convention for multi-transformer plugins.** When a single plugin
ships several transformers that share an `applies_to` (e.g. a
plugin covering five MCP tools, all matching `ToolUseMessage` at
`TOOL_INPUT_GENERIC - 500`), they will collide with each other on
the `(priority, applies_to)` tie and emit warnings at startup. Two
ways to silence the self-collision:

1. **Per-tool offset.** Give each transformer in the plugin a
   small single-digit offset off the base: `priority =
   TOOL_INPUT_GENERIC - 504`, `- 503`, `- 502`, `- 501`, `- 500`.
   The offset range stays narrow enough that the plugin still
   sits in a coherent "slot" relative to built-ins / other
   plugins, but each transformer is uniquely ordered against the
   others in the same plugin.
2. **Narrow `applies_to`.** If the transformers actually match
   disjoint subsets (one targets ToolUseMessage, another
   ToolResultMessage), the tie disappears naturally — same
   priority is fine.

The per-tool-offset pattern is the right answer when all your
transformers genuinely share `applies_to` and only differ in the
`tool_name` they narrow to inside `transform()`.

**Important caveat about v1 semantics.** In v1, plugin transformers
run as a **post-classification pass**: the built-in factory chain
classifies every entry first, *then* the priority-ordered plugin
list runs. So the priority ordering applies *among plugins*, not
against the built-in classifiers (which have already finished by
the time your plugin sees a candidate). The RFC's "interleaved with
built-in detectors" framing is a v2 consideration; v1's
post-classification scope covers every documented use case (clmail
hook-demotion, MCP tool rendering) because plugins always operate
on a candidate the built-in chain has classified (typically as
`UserTextMessage` or generic `ToolUseMessage`).

### 8.3 First non-`None` wins

`apply_transformers` walks the priority-sorted list, asks each
matching transformer (via `applies_to`), and returns the first
non-`None` reply. A transformer that returns `None` for a candidate
lets the next matching transformer try — this is the natural way to
say "specific filter inside a broad `applies_to`".

---

## 9. Testing your plugin

The plugin system ships with a four-layer test strategy in
[`test/test_plugin_system.py`](../test/test_plugin_system.py); your
own plugin should follow the same shape:

| Layer | What it covers | How to write yours |
|---|---|---|
| **1. Loader unit** | Validator rejects malformed metadata; sort and tie-break warnings | Usually skip for a normal plugin — the core tests cover this. |
| **2. Dispatch matrix** | Renderer-side vs class-side resolution; HTML vs Markdown output | Skip unless your plugin does something exotic with the dispatcher. |
| **3. Transformer integration** | End-to-end: real `MessageContent` through your `transform()` and class-side render methods | Always write this. Drive your transformer with hand-built `MessageMeta.empty()` candidates; assert the replacement is an instance of your subclass and that the render methods return the expected text. |
| **4. Text-equivalence** | If your plugin reads `UserTextMessage.items`, assert that the joined text matches what the factory's `extract_text_content` produces | Recommended for any plugin keying on user text — protects you against future core refactors that sneak normalization between extraction and the items list. |

For an installable test plugin (your own or a fixture in your own
repo), declare it as an editable dev-dependency and reset the loader
cache in a test fixture:

```python
@pytest.fixture(autouse=True)
def _reset_plugin_cache():
    from claude_code_log.plugins import reset_cache
    reset_cache()
    yield
    reset_cache()
```

To inject a plugin directly (bypassing entry-point discovery — useful
for exception-safety tests):

```python
import claude_code_log.plugins as plugins
plugins._cached_transformers = [MyTransformer()]
```

Always reset the cache in a `try/finally` or via the autouse fixture
to avoid leaking state across tests.

---

## 10. Common patterns and pitfalls

**Defensive narrowing in `transform`.** Even though `applies_to`
filters the dispatch, write `if not isinstance(content, MyType):
return None` as the first line. It costs nothing, makes the body's
type-narrowing explicit to readers and to mypy/pyright, and survives
a future plugin author copy-pasting your code with the wrong
`applies_to`.

**`format_html` returning `None`.** Most plugins should return `None`
to fall back to mistune-rendered Markdown. Write a custom
`format_html` only when the Markdown formulation can't capture what
you want (e.g. embedded SVG, complex tables that mistune mangles).

**Don't escape Markdown manually for code spans.** Backslashes do not
escape backticks inside inline code spans (CommonMark explicit). If
you embed user input in `` `...` ``, count the longest backtick run
in the value and use a fence one tick longer. See `_inline_code` in
`markdown/renderer.py` for the helper.

**Inheriting from a built-in is mandatory for return-type
enforcement.** A sibling `MessageContent` subclass will be rejected
by `apply_transformers`. If you don't need the parent class's
fields, set them with defaults (`items: list = field(default_factory=list)`)
and ignore them in your render methods — that's what
`TestHookNotificationMessage` does.

**Priority constants, not literals.** Hard-coded `400` looks fine
until core renumbers `TEAMMATE_MESSAGE` to `350` and your plugin
silently changes order. Import from `factories.priorities`.

**Cache invalidation in tests.** Every test that adds, removes, or
modifies plugins (including injecting directly via
`_cached_transformers`) must `reset_cache()` afterward — process-wide
state otherwise leaks across tests.

**`detail_visibility` is checked via `hasattr` for the LOW keep-list
opt-out.** This means inheriting `detail_visibility` from a future
core-migrated parent class behaves the same as declaring it
yourself: the keep-list is bypassed. Usually what you want; mention
it if you're debugging a "why is my plugin visible at LOW even though
the tool isn't in `_LOW_KEEP_TOOLS`?" question.

**Markdown-shaped HTML and the `.markdown` CSS scope.** The
dispatcher returns your `format_html` output unmodified; the host
template wraps it in `<div class="content">…</div>`, *not*
`<div class="content markdown">…</div>`. So host theme rules scoped
under `.markdown` (table borders, code-block backgrounds, `<pre>`
overflow, list spacing) won't fire on your output unless the wrap
carries the class. Two ways to opt in:

1. **`has_markdown = True` on the subclass** (preferred when your
   `format_html` emits Markdown-shaped content end-to-end). The host
   template at `html/templates/transcript.html` reads
   `message.content.has_markdown` and flips the `markdown` class
   onto the wrapping `<div class='content'>` automatically:

   ```python
   @dataclass
   class MyMarkdownShapedMessage(ToolResultMessage):
       @property
       def has_markdown(self) -> bool:
           return True  # → <div class='content markdown'>
   ```

   Mirrors what built-ins (`AwaySummaryMessage`, `TeammateMessage`,
   `AssistantTextMessage`) already do — no plugin-author divergence.
   See `tool_communicate_result.py` in the reference plugin for a
   worked example.

2. **Wrap inline** (`<div class="markdown">…</div>`) when only part
   of your output is Markdown-shaped or when you need fine-grained
   scope control:

   ```python
   return f'<div class="markdown">{render_markdown(self.body)}</div>'
   ```

`render_markdown_collapsible` already wraps for you (its short-
content branch emits `<div class="{css_class} markdown">`); only
the bare `render_markdown` path needs one of the two recipes above.

**Note on the synthesis path.** When you DON'T define `format_html`
and the HtmlRenderer dispatch synthesizes HTML from your
`format_markdown` (see [§5.1](#51-htmlrenderer-extension-actual-class-precedence--markdown-synthesis)),
the synthesizer always wraps the mistune output in
`<div class="markdown">`. You don't need to set `has_markdown = True`
for that path — it's implicit in the synthesis. `has_markdown` only
matters when you implement `format_html` yourself and want the host
template's outer `<div class='content'>` wrapper to pick up the
`.markdown` class.

**Don't combine `has_markdown = True` with the synthesis path.** If
your class has no `format_html` (so synthesis fires AND wraps in
`<div class="markdown">`) AND you also set `has_markdown = True`,
the host template flips the `markdown` class onto its outer
`<div class='content'>` wrapper — you end up with the synthesizer's
`<div class="markdown">` nested inside `<div class="content markdown">`.
Benign for CSS (selectors don't care about depth) but visible in
the DOM, surprising on `view-source`, and harmlessly heavier. Rule
of thumb: `has_markdown = True` is the right opt-in only for
classes with an **explicit** `format_html` whose return value does
NOT already wrap. Synthesis classes leave `has_markdown` alone.

---

## 11. Reference

| Surface | Location |
|---|---|
| Protocol + loader + dispatch | [`claude_code_log/plugins.py`](../claude_code_log/plugins.py) |
| Priority constants | [`claude_code_log/factories/priorities.py`](../claude_code_log/factories/priorities.py) |
| Renderer dispatch | `Renderer._dispatch_format`, `Renderer._dispatch_title`, `HtmlRenderer._class_dispatch_format` in [`renderer.py`](../claude_code_log/renderer.py) / [`html/renderer.py`](../claude_code_log/html/renderer.py) |
| Visibility filter | `_content_visible_at`, `_filter_template_by_detail`, `_DETAIL_ORDER` in [`renderer.py`](../claude_code_log/renderer.py) |
| Reference plugin (canonical example) | [`test/_plugins/clmail/`](../test/_plugins/clmail/) + [`README.md`](../test/_plugins/clmail/README.md) |
| Test suite (four layers) | [`test/test_plugin_system.py`](../test/test_plugin_system.py) |
| Design discussion | [`work/tool-renderer-plugins.md`](../work/tool-renderer-plugins.md) |

---

## 12. v2 directions (informational)

Out of scope for v1; mentioned here so contributors don't keep
re-rediscovering them:

- **Interleaved dispatch.** Let plugins run *between* built-in
  detectors (e.g. before the generic `TextFallback` classifier), so
  a plugin can claim a `UserTextMessage` before the built-in chain
  has decided. Needs a redesign of the factory loop to call into
  the plugin chain at each detector boundary.
- **Renderer-side plugin extension.** Today only `MessageContent`
  subclasses participate; a v2 plugin could contribute renderer-side
  `format_<X>` methods for an existing core class without
  subclassing. Lower priority — class-side dispatch already covers
  90 % of the use cases.
- **Priority namespacing.** A `priority: ClassVar[int]` is global;
  large plugin ecosystems may want per-plugin priority namespaces
  with explicit ordering hints (e.g. `before=other_plugin`). Not
  needed at current scale.
