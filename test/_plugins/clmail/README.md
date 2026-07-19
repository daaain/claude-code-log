# Reference Test Plugin for `claude-code-log`

This package is the layer-4 fixture for the plugin-system test suite
**and** the canonical reference for third-party plugin authors. The
two roles are intentionally combined per the RFC's
`## Test-embedded reference plugin` section: tests against a real
plugin (vs. mocks) give much higher confidence in the contract, and
living code can't drift from the spec.

## Layout

```
claude_code_log_clmail_test/
├── __init__.py
└── transformers/
    ├── __init__.py
    ├── hook_demotion.py        # UserTextMessage rewrite by text-prefix
    └── tool_communicate.py     # ToolUseMessage rewrite by tool_name
```

## Entry-point declarations

```toml
[project.entry-points."claude_code_log.plugins"]
testhook_demotion       = "claude_code_log_clmail_test.transformers.hook_demotion:TestHookDemotion"
tool_clmail_communicate = "claude_code_log_clmail_test.transformers.tool_communicate:ClmailCommunicateInputTransformer"
```

Each `MessageTransformer` class declares:

| ClassVar     | Purpose                                                                   |
|--------------|---------------------------------------------------------------------------|
| `name`       | Stable identifier surfaced in startup logs and collision warnings.        |
| `priority`   | Integer; lower = runs earlier. Use module constants from `factories.priorities`. |
| `applies_to` | Tuple of `MessageContent` subclasses the transformer matches via MRO.     |

Plus a `transform(content, meta) -> Optional[MessageContent]` method
that returns a replacement (or `None` to pass through).

## Class-side format / title methods

Plugin-defined `MessageContent` subclasses carry their own
`format_markdown(self, renderer, message)`,
`format_html(self, renderer, message)`, and
`title(self, renderer, message)` methods. The renderer's
`_dispatch_format` walks the MRO and consults these methods after
exhausting the renderer-side `format_<ClassName>` chain (Strategy 2
in the RFC). Returning `None` from `format_html` falls back to
`mistune(format_markdown)` — consistent with the rest of the codebase.

## `depth_visibility`

A `ClassVar[RenderingDepth]` on the plugin's `MessageContent` subclass
declares the minimum detail level at which the message is rendered.
Monotone-down: a message is visible iff `current_detail` is at least
as verbose as `depth_visibility` (with `FULL` most verbose,
`USER_ONLY` least). Plugin classes that declare their own
`depth_visibility` bypass the orthogonal `_LOW_KEEP_TOOLS` tool-name
allowlist — their declared visibility is authoritative.

## Installing this fixture for tests

The test suite installs this package in editable mode via
`uv pip install -e test/_plugins/clmail` during pytest collection
(see `test/conftest.py`). Production installs of `claude-code-log`
don't see it; only CI and local test runs do.
