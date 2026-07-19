"""Rich rendering for Codex's OpenAI Developer Docs MCP tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Optional

from ..factories.priorities import TOOL_INPUT_GENERIC, TOOL_OUTPUT_GENERIC
from ..html.tool_formatters import render_params_table
from ..models import (
    DetailLevel,
    MessageContent,
    MessageMeta,
    ToolResultContent,
    ToolResultMessage,
    ToolUseContent,
    ToolUseMessage,
)
from ..plugins import (
    MessageTransformer,
    render_markdown_collapsible,
    safe_markdown_inline,
    safe_markdown_link_target,
)
from ..utils import is_safe_web_url

if TYPE_CHECKING:
    from ..renderer import Renderer, TemplateMessage


TOOL_NAME = "CodexDoc"


def _input_params(content: ToolUseMessage) -> Optional[dict[str, str]]:
    if not isinstance(content.input, ToolUseContent):
        return None
    raw = content.input.input
    url = raw.get("url")
    anchor = raw.get("anchor")
    if not isinstance(url, str) or not is_safe_web_url(url):
        return None
    if anchor is not None and not isinstance(anchor, str):
        return None
    params = {"url": url}
    if anchor:
        params["anchor"] = anchor
    return params


def _result_body(content: ToolResultMessage) -> Optional[str]:
    if content.is_error or not isinstance(content.output, ToolResultContent):
        return None
    body = content.output.content
    return body if isinstance(body, str) and body else None


@dataclass
class CodexDocInputMessage(ToolUseMessage):
    """Codex docs request with the URL and anchor kept prominent."""

    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    def format_html(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return render_params_table(_input_params(self) or {})

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        params = _input_params(self)
        if params is None:
            return ""
        anchor = params.get("anchor", "")
        target = f"{params['url']}{anchor}"
        return f"[{safe_markdown_inline(target)}]({safe_markdown_link_target(target)})"

    def title(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return "📚 OpenAI Docs"


@dataclass
class CodexDocResultMessage(ToolResultMessage):
    """OpenAI documentation body rendered as collapsible Markdown."""

    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    @property
    def has_markdown(self) -> bool:
        return True

    def format_html(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        body = _result_body(self)
        if body is None:
            return ""
        return render_markdown_collapsible(
            raw_content=body,
            css_class="codex-doc-result",
            line_threshold=20,
            preview_line_count=5,
        )

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return _result_body(self) or ""

    def title(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return ""


class CodexDocInputTransformer:
    name: ClassVar[str] = "builtin.codex-doc.input"
    priority: ClassVar[int] = TOOL_INPUT_GENERIC - 499
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolUseMessage,)

    def transform(
        self, content: MessageContent, meta: MessageMeta
    ) -> Optional[MessageContent]:
        del meta
        if (
            not isinstance(content, ToolUseMessage)
            or content.tool_name != TOOL_NAME
            or _input_params(content) is None
        ):
            return None
        return CodexDocInputMessage(
            meta=content.meta,
            input=content.input,
            tool_use_id=content.tool_use_id,
            tool_name=content.tool_name,
            skill_body=content.skill_body,
        )


class CodexDocResultTransformer:
    name: ClassVar[str] = "builtin.codex-doc.result"
    priority: ClassVar[int] = TOOL_OUTPUT_GENERIC - 499
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolResultMessage,)

    def transform(
        self, content: MessageContent, meta: MessageMeta
    ) -> Optional[MessageContent]:
        del meta
        if (
            not isinstance(content, ToolResultMessage)
            or content.tool_name != TOOL_NAME
            or _result_body(content) is None
        ):
            return None
        return CodexDocResultMessage(
            meta=content.meta,
            tool_use_id=content.tool_use_id,
            output=content.output,
            is_error=content.is_error,
            tool_name=content.tool_name,
            file_path=content.file_path,
        )


def builtin_transformers() -> tuple[MessageTransformer, ...]:
    """Return fresh built-in transformer instances for loader reloads."""
    return (CodexDocInputTransformer(), CodexDocResultTransformer())


__all__ = [
    "CodexDocInputMessage",
    "CodexDocResultMessage",
    "builtin_transformers",
]
