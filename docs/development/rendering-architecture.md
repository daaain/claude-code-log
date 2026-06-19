# Rendering Architecture

> See [application_model.md](application_model.md) for the system overview.

This document describes how Claude Code transcript data flows from raw JSONL entries to final output (HTML, Markdown, JSON). The architecture separates concerns into distinct layers:

1. **Parsing Layer** - Raw JSONL to typed transcript entries
2. **Factory Layer** - Transcript entries to `MessageContent` models
3. **Rendering Layer** - Format-neutral tree building and relationship processing
4. **Output Layer** - Format-specific rendering (HTML, Markdown, JSON)

---

## 1. Data Flow Overview

```
JSONL File
    ↓ (parser.py)
list[TranscriptEntry]
    ↓ (factories/)
list[TemplateMessage] with MessageContent     ← factory-layer
                                                normalisation seam
                                                (raw → display-polished)
    ↓ (renderer.py: generate_template_messages)
Tree of TemplateMessage (roots with children)
+ RenderingContext (message registry)
+ Session navigation data
    ↓ (html/renderer.py | markdown/renderer.py | json/renderer.py)
Final output (HTML, Markdown, or JSON)
```

**The factory-layer seam matters**: any cleanup that should appear
in *every* output format (slash-command normalisation, command-args
hardening, teammate session-color enrichment, etc.) lives at factory
time, in the typed `MessageContent` models. The three renderers are
pure consumers of the polished tree — they never re-implement
display polish per format. As a corollary, when a new output format
is added (JSON shipped this way in PR #36), it inherits all polish
for free as long as it consumes `generate_template_messages`'
output.

**Key cardinality rules**:
- Each transcript entry has a `uuid`, but a single entry's `list[ContentItem]` may be chunked and produce multiple `MessageContent` objects (e.g., tool_use items are split into separate messages)
- Each `MessageContent` gets exactly one `TemplateMessage` wrapper
- The `message_index` (assigned during registration) uniquely identifies a `TemplateMessage` within a render

---

## 2. Naming Conventions

The codebase uses consistent suffixes to distinguish layers:

| Suffix | Layer | Examples |
|--------|-------|----------|
| `*Content` | ContentItem (JSONL parsing) | `TextContent`, `ToolUseContent`, `ThinkingContent`, `ImageContent` |
| `*Input` | Tool input models | `BashInput`, `ReadInput`, `TaskInput` |
| `*Output` | Tool output models | `ReadOutput`, `EditOutput`, `TaskOutput` |
| `*Message` | MessageContent (rendering) | `UserTextMessage`, `ToolUseMessage`, `AssistantTextMessage` |
| `*Model` | Pydantic JSONL models | `UserMessageModel`, `AssistantMessageModel` |

**Key distinction**:
- `ToolUseContent` is the raw JSONL content item
- `ToolUseMessage` is the render-time wrapper containing a typed `ToolInput`
- `BashInput` is a specific tool input model parsed from `ToolUseContent.input`

---

## 3. The Factory Layer

Factories ([factories/](../claude_code_log/factories/)) transform raw transcript data into typed `MessageContent` models. Each factory focuses on a specific message category:

| Factory | Creates | Key Function |
|---------|---------|--------------|
| [meta_factory.py](../claude_code_log/factories/meta_factory.py) | `MessageMeta` | `create_meta(entry)` |
| [user_factory.py](../claude_code_log/factories/user_factory.py) | User message types | `create_user_message(meta, content_list, ...)` |
| [assistant_factory.py](../claude_code_log/factories/assistant_factory.py) | Assistant messages | `create_assistant_message(meta, items)` |
| [tool_factory.py](../claude_code_log/factories/tool_factory.py) | Tool use/result | `create_tool_use_message(meta, item, ...)` |
| [system_factory.py](../claude_code_log/factories/system_factory.py) | System messages | `create_system_message(meta, ...)` |

### Factory Pattern

All factory functions require `MessageMeta` as the first parameter:

```python
def create_user_message(
    meta: MessageMeta,
    content_list: list[ContentItem],
    ...
) -> UserTextMessage | UserSlashCommandMessage | ...
```

This ensures every `MessageContent` has valid metadata accessible via `content.meta`.

### Tool Input Parsing

Tool inputs are parsed into typed models in [tool_factory.py:create_tool_input()](../claude_code_log/factories/tool_factory.py):

```python
TOOL_INPUT_MODELS: dict[str, type[ToolInput]] = {
    "Bash": BashInput,
    "Read": ReadInput,
    "Write": WriteInput,
    ...
}

def create_tool_input(tool_use: ToolUseContent) -> ToolInput:
    model_class = TOOL_INPUT_MODELS.get(tool_use.name)
    if model_class:
        return model_class.model_validate(tool_use.input)
    return tool_use  # Fallback to raw ToolUseContent
```

### Tool Output Parsing

Tool outputs use a **different approach** than inputs. While inputs are parsed via Pydantic `model_validate()`, outputs are extracted from text using **regex patterns** since tool results arrive as text content:

```python
TOOL_OUTPUT_PARSERS: dict[str, ToolOutputParser] = {
    "Read": parse_read_output,
    "Edit": parse_edit_output,
    "Write": parse_write_output,
    "Bash": parse_bash_output,
    "Task": parse_task_output,
    ...
}

def create_tool_output(tool_name, tool_result, file_path) -> ToolOutput:
    if parser := TOOL_OUTPUT_PARSERS.get(tool_name):
        if parsed := parser(tool_result, file_path):
            return parsed
    return tool_result  # Fallback to raw ToolResultContent
```

Each parser extracts text from `ToolResultContent` and parses patterns like:
- `cat -n` format: `"   123→content"` for file content with line numbers
- Structured prefixes: `"The file ... has been updated."` for edit results

---

## 4. The TemplateMessage Wrapper

`TemplateMessage` (in [renderer.py](../claude_code_log/renderer.py)) wraps `MessageContent` with render-time state:

**MessageContent** (pure transcript data):
- `meta: MessageMeta` - timestamp, session_id, uuid, is_sidechain, etc.
- `message_type` property - type identifier ("user", "assistant", etc.)
- `has_markdown` property - whether content contains markdown

**TemplateMessage** (render-time wrapper):
- `content: MessageContent` - the wrapped content
- `meta` property - delegates to `content.meta` (`message.meta is message.content.meta`)
- `message_index: Optional[int]` - unique index in RenderingContext registry
- `message_id` property - formatted as `"d-{message_index}"` for HTML element IDs

Relationship fields (populated by processing phases, using `message_index` for references):
- Pairing: `pair_first`, `pair_last`, `pair_duration`, `is_first_in_pair`, `is_last_in_pair`
- Hierarchy: `ancestry` (list of parent `message_index` values), `children`
- Fold/unfold: `immediate_children_count`, `total_descendants_count`

---

## 5. Format-Neutral Processing Pipeline

The core rendering pipeline is in [generate_template_messages()](../claude_code_log/renderer.py). It returns:

1. **Tree of TemplateMessage** - Session headers as roots with nested children
2. **Session navigation data** - For table of contents
3. **RenderingContext** - Message registry for `message_index` lookups

> Doc links into `renderer.py` use function/class names rather than
> line numbers — the file is large and churns, so line anchors drift.
> Search by symbol name.

### Processing Phases

The pipeline runs many strictly-ordered in-place passes over a flat
`list[TemplateMessage]`. The four below are the conceptual backbone;
the remaining passes (listed in the addendum) refine ordering,
hierarchy, and cross-links on top of them.

#### Phase 1: Message Loop
[_render_messages()](../claude_code_log/renderer.py) creates `TemplateMessage` wrappers for each transcript entry. The loop handles:
- Inserting session headers at session boundaries (trunk + branch)
- Creating `MessageContent` via factories
- Registering messages in `RenderingContext`

#### Phase 2: Pairing
[_identify_message_pairs()](../claude_code_log/renderer.py) marks related messages:
- **Adjacent pairs**: thinking+assistant, bash-input+output, system+slash-command
- **Indexed pairs**: tool_use+tool_result (by tool_use_id)

After identification, [_reorder_paired_messages()](../claude_code_log/renderer.py) moves `pair_last` messages adjacent to their `pair_first`.

#### Phase 3: Hierarchy
[_build_message_hierarchy()](../claude_code_log/renderer.py) assigns `ancestry` based on message relationships:
- User messages at level 1
- Assistant/system at level 2
- Tool use/result at level 3
- Sidechain messages at level 4+

#### Phase 4: Tree Building
[_build_message_tree()](../claude_code_log/renderer.py) populates `children` lists from `ancestry`:

```
Session Header (root)
  └─ User message
       └─ Assistant message
            └─ Tool use
            └─ Tool result
                 └─ Sidechain assistant (Task result children)
```

#### Full pass ordering (addendum)

The four backbone phases above run *within* a longer ordered sequence.
In code order, `generate_template_messages`:

1. **Setup** — filters warmup sessions, then prepares session metadata:
   `prepare_session_summaries` + `prepare_session_ai_titles` (merged),
   `prepare_session_team_names`, and `_extract_session_hierarchy`.
2. **Pre-render filtering** — `_filter_messages` (structural only).
   Detail-level filtering is no longer a pre-render pass — the
   single-axis ghosting model moved it entirely to step 5.
3. **Collect + render** — `_collect_session_info`, then
   `_render_messages` (**Phase 1**: wrappers, session headers,
   registration), then `_pair_skill_tool_uses` (which ghosts the
   consumed slots in place and calls `_drop_anchor_refs_into_ghosts`).
4. **Junction linking** — junction forward-link population on fork
   points (`_link_junction_forwards`). Branch-header previews are
   computed in step 3 by `_build_branch_header` scanning the
   branch's DAG-line uuids; there's no separate back-fill pass.
5. **Post-render detail filter** — `_ghost_template_by_detail` (only
   below FULL): sets non-visible slots to `None` in place (no reindex),
   then calls `_repair_stale_anchor_refs`.
6. **Nav + structure** — `prepare_session_navigation`, then
   `_reorder_session_template_messages`, `_identify_message_pairs`
   (**Phase 2**), `_reorder_paired_messages`,
   `_relocate_subagent_blocks`, `_build_message_hierarchy`
   (**Phase 3**), `_mark_messages_with_children`, `_build_message_tree`
   (**Phase 4**), `_cleanup_sidechain_duplicates`.
7. **Trailing metadata / link passes** — `_populate_teammate_colors`,
   `_populate_task_metadata`, `_link_async_notifications`,
   `_link_workflow_runs`, `_link_tool_use_notifications`,
   `_link_cron_jobs_by_id`, `_link_task_id_consumers`.
8. **Workflow splice (must stay last)** — `_splice_workflow_runs`
   attaches each linked `WorkflowRun`'s phase→agent→side-channel
   sub-tree at its Workflow tool_use site. It *appends* synthetic and
   grafted nodes through `ctx.register` (the monotonic index
   allocator), so it has to follow every pass that iterates
   `ctx.messages`. See [workflows.md § 5](workflows.md).

The code in `generate_template_messages` is the authoritative ordering.

---

## 6. RenderingContext

`RenderingContext` (in [renderer.py](../claude_code_log/renderer.py)) holds per-render state:

```python
@dataclass
class RenderingContext:
    messages: list[TemplateMessage]  # All messages by index
    tool_use_context: dict[str, ToolUseContent]  # For result→use lookup
    session_first_message: dict[str, int]  # Session header indices

    def register(self, message: TemplateMessage) -> int:
        """Assign message_index and add to registry."""

    def get(self, message_index: int) -> Optional[TemplateMessage]:
        """Lookup by index."""
```

This enables parallel-safe rendering where each render operation gets its own context.

---

## 7. The Renderer Class Hierarchy

The base `Renderer` class (in [renderer.py](../claude_code_log/renderer.py)) defines the method-based dispatcher pattern. Subclasses implement format-specific rendering.

### Dispatch Mechanism

The dispatcher finds methods by content type name and passes both the typed object and the `TemplateMessage`:

```python
def _dispatch_format(self, obj: Any, message: TemplateMessage) -> str:
    """Dispatch to format_{ClassName}(obj, message) method."""
    for cls in type(obj).__mro__:
        if cls is object:
            break
        if method := getattr(self, f"format_{cls.__name__}", None):
            return method(obj, message)
    return ""
```

For example, `ToolUseMessage` with `BashInput`:
1. `format_content(message)` calls `_dispatch_format(message.content, message)`
2. Finds `format_ToolUseMessage(content, message)` which calls `_dispatch_format(content.input, message)`
3. Finds `format_BashInput(input, message)` for the specific tool

### Consistent (obj, message) Signature

All `format_*` and `title_*` methods receive both parameters:

```python
def format_BashInput(self, input: BashInput, _: TemplateMessage) -> str:
    return format_bash_input(input)

def title_BashInput(self, input: BashInput, message: TemplateMessage) -> str:
    return self._tool_title(message, "💻", input.description)
```

This design gives handlers access to:
- **The typed object** (`input: BashInput`) for type-safe field access without casting
- **The full context** (`message: TemplateMessage`) for paired message lookups, ancestry, etc.

Methods that don't need the message parameter use `_` or `_message` (for LSP compliance in overrides).

### Title Dispatch

Similar pattern for titles via `title_{ClassName}` methods:

```python
def title_ToolUseMessage(self, content: ToolUseMessage, message: TemplateMessage) -> str:
    if title := self._dispatch_title(content.input, message):
        return title
    return content.tool_name  # Default fallback
```

### Subclass Implementations

**HtmlRenderer** ([html/renderer.py](../claude_code_log/html/renderer.py)):
- Implements `format_*` methods by delegating to formatter functions
- `_flatten_preorder()` traverses tree, formats content, builds flat list for template
- Generates HTML via Jinja2 templates

**MarkdownRenderer** ([markdown/renderer.py](../claude_code_log/markdown/renderer.py)):
- Implements `format_*` methods inline
- Writes directly to file/string without templates
- Simpler structure suited to plain text output

**JsonRenderer** ([json/renderer.py](../claude_code_log/json/renderer.py)):
- Doesn't implement `format_*` per content type — instead serialises
  the entire `TemplateMessage` subtree via `dataclasses.asdict` plus
  a small `_json_default` shim for the Pydantic models embedded in
  tool inputs/outputs (and for `Enum`/`Path`).
- Calls `title_content(msg)` to attach a per-node title that mirrors
  what HTML/Markdown surface — the only place dispatcher methods are
  reused.
- Output is a single JSON document per session (or per combined
  transcript / projects index) with the message tree nested directly
  under each node's `children` array. See [application_model.md
  § 2.5](application_model.md#25-json-export) for the payload shape
  and inheritance from the factory-layer normalisation seam.

---

## 8. HTML Formatter Organization

HTML formatters are split by message category:

| Module | Scope | Key Functions |
|--------|-------|---------------|
| [user_formatters.py](../claude_code_log/html/user_formatters.py) | User messages | `format_user_text_model_content()`, `format_bash_input_content()` |
| [assistant_formatters.py](../claude_code_log/html/assistant_formatters.py) | Assistant/thinking | `format_assistant_text_content()`, `format_thinking_content()` |
| [system_formatters.py](../claude_code_log/html/system_formatters.py) | System messages | `format_system_content()`, `format_session_header_content()` |
| [tool_formatters.py](../claude_code_log/html/tool_formatters.py) | Tool inputs/outputs | `format_bash_input()`, `format_read_output()`, etc. |
| [utils.py](../claude_code_log/html/utils.py) | Shared utilities | `render_markdown()`, `escape_html()`, `CSS_CLASS_REGISTRY` |

---

## 9. CSS Class Derivation

CSS classes are derived from content types using `CSS_CLASS_REGISTRY` in [html/utils.py](../claude_code_log/html/utils.py#L56):

```python
CSS_CLASS_REGISTRY: dict[type[MessageContent], list[str]] = {
    SystemMessage: ["system"],  # level added dynamically
    UserTextMessage: ["user"],
    UserSteeringMessage: ["user", "steering"],
    ToolUseMessage: ["tool_use"],
    ToolResultMessage: ["tool_result"],  # error added dynamically
    ...
}
```

The function `css_class_from_message()` walks the content type's MRO to find matching classes, then adds dynamic modifiers (sidechain, error level).

See [css-classes.md](css-classes.md) for the complete reference.

---

## 10. Key Architectural Decisions

### Content as Source of Truth

`MessageContent.meta` holds all identity data. `TemplateMessage.meta` is the same object:
```python
assert message.meta is message.content.meta  # Same object
```

Note that `meta.uuid` is the original transcript entry's UUID. Since a single entry may be split into multiple `MessageContent` objects (e.g., multiple tool_use items), several messages can share the same UUID. Use `message_index` for unique identification within a render.

### Tree-First Architecture

`generate_template_messages()` returns tree roots. Flattening for template rendering is an explicit step in `HtmlRenderer._flatten_preorder()`. This keeps the tree authoritative while supporting existing flat-list templates.

### Separation of Concerns

- **models.py**: Pure data structures, no rendering logic
- **factories/**: Data transformation, no I/O. **The
  normalisation seam** — display polish for *all* output formats
  lives here, not in renderers (e.g. `simplify_command_tags` lifting
  bare `<command-name>X</command-name>` to `/X`, with the same fix
  applied to both `simplify_command_tags` and
  `create_slash_command_message` so HTML/Markdown/JSON observe a
  single shape).
- **renderer.py**: Format-neutral processing (pairing, hierarchy, tree)
- **html/**, **markdown/**, **json/**: Format-specific output generation,
  consuming the polished tree without re-implementing display rules.

---

## Related Documentation

- [messages.md](messages.md) - Complete message type reference
- [css-classes.md](css-classes.md) - CSS class combinations and rules
- [message-hierarchy.md](message-hierarchy.md) - Fold/unfold state machine
- [dag.md](dag.md) - DAG-based message architecture (replaces timestamp-based ordering)
- [workflows.md](workflows.md) - Dynamic-workflow parsing + the run-tree splice
