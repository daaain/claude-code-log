"""Rich rendering for Codex's OpenAI Developer Docs MCP tool."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import re
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

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
SEARCH_TOOL_NAME = "CodexDocSearch"


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


def _search_input_params(content: ToolUseMessage) -> Optional[dict[str, Any]]:
    if not isinstance(content.input, ToolUseContent):
        return None
    raw = content.input.input
    query = raw.get("query")
    limit = raw.get("limit")
    if not isinstance(query, str) or not query:
        return None
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
    ):
        return None
    params: dict[str, Any] = {"query": query}
    if limit is not None:
        params["limit"] = limit
    return params


_SEARCH_HIGHLIGHT_TAG_RE = re.compile(r"</?span(?:\s[^>]*)?>", re.IGNORECASE)


def _search_result_markdown(content: ToolResultMessage) -> Optional[str]:
    body = _result_body(content)
    if body is None:
        return None
    if body == "[Output omitted by Codex truncation]":
        return body
    try:
        decoded: Any = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    hits_value = cast(dict[str, Any], decoded).get("hits")
    if not isinstance(hits_value, list):
        return None

    rendered: list[str] = []
    for item in cast(list[Any], hits_value):
        if not isinstance(item, dict):
            continue
        hit = cast(dict[str, Any], item)
        url = hit.get("url")
        if not isinstance(url, str) or not is_safe_web_url(url):
            continue
        hierarchy_value = hit.get("hierarchy")
        hierarchy = (
            cast(dict[str, Any], hierarchy_value)
            if isinstance(hierarchy_value, dict)
            else {}
        )
        title_value = hierarchy.get("lvl1")
        section_value = hierarchy.get("lvl2")
        title = title_value if isinstance(title_value, str) else url
        label = (
            f"{title} — {section_value}"
            if isinstance(section_value, str) and section_value
            else title
        )
        rendered.append(
            f"- [{safe_markdown_inline(unescape(label))}]"
            f"({safe_markdown_link_target(url)})"
        )

        snippet_value = hit.get("_snippetResult")
        snippet = ""
        if isinstance(snippet_value, dict):
            snippet_content = cast(dict[str, Any], snippet_value).get("content")
            if isinstance(snippet_content, dict):
                raw_snippet = cast(dict[str, Any], snippet_content).get("value")
                if isinstance(raw_snippet, str):
                    snippet = raw_snippet
        if not snippet:
            raw_content = hit.get("content")
            if isinstance(raw_content, str):
                snippet = raw_content[:300]
        if snippet:
            snippet = unescape(_SEARCH_HIGHLIGHT_TAG_RE.sub("", snippet))
            snippet = " ".join(snippet.split())
            rendered.append(f"  {safe_markdown_inline(snippet)}")

    return "\n".join(rendered) if rendered else "No documentation results."


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


@dataclass
class CodexDocSearchInputMessage(ToolUseMessage):
    """OpenAI documentation search with its query kept prominent."""

    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    def format_html(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return render_params_table(_search_input_params(self) or {})

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        params = _search_input_params(self)
        if params is None:
            return ""
        lines = [f"Query: {safe_markdown_inline(cast(str, params['query']))}"]
        if "limit" in params:
            lines.append(f"Limit: {params['limit']}")
        return "\n".join(lines)

    def title(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return "🔎 OpenAI Docs"


@dataclass
class CodexDocSearchResultMessage(ToolResultMessage):
    """OpenAI documentation search hits rendered as a compact link list."""

    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    @property
    def has_markdown(self) -> bool:
        return True

    def format_html(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        markdown = _search_result_markdown(self)
        if markdown is None:
            return ""
        return render_markdown_collapsible(
            raw_content=markdown,
            css_class="codex-doc-search-result",
            line_threshold=12,
            preview_line_count=6,
        )

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return _search_result_markdown(self) or ""

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


class CodexDocSearchInputTransformer:
    name: ClassVar[str] = "builtin.codex-doc-search.input"
    priority: ClassVar[int] = TOOL_INPUT_GENERIC - 498
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolUseMessage,)

    def transform(
        self, content: MessageContent, meta: MessageMeta
    ) -> Optional[MessageContent]:
        del meta
        if (
            not isinstance(content, ToolUseMessage)
            or content.tool_name != SEARCH_TOOL_NAME
            or _search_input_params(content) is None
        ):
            return None
        return CodexDocSearchInputMessage(
            meta=content.meta,
            input=content.input,
            tool_use_id=content.tool_use_id,
            tool_name=content.tool_name,
            skill_body=content.skill_body,
        )


class CodexDocSearchResultTransformer:
    name: ClassVar[str] = "builtin.codex-doc-search.result"
    priority: ClassVar[int] = TOOL_OUTPUT_GENERIC - 498
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolResultMessage,)

    def transform(
        self, content: MessageContent, meta: MessageMeta
    ) -> Optional[MessageContent]:
        del meta
        if (
            not isinstance(content, ToolResultMessage)
            or content.tool_name != SEARCH_TOOL_NAME
            or _search_result_markdown(content) is None
        ):
            return None
        return CodexDocSearchResultMessage(
            meta=content.meta,
            tool_use_id=content.tool_use_id,
            output=content.output,
            is_error=content.is_error,
            tool_name=content.tool_name,
            file_path=content.file_path,
        )


def builtin_transformers() -> tuple[MessageTransformer, ...]:
    """Return fresh built-in transformer instances for loader reloads."""
    return (
        CodexDocInputTransformer(),
        CodexDocResultTransformer(),
        CodexDocSearchInputTransformer(),
        CodexDocSearchResultTransformer(),
    )


__all__ = [
    "CodexDocInputMessage",
    "CodexDocResultMessage",
    "CodexDocSearchInputMessage",
    "CodexDocSearchResultMessage",
    "builtin_transformers",
]
