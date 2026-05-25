# Rendering: Future Work

This document captures potential improvements and future work for the rendering system.

---

## 1. Recursive Template Rendering

Currently, `HtmlRenderer._flatten_preorder()` flattens the message tree into a list for template rendering. The template uses a flat `{% for message in messages %}` loop with CSS class-based ancestry for JavaScript fold/unfold.

### Goal

Pass tree roots directly to the template and use a recursive macro:

```jinja2
{% macro render_message(message, html_content, depth=0) %}
<div class='message {{ message.css_class }}' data-depth='{{ depth }}'>
    <div class='content'>{{ html_content | safe }}</div>
    {% if message.children %}
    <div class='children'>
        {% for child, child_html in message.children_with_html %}
        {{ render_message(child, child_html, depth + 1) }}
        {% endfor %}
    </div>
    {% endif %}
</div>
{% endmacro %}

{% for root, root_html in roots_with_html %}
{{ render_message(root, root_html) }}
{% endfor %}
```

### Benefits

- **Simpler JavaScript**: Fold/unfold becomes trivial with nested DOM:
  ```javascript
  messageEl.querySelector('.children').style.display = 'none';
  ```
- **Natural nesting**: DOM structure mirrors logical tree structure
- **Elimination of flatten step**: One less transformation

### Migration Steps

1. Create recursive render macro
2. Update DOM structure to use nested `.children` divs
3. Migrate JavaScript fold/unfold to use nested DOM
4. Pass `root_messages` directly to template

### Considerations

- JavaScript fold/unfold currently relies on CSS class queries (`.message.${targetId}`)
- Changing DOM structure requires JS migration
- Current approach works correctly, so this is optional optimization

---

## 2. Visitor Pattern for Multi-Format Output

For cleaner multi-format support, consider a visitor pattern where each output format implements a visitor over the message tree.

### Current Approach

```python
class Renderer:
    def format_content(self, message) -> str:
        return self._dispatch_format(message.content)

class HtmlRenderer(Renderer):
    def format_SystemMessage(self, content) -> str:
        return format_system_content(content)

class MarkdownRenderer(Renderer):
    def format_SystemMessage(self, content) -> str:
        return f"## System\n{content.text}"
```

### Visitor Alternative

```python
class MessageVisitor(Protocol):
    def visit_system_message(self, content: SystemMessage) -> T: ...
    def visit_user_message(self, content: UserTextMessage) -> T: ...
    # ...

class HtmlVisitor(MessageVisitor[str]):
    def visit_system_message(self, content):
        return format_system_content(content)

class MarkdownVisitor(MessageVisitor[str]):
    def visit_system_message(self, content):
        return f"## System\n{content.text}"
```

The current dispatcher approach works well; the visitor pattern would mainly help if we add many more output formats.

---

## 3. Performance Optimization

Benchmarks (3.35s for 3917 messages) show adequate performance, but potential improvements:

### Template Caching

Jinja2 templates are already cached via `@lru_cache`. No action needed.

### Pygments Caching

Syntax highlighting is a significant portion of render time. Could cache highlighted code by content hash for repeated identical blocks.

### Parallel Rendering

`RenderingContext` is already designed for parallel-safe rendering. Could process multiple sessions in parallel with separate contexts.

---

## Related Documentation

- [dev-docs/rendering-architecture.md](../dev-docs/rendering-architecture.md) - Current architecture
- [dev-docs/message-hierarchy.md](../dev-docs/message-hierarchy.md) - Fold/unfold state machine
