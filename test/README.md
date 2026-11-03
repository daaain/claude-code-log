# Claude Code Log Testing & Style Guide

This directory contains comprehensive testing infrastructure and visual documentation for the Claude Code Log template system.

## Test Data (`test_data/`)

Representative JSONL files covering all message types and edge cases:

**Note**: After the module split, import paths have changed:

- `from claude_code_log.parser import load_transcript, extract_text_content`
- `from claude_code_log.html.renderer import generate_html, format_timestamp`
- `from claude_code_log.converter import convert_jsonl_to_html`

### `representative_messages.jsonl`

A comprehensive conversation demonstrating:

- User and assistant messages
- Tool use and tool results (success cases)
- Markdown formatting and code blocks
- Summary messages
- Multiple message interactions

### `edge_cases.jsonl`

Edge cases and special scenarios:

- Complex markdown formatting
- Very long text content
- Tool errors and error handling
- System command messages
- Command output parsing
- Special characters and Unicode
- HTML escaping scenarios

### `session_b.jsonl`

Additional session for testing multi-session handling:

- Different source file content
- Session divider behavior
- Cross-session message ordering

### `real_projects/` (Integration Test Data)

Real-world JSONL data from open-source Claude Code projects, used for integration testing:

| Project | Size | Files | Purpose |
|---------|------|-------|---------|
| `-Users-dain-workspace-JSSoundRecorder` | ~528KB | 11 | Small project, quick tests |
| `-Users-dain-workspace-coderabbit-review-helper` | ~6.5MB | 40 | Empty file edge cases (9 empty files) |
| `-Users-dain-workspace-danieldemmel-me-next` | ~1.7MB | 11 | Multi-cwd sessions, path conversion |
| `-Users-dain-workspace-claude-code-log-sample` | ~9MB | 23 | Curated sample with size variety |

These files test:

- **Multi-project hierarchy processing** with `--projects-dir`
- **Cache operations** with realistic data volumes
- **Edge cases**: Empty files, naming ambiguity, path conversion
- **CLI operations** with custom projects directory

## Asserting against a full rendered page

`generate_html()` returns a **whole document** — the inlined stylesheets, the
scripts and the page chrome, all of it ahead of any message content. So a
substring assertion over that output is **ambiguous by default**, and
`str.index` / `str.find` return the *earliest* hit, which is very often in the
CSS.

This is not hypothetical. A test asserting that one image rendered before
another used the bare words `first` and `second` as its image payloads:

```python
assert html.index("second") < html.index("first")   # ← cannot fail
```

`--text-secondary` sits at `claude_code_log/html/templates/components/global_styles.css:87`
and `.header > span:first-child` at `:180`, both in the `<head>`. The assertion
compared those two CSS offsets, held for every input, and would have passed
with the feature it was pinning entirely removed.

**So for any ordering, position or presence claim over a rendered page:**

1. Use tokens that cannot occur in a template, stylesheet, script or prose —
   not English words, and not anything resembling a CSS identifier.
2. **Assert the token is unique**, in the test:

   ```python
   assert html.count("ZZBLOCKALPHAZZ") == 1
   ```

   Keep this even when the tokens obviously look unique. It is what stops a
   future stylesheet or template edit silently re-ambiguating the assertion —
   `first` and `second` were unique-looking too. The one-time check becomes a
   standing guard so nobody has to remember.

   **Assert the count you actually expect, not always `1`.** Some counts are
   legitimately higher and the guard is still worth having: **user message text
   appears twice**, because the renderer emits a markdown view *and* a raw
   view of the same message, so `count(...) == 2` is correct there and
   `index()` anchors on the rendered copy. A count guard that asserts the real
   number still fails when a template change alters it — which is the point.
   Anchoring on text at all is worth avoiding where an image payload will do.
3. When an assertion is the *only* thing pinning a claim, check that it can
   fail on its own: neutralise the feature **and** relax the test's other
   assertions, so that assertion is the only one that could fail — then confirm
   it does. A green mutation only tells you *some* assertion fired; read which
   one.

## Template Tests (`test_template_rendering.py`)

Comprehensive unit tests that verify:

### Core Functionality

- ✅ Basic HTML structure generation
- ✅ All message types render correctly
- ✅ Session divider logic (only first session shown)
- ✅ Multi-session content combining
- ✅ Empty file handling

### Message Type Coverage

- ✅ User messages with markdown
- ✅ Assistant responses
- ✅ Tool use and tool results
- ✅ Error handling for failed tools
- ✅ System command messages
- ✅ Command output parsing
- ✅ Summary messages

### Formatting & Safety

- ✅ Timestamp formatting
- ✅ CSS class application
- ✅ HTML escaping for security
- ✅ Unicode and special character support
- ✅ JavaScript markdown setup

### Template Systems

- ✅ Transcript template (individual conversations)
- ✅ Index template (project listings)
- ✅ Project summary statistics
- ✅ Date range filtering display

## Visual Style Guide (`../scripts/generate_style_guide.py`)

Generates comprehensive visual documentation:

### Generated Files

- **Main Index** (`index.html`) - Overview and navigation
- **Transcript Guide** (`transcript_style_guide.html`) - All message types
- **Index Guide** (`index_style_guide.html`) - Project listing examples

### Coverage

The style guide demonstrates:

- 📝 **Message Types**: User, assistant, system, summary
- 🛠️ **Tool Interactions**: Usage, results, errors
- 📏 **Text Handling**: Long content, wrapping, formatting
- 🌍 **Unicode Support**: Special characters, emojis, international text
- ⚙️ **System Messages**: Commands, outputs, parsing
- 🎨 **Visual Design**: Typography, colors, spacing, responsive layout

### Usage

```bash
# Generate style guides
uv run python scripts/generate_style_guide.py

# Open in browser
open scripts/style_guide_output/index.html
```

## Running Tests

### Test Categories

The project uses a categorized test system to avoid async event loop conflicts between different testing frameworks:

#### Test Categories

- **Unit Tests** (no mark): Fast, standalone tests with no external dependencies
- **TUI Tests** (`@pytest.mark.tui`): Tests for the Textual-based Terminal User Interface
- **Browser Tests** (`@pytest.mark.browser`): Playwright-based tests that run in real browsers
- **Integration Tests** (`@pytest.mark.integration`): Tests with realistic JSONL data from `test_data/real_projects/`
- **Snapshot Tests**: HTML regression tests using syrupy (runs with unit tests)

### Snapshot Testing

Snapshot tests capture the full HTML output and detect unintended regressions. They use [syrupy](https://github.com/syrupy-project/syrupy) with a custom serializer that normalises dynamic content (library version, tmp paths).

```bash
# Run snapshot tests
uv run pytest test/test_snapshot_html.py -v

# Update snapshots after intentional HTML changes
uv run pytest -n0 test/test_snapshot_html.py --snapshot-update

# Review changes before committing
git diff test/__snapshots__/
```

**Snapshot files** are stored in `test/__snapshots__/test_snapshot_html.ambr` and must be committed to version control.

**When to update snapshots**:

1. Run tests - if they fail, review the diff
2. If changes are intentional, run with `--snapshot-update`
3. Commit updated snapshots with your code changes

#### Running Tests

```bash
# Run only unit tests (fast, recommended for development)
just test
# or: uv run pytest -m "not (tui or browser or integration)" -v

# Run TUI tests (isolated event loop)
just test-tui
# or: uv run pytest -m tui -v

# Run browser tests (requires Chromium)
just test-browser
# or: uv run pytest -m browser -v

# Run integration tests with realistic data
just test-integration
# or: uv run pytest -m integration -v

# Run all tests in sequence (separated to avoid conflicts)
just test-all

# Run specific test file
uv run pytest test/test_template_rendering.py -v

# Run specific test method
uv run pytest test/test_template_rendering.py::TestTemplateRendering::test_representative_messages_render -v

# Run tests with coverage
just test-cov
```

#### Why Test Categories?

The test suite is categorized because:

- **TUI tests** use Textual's async event loop (`run_test()`)
- **Browser tests** use Playwright's internal asyncio
- **Integration tests** process real-world data (slower, more comprehensive)
- **pytest-asyncio** manages async test execution

Running all tests together can cause "RuntimeError: This event loop is already running" conflicts. The categorization ensures reliable test execution by isolating different async frameworks.

### Test Coverage

Generate detailed coverage reports:

```bash
# Run tests with coverage and HTML report
uv run pytest --cov=claude_code_log --cov-report=html --cov-report=term

# View coverage by module
uv run pytest --cov=claude_code_log --cov-report=term-missing

# Open HTML coverage report
open htmlcov/index.html
```

Current coverage: **78%+** across all modules:

- `parser.py`: 81% - Data extraction and parsing
- `renderer.py`: 86% - HTML generation and formatting  
- `converter.py`: 52% - High-level orchestration
- `models.py`: 89% - Pydantic data models

### Manual Testing

```bash
# Test with representative data
uv run python -c "
from claude_code_log.converter import convert_jsonl_to_html
from pathlib import Path
html_file = convert_jsonl_to_html(Path('test/test_data/representative_messages.jsonl'))
print(f'Generated: {html_file}')
"

# Test multi-session handling
uv run python -c "
from claude_code_log.converter import convert_jsonl_to_html
from pathlib import Path
html_file = convert_jsonl_to_html(Path('test/test_data/'))
print(f'Generated: {html_file}')
"
```

## Development Workflow

When modifying templates:

1. **Make Changes** to `claude_code_log/templates/`
2. **Run Tests** to verify functionality
3. **Generate Style Guide** to check visual output
4. **Review in Browser** to ensure design consistency

## File Structure

```
test/
├── README.md                     # This file
├── conftest.py                   # Pytest configuration and fixtures
├── snapshot_serializers.py       # Custom syrupy serializer for HTML normalisation
├── __snapshots__/                # Syrupy snapshot files
│   └── test_snapshot_html.ambr   # HTML output snapshots
├── test_data/                    # Test JSONL samples
│   ├── representative_messages.jsonl
│   ├── edge_cases.jsonl
│   ├── session_b.jsonl
│   └── real_projects/            # Realistic multi-project test data (~18MB)
│       ├── -Users-dain-workspace-JSSoundRecorder/
│       ├── -Users-dain-workspace-coderabbit-review-helper/
│       ├── -Users-dain-workspace-danieldemmel-me-next/
│       └── -Users-dain-workspace-claude-code-log-sample/
├── test_snapshot_html.py         # HTML output snapshot tests
├── test_integration_realistic.py # Integration tests with real data
├── test_template_rendering.py    # Template rendering tests
├── test_template_data.py         # Template data structure tests
├── test_template_utils.py        # Utility function tests
├── test_message_filtering.py     # Message filtering tests
├── test_date_filtering.py        # Date filtering tests
└── test_*.py                     # Additional test modules

scripts/
├── setup_test_data.sh            # Copies test data from ~/.claude/projects/
├── generate_style_guide.py       # Visual documentation generator
└── style_guide_output/           # Generated style guides
    ├── index.html
    ├── transcript_style_guide.html
    └── index_style_guide.html

htmlcov/                          # Coverage reports
├── index.html                    # Main coverage report
└── *.html                        # Per-module coverage details
```

## Benefits

This testing infrastructure provides:

- **Regression Prevention**: Catch template breaking changes with snapshot testing
- **Coverage Tracking**: 78%+ test coverage with detailed reporting
- **Module Testing**: Focused tests for parser, renderer, and converter modules
- **Integration Testing**: Real-world data from open-source projects (~18MB)
- **Visual Documentation**: See how all message types render
- **Development Reference**: Example data for testing new features
- **Quality Assurance**: Verify edge cases and error handling
- **Design Consistency**: Maintain visual standards across updates
- **CLI Testing**: Full coverage of `--projects-dir` and multi-project operations

The combination of unit tests, integration tests, coverage tracking, and visual style guides ensures both functional correctness and design quality across the modular codebase.
