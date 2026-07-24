"""Rich rendering for Codex's OpenAI Developer Docs MCP tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ..factories.priorities import TOOL_INPUT_GENERIC, TOOL_OUTPUT_GENERIC
from ..html.tool_formatters import render_params_table
from ..models import (
    MessageContent,
    MessageMeta,
    RenderingDepth,
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


def _input_params(content: ToolUseMessage) -> dict[str, str] | None:
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


def _result_body(content: ToolResultMessage) -> str | None:
    if content.is_error or not isinstance(content.output, ToolResultContent):
        return None
    body = content.output.content
    return body if isinstance(body, str) and body else None


def _search_input_params(content: ToolUseMessage) -> dict[str, Any] | None:
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


_SEARCH_HIT_START_RE = re.compile(r'\{\s*"url"\s*:')


def _search_hits(body: str) -> list[dict[str, Any]] | None:
    """Decode hits, salvaging complete objects from Codex-truncated JSON."""
    try:
        decoded: Any = json.loads(body)
    except (ValueError, RecursionError):
        decoded = None
    if isinstance(decoded, dict):
        hits_value = cast(dict[str, Any], decoded).get("hits")
        if isinstance(hits_value, list):
            return [
                cast(dict[str, Any], hit)
                for hit in cast(list[Any], hits_value)
                if isinstance(hit, dict)
            ]

    decoder = json.JSONDecoder()
    recovered: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for match in _SEARCH_HIT_START_RE.finditer(body):
        try:
            value, _ = decoder.raw_decode(body, match.start())
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, dict):
            continue
        hit = cast(dict[str, Any], value)
        url = hit.get("url")
        if isinstance(url, str) and url not in seen_urls:
            seen_urls.add(url)
            recovered.append(hit)
    return recovered or None


def _search_result_markdown(content: ToolResultMessage) -> str | None:
    body = _result_body(content)
    if body is None:
        return None
    if body == "[Output omitted by Codex truncation]":
        return body
    hits = _search_hits(body)
    if hits is None:
        return None

    rendered: list[str] = []
    for hit in hits:
        url = hit.get("url")
        if not isinstance(url, str) or not is_safe_web_url(url):
            continue
        hierarchy_value = hit.get("hierarchy")
        hierarchy = (
            cast(dict[str, Any], hierarchy_value)
            if isinstance(hierarchy_value, dict)
            else {}
        )
        hierarchy_parts = [
            value
            for level in range(7)
            if isinstance((value := hierarchy.get(f"lvl{level}")), str) and value
        ]
        label = " • ".join(hierarchy_parts) if hierarchy_parts else url
        rendered.append(
            f"### [{safe_markdown_inline(unescape(label))}]"
            f"({safe_markdown_link_target(url)})"
        )
        raw_content = hit.get("content")
        if isinstance(raw_content, str) and raw_content:
            rendered.extend(("", raw_content.strip()))
        rendered.extend(("", "---", ""))

    if not rendered:
        return "No documentation results."
    while rendered and not rendered[-1]:
        rendered.pop()
    if rendered and rendered[-1] == "---":
        rendered.pop()
    return "\n".join(rendered).rstrip()


@dataclass
class CodexDocInputMessage(ToolUseMessage):
    """Codex docs request with the URL and anchor kept prominent."""

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.AGENT

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

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.AGENT

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

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.AGENT

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

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.AGENT

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
    ) -> MessageContent | None:
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
    ) -> MessageContent | None:
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
    ) -> MessageContent | None:
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
    ) -> MessageContent | None:
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
