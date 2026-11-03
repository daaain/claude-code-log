---
name: tool-renderer
description: Implement specialized rendering for Claude Code tools. Use when adding a new tool type (WebSearch, WebFetch, etc.) to the transcript viewer, or when asked to implement tool rendering.
---

# Implementing a Tool Renderer

This guide walks through adding rendering support for a new Claude Code tool, using WebSearch as an example.

## Before You Start

**Examine existing test data** to understand the tool's actual JSON structure:

```bash
# Find test files containing the tool
rg -l "ToolName" test/test_data/

# Look at actual JSONL entries
rg '"name":\s*"ToolName"' test/test_data/ -A 2 -B 2
```

Key fields to identify:
- **Input parameters**: What's in `tool_use.input`?
- **toolUseResult structure**: What metadata does the structured result contain?
- **tool_result.content**: What does the raw text output look like?

The `toolUseResult` field on transcript entries often contains richer structured data than `tool_result.content`. **Always prefer parsing from `toolUseResult` when available.**

## Overview

Tool rendering involves several components working together:

1. **Models** (`models.py`) - Type definitions for tool inputs and outputs
2. **Factory** (`factories/tool_factory.py`) - Parsing raw JSON into typed models
3. **HTML Formatters** (`html/tool_formatters.py`) - HTML rendering functions
4. **Renderers** - Integration with HTML and Markdown renderers

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

**Important**: Always check if the tool has structured `toolUseResult` data available. This is the preferred approach because:
- It's more reliable than regex parsing of text content
- It often contains metadata (timing, byte counts, status codes) not in the text
- The structure is well-defined and type-safe

Example `toolUseResult` structures in test data:
```json
// WebSearch
{"query": "...", "results": [...], "durationSeconds": 15.7}

// WebFetch
{"url": "...", "result": "...", "code": 200, "codeText": "OK", "bytes": 12345, "durationMs": 1500}
```

Create a parser function that extracts from `toolUseResult`:

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

Add to `TOOL_OUTPUT_PARSERS` and **register in `PARSERS_WITH_TOOL_USE_RESULT`** if using the extended signature:

```python
TOOL_OUTPUT_PARSERS: dict[str, ToolOutputParser] = {
    # ... existing entries ...
    "WebSearch": parse_websearch_output,
}

# REQUIRED for parsers that use toolUseResult - without this, the structured
# data won't be passed to your parser!
PARSERS_WITH_TOOL_USE_RESULT: set[str] = {"WebSearch", "WebFetch"}
```

**Note**: If your parser has the 3-argument signature `(tool_result, file_path, tool_use_result)`, you MUST add it to `PARSERS_WITH_TOOL_USE_RESULT`. Otherwise `create_tool_output()` won't pass the structured data.

## Step 3: Implement HTML Formatters

In `html/tool_formatters.py`:

### Input Formatter

**Design consideration**: The title already shows key info (tool name + primary parameter). Only show content in the body if it adds value or is too long for the title.

```python
def format_websearch_input(search_input: WebSearchInput) -> str:
    """Format WebSearch tool use content."""
    # If query is short enough to fit in title, return empty
    if len(search_input.query) <= 100:
        return ""  # Full query shown in title
    escaped_query = escape_html(search_input.query)
    return f'<div class="websearch-query">{escaped_query}</div>'
```

This avoids redundancy when the title already shows everything important.

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

### Escaping: all transcript content is untrusted

Every transcript value is attacker-controlled — the assistant echoes
user, file and web input verbatim, so a `<script>` payload can reach any
field you render. Run every value you interpolate into f-string HTML
through `escape_html()`, and render markdown only through
`render_markdown` / `render_markdown_collapsible`, which escape raw HTML
and neutralise unsafe link schemes. Add a payload-bearing case for each
new field you render (see `test/test_markdown_rendering.py` and
`test/test_xss_browser.py`). Detail:
[dev-docs/implementing-a-tool-renderer.md](../../../dev-docs/implementing-a-tool-renderer.md).

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

### Watch out: the template's wrench-suppression is emoji-range-gated

Tool messages get a default `🛠️` (tool_use) or `🧰` (tool_result)
prefix prepended by `html/templates/transcript.html` *unless* the
title already starts with an emoji that
`html/utils.py::starts_with_emoji` recognises. That function
whitelists specific Unicode ranges, listed in its docstring.

If the icon you pass to `_tool_title` falls **outside** those
ranges, the template adds the default icon in front of it,
producing a redundant double-icon title like
`🛠️ <your-icon> <ToolName>`. Verify by rendering a fixture and
grepping for `🛠️` co-occurring with your icon, or by calling
`starts_with_emoji` on your title.

If your icon is a real emoji that lives in a Unicode range not
listed there, **add the range** to `starts_with_emoji` rather than
picking a different icon.

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

Create a dedicated test file `test/test_{toolname}_rendering.py`. Tests are **required** - they catch regressions and document expected behavior.

### Test Structure

```python
"""Test cases for {ToolName} tool rendering."""

from claude_code_log.factories.tool_factory import parse_{toolname}_output
from claude_code_log.html.tool_formatters import (
    format_{toolname}_input,
    format_{toolname}_output,
)
from claude_code_log.models import (
    ToolResultContent,
    {ToolName}Input,
    {ToolName}Output,
)


class Test{ToolName}Input:
    """Test input model and formatting."""

    def test_input_basic(self):
        """Test input model creation."""
        ...

    def test_format_input_short(self):
        """Test formatting when content fits in title."""
        ...

    def test_format_input_long(self):
        """Test formatting when content is too long for title."""
        ...


class Test{ToolName}Parser:
    """Test output parsing."""

    def test_parse_structured_output(self):
        """Test parsing from structured toolUseResult."""
        ...

    def test_parse_minimal_output(self):
        """Test parsing with only required fields."""
        ...

    def test_parse_missing_field(self):
        """Test graceful failure with missing required field."""
        ...

    def test_parse_no_tool_use_result(self):
        """Test returns None when no toolUseResult."""
        ...


class Test{ToolName}OutputFormatting:
    """Test output HTML formatting."""

    def test_format_output_full(self):
        """Test formatting with all metadata."""
        ...

    def test_format_output_minimal(self):
        """Test formatting with minimal data."""
        ...
```

### Running Tests

```bash
# Run just your new tests
uv run pytest test/test_{toolname}_rendering.py -v

# Run full test suite to check for regressions
uv run pytest -m "not (tui or browser)" -v
```

## Checklist

### Models (`models.py`)
- [ ] Add input model (Pydantic `BaseModel`)
- [ ] Add output model (dataclass with all fields from `toolUseResult`)
- [ ] Update `ToolInput` union
- [ ] Update `ToolOutput` union

### Factory (`factories/tool_factory.py`)
- [ ] Add to `TOOL_INPUT_MODELS`
- [ ] Import output model
- [ ] Implement output parser with 3-arg signature if using `toolUseResult`
- [ ] Add to `TOOL_OUTPUT_PARSERS`
- [ ] Add to `PARSERS_WITH_TOOL_USE_RESULT` (required if parser uses `toolUseResult`)

### HTML (`html/tool_formatters.py`, `html/renderer.py`)
- [ ] Import models
- [ ] Add input formatter function
- [ ] Add output formatter function
- [ ] Update `__all__` exports
- [ ] Wire up `format_{Input}` method in renderer
- [ ] Wire up `format_{Output}` method in renderer
- [ ] Add `title_{Input}` method in renderer

### Markdown (`markdown/renderer.py`)
- [ ] Import models
- [ ] Add `format_{Input}` method
- [ ] Add `format_{Output}` method
- [ ] Add `title_{Input}` method

### Tests (`test/test_{toolname}_rendering.py`)
- [ ] Create test file
- [ ] Test input model creation
- [ ] Test input formatting (short/long content)
- [ ] Test parser with full structured data
- [ ] Test parser with minimal data
- [ ] Test parser with missing fields (graceful failure)
- [ ] Test parser with no `toolUseResult`
- [ ] Test output formatting
- [ ] Run full test suite to verify no regressions
