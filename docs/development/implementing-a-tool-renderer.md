# Implementing a Tool Renderer

> See [application_model.md](application_model.md) for the system overview.

This guide walks through adding rendering support for a new Claude Code tool, using WebSearch as an example.

## Overview

Tool rendering involves several components working together:

1. **Models** (`models.py`) - Type definitions for tool inputs and outputs
2. **Factory** (`factories/tool_factory.py`) - Parsing raw JSON into typed models
3. **HTML Formatters** (`html/tool_formatters.py`) - HTML rendering functions
4. **Renderers** - Integration with HTML and Markdown renderers

JSON output (`json/renderer.py`, since PR #36) needs **no per-tool
integration**: it serialises whatever typed input/output models the
factory produced via `dataclasses.asdict` (with a `_json_default`
shim for Pydantic models embedded inside the dataclasses). Add the
models in Step 1 and the factory hooks in Steps 2–3, and your tool
shows up in JSON exports automatically. The HTML/Markdown formatter
work in Steps 4–5 stays format-specific.

## Step 1: Define Models

### Tool Input Model

Add a Pydantic model for the tool's input parameters in `models.py`:

```python
class WebSearchInput(BaseModel):
    """Input parameters for the WebSearch tool."""
    query: str
```

### Tool Output Model

Add a dataclass for the parsed output. Output models are dataclasses (not Pydantic) since they're created by our parsers, not from JSON:

```python
@dataclass
class WebSearchLink:
    """Single search result link."""
    title: str
    url: str

@dataclass
class WebSearchOutput:
    """Parsed WebSearch tool output."""
    query: str
    links: list[WebSearchLink]
    preamble: Optional[str] = None  # Text before the Links
    summary: Optional[str] = None   # Markdown analysis after the Links
```

**Note:** Some tools have structured output with multiple sections. WebSearch is parsed as **preamble/links/summary** - text before Links, the Links JSON array, and markdown analysis after. This allows flexible rendering while preserving all content.

### Update Type Unions

Add the new types to the `ToolInput` and `ToolOutput` unions:

```python
ToolInput = Union[
    # ... existing types ...
    WebSearchInput,
    ToolUseContent,  # Generic fallback - keep last
]

ToolOutput = Union[
    # ... existing types ...
    WebSearchOutput,
    ToolResultContent,  # Generic fallback - keep last
]
```

## Step 2: Implement Factory Functions

In `factories/tool_factory.py`:

### Register Input Model

Add the input model to `TOOL_INPUT_MODELS`:

```python
TOOL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    # ... existing entries ...
    "WebSearch": WebSearchInput,
}
```

### Implement Output Parser

Create a parser function that extracts structured data from the raw result. Some tools (like WebSearch) have structured `toolUseResult` data available on the transcript entry, which is cleaner than regex parsing:

```python
def _parse_websearch_from_structured(
    tool_use_result: ToolUseResult,
) -> Optional[WebSearchOutput]:
    """Parse WebSearch from structured toolUseResult data.

    The toolUseResult for WebSearch has the format:
    {
        "query": "search query",
        "results": [
            {"tool_use_id": "...", "content": [{"title": "...", "url": "..."}]},
            "Analysis text..."
        ]
    }
    """
    if not isinstance(tool_use_result, dict):
        return None
    query = tool_use_result.get("query")
    results = tool_use_result.get("results")
    # ... extract links from results[0].content, summary from results[1] ...
    return WebSearchOutput(query=query, links=links, preamble=None, summary=summary)


def parse_websearch_output(
    tool_result: ToolResultContent,
    file_path: Optional[str],
    tool_use_result: Optional[ToolUseResult] = None,  # Extended signature
) -> Optional[WebSearchOutput]:
    """Parse WebSearch tool result from structured toolUseResult."""
    del tool_result, file_path  # Unused
    if tool_use_result is None:
        return None
    return _parse_websearch_from_structured(tool_use_result)
```

### Register Output Parser

Add to `TOOL_OUTPUT_PARSERS` and `PARSERS_WITH_TOOL_USE_RESULT`:

```python
TOOL_OUTPUT_PARSERS: dict[str, ToolOutputParser] = {
    # ... existing entries ...
    "WebSearch": parse_websearch_output,
}

# Parsers that accept the extended signature with tool_use_result
PARSERS_WITH_TOOL_USE_RESULT: set[str] = {"WebSearch"}
```

## Step 3: Implement HTML Formatters

In `html/tool_formatters.py`:

### Input Formatter

```python
def format_websearch_input(search_input: WebSearchInput) -> str:
    """Format WebSearch tool use content."""
    escaped_query = escape_html(search_input.query)
    return f'<div class="websearch-query">🔍 {escaped_query}</div>'
```

### Output Formatter

For tools with structured content like WebSearch, combine all parts into markdown then render:

```python
def _websearch_as_markdown(output: WebSearchOutput) -> str:
    """Convert WebSearch output to markdown: preamble + links list + summary."""
    parts = []
    if output.preamble:
        parts.extend([output.preamble, ""])
    for link in output.links:
        parts.append(f"- [{link.title}]({link.url})")
    if output.summary:
        parts.extend(["", output.summary])
    return "\n".join(parts)


def format_websearch_output(output: WebSearchOutput) -> str:
    """Format WebSearch as single collapsible markdown block."""
    markdown_content = _websearch_as_markdown(output)
    return render_markdown_collapsible(markdown_content, "websearch-results")
```

### Update Exports

Add functions to `__all__`:

```python
__all__ = [
    # ... existing exports ...
    "format_websearch_input",
    "format_websearch_output",
]
```

## Step 4: Wire Up HTML Renderer

In `html/renderer.py`:

### Import Formatters

```python
from .tool_formatters import (
    # ... existing imports ...
    format_websearch_input,
    format_websearch_output,
)
```

### Add Format Methods

```python
def format_WebSearchInput(self, input: WebSearchInput, _: TemplateMessage) -> str:
    return format_websearch_input(input)

def format_WebSearchOutput(self, output: WebSearchOutput, _: TemplateMessage) -> str:
    return format_websearch_output(output)
```

### Add Title Method (Optional)

For a custom title in the message header:

```python
def title_WebSearchInput(self, input: WebSearchInput, message: TemplateMessage) -> str:
    return self._tool_title(message, "🔎", f'"{input.query}"')
```

## Step 5: Implement Markdown Renderer

In `markdown/renderer.py`:

### Import Models

```python
from ..models import (
    # ... existing imports ...
    WebSearchInput,
    WebSearchOutput,
)
```

### Add Format Methods

```python
def format_WebSearchInput(self, input: WebSearchInput, _: TemplateMessage) -> str:
    """Format -> empty (query shown in title)."""
    return ""

def format_WebSearchOutput(self, output: WebSearchOutput, _: TemplateMessage) -> str:
    """Format -> markdown list of links."""
    parts = [f"Query: *{output.query}*", ""]
    for link in output.links:
        parts.append(f"- [{link.title}]({link.url})")
    return "\n".join(parts)

def title_WebSearchInput(self, input: WebSearchInput, _: TemplateMessage) -> str:
    """Title -> '🔎 WebSearch `query`'."""
    return f'🔎 WebSearch `{input.query}`'
```

## Step 6: Add Tests

Create test cases in the appropriate test files:

1. **Parser tests** - Verify output parsing handles various formats
2. **Formatter tests** - Verify HTML/Markdown output is correct
3. **Integration tests** - Verify end-to-end rendering

JSON output is exercised by the broader `test/test_json_rendering.py`
/ `test/test_json_real_projects.py` suites; per-tool JSON output
typically needs no dedicated test because the `dataclasses.asdict`
serialisation is trivial. Add a JSON-specific case only if your tool
embeds a non-dataclass type the `_json_default` shim doesn't already
cover.

## Renderer-set input fields driven by tool_result data

Most renderer passes set fields *on the consumer's input model*
based on what an *earlier* tool_result emitted — e.g.
`TaskOutputInput.creating_call_message_index` is stamped by
`_link_task_id_consumers` from the matching `BashOutput.background_task_id`
so the consumer's title can back-link to the spawn (#154).

PR #158 introduced the *forward* counterpart: fields set on the
**spawn's** input model that are sourced from the spawn's *own*
tool_result. Concretely, `BashInput.minted_background_task_id` and
`TaskInput.minted_agent_id` are hoisted from `BashOutput.background_task_id`
/ the parsed launch confirmation so the spawn card's title can show
`#<id>` directly (instead of leaving the reader to scrape it out of
the result body). The same pass also stamps `linked_consumer_message_index`
on the spawn from the first consumer it finds.

This is the first "renderer-set input field driven by the same
tool_use's tool_result" shape in the codebase. If you add another,
keep these conventions:

- **Field lives on the input model**, not the output model — title
  formatters read from the input, so the field has to be there to
  drive the title.
- **Default `None`**, set only inside the renderer pass; never trust
  parser-side state for this.
- **Use `ctx.get(message_index)`** to navigate from the tool_result's
  `pair_first` back to the spawn's `TemplateMessage` — that's the
  primary lookup, not iterating `ctx.messages` again.
- **First wins** (e.g. `setdefault`-style assignment guarded by an
  `is None` check) so re-running the pass is idempotent and document
  order remains deterministic.
- **Title formatter degrades gracefully**: when the field is `None`
  (no matching result observed, or the spawn lives outside the
  loaded slice), fall back to the plain title shape — `[async]`
  without the id, plain `#<id>` without the anchor, etc.

## Checklist

- [ ] Add input model to `models.py`
- [ ] Add output model to `models.py`
- [ ] Update `ToolInput` union
- [ ] Update `ToolOutput` union
- [ ] Add to `TOOL_INPUT_MODELS` in factory
- [ ] Implement output parser function
- [ ] Add to `TOOL_OUTPUT_PARSERS` in factory
- [ ] Add to `PARSERS_WITH_TOOL_USE_RESULT` if using structured data (optional)
- [ ] Add HTML input formatter
- [ ] Add HTML output formatter
- [ ] Wire up HTML renderer format methods
- [ ] Add HTML title method (if needed)
- [ ] Add Markdown format methods
- [ ] Add Markdown title method
- [ ] Add tests
- [ ] Update `__all__` exports
