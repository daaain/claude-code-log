"""Normalization of Codex's private web-tool result serialization."""

from __future__ import annotations

import re

_SOURCE_REF_RE = re.compile(r"cite(?P<ref>turn\d+(?:search|view)\d+)")
_NUMERIC_CITE_RE = re.compile(r"cite\d+†(?P<label>.*?)", re.DOTALL)
_SOURCE_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(?P<title>[^\n]+?)\s*(?:\n[ \t]*)?"
    r"\((?P<url>https?://[^)\n]+)\)[ \t]*\n"
    r"(?=citeturn\d+(?:search|view)\d+)"
)
_FETCH_METADATA_RE = re.compile(
    r"Content type:\s*(?P<content_type>[^;\n]+);\s*"
    r"Source:\s*.*?;\s*"
    r"(?:Redirected to URL:\s*(?P<redirect>https?://[^;\n]+);\s*)?"
    r"Total lines:\s*(?P<lines>\d+)",
)
_LINE_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_])L\d+\s*:[ \t]?")
_SEPARATOR_RE = re.compile(r"-{40,}")
_WORD_LIMIT_RE = re.compile(r"[ \t]*\[wordlim:\s*\d+\][ \t]*", re.IGNORECASE)
_CRAWLED_RE = re.compile(r"Crawled:\s*([^;\n]+);\s*", re.IGNORECASE)


def normalize_codex_web_result(text: str) -> tuple[str, list[str]]:
    """Turn flattened Codex web output into Markdown plus reusable refs.

    Named ``turn…search/view…`` references identify whole result blocks and
    can be reused by later ``open``/``find`` actions. Numeric citations are
    page-local link indices without a URL mapping in rollout files, so their
    wrappers are discarded while their readable labels are retained.
    """

    refs: list[str] = []

    def source_header(match: re.Match[str]) -> str:
        title = match.group("title").strip().replace("[", r"\[").replace("]", r"\]")
        return f"## [{title}]({match.group('url')})\n\n"

    def source_ref(match: re.Match[str]) -> str:
        ref = match.group("ref")
        if ref not in refs:
            refs.append(ref)
        return ""

    def numeric_cite(match: re.Match[str]) -> str:
        return match.group("label").strip().replace("†", " — ")

    def fetch_metadata(match: re.Match[str]) -> str:
        parts = [f"Fetched {match.group('content_type').strip()}"]
        redirect = match.group("redirect")
        if redirect:
            parts.append(f"[canonical source]({redirect.strip()})")
        parts.append(f"{int(match.group('lines')):,} lines")
        return "> " + " · ".join(parts)

    markdown = _SEPARATOR_RE.sub("\n\n---\n\n", text)
    markdown = _SOURCE_HEADER_RE.sub(source_header, markdown)
    markdown = _SOURCE_REF_RE.sub(source_ref, markdown)
    markdown = _WORD_LIMIT_RE.sub("", markdown)
    markdown = _FETCH_METADATA_RE.sub(fetch_metadata, markdown)
    markdown = _CRAWLED_RE.sub(r"> Crawled \1\n\n", markdown)
    markdown = _NUMERIC_CITE_RE.sub(numeric_cite, markdown)
    markdown = _LINE_MARKER_RE.sub("\n", markdown)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip(), refs


__all__ = ["normalize_codex_web_result"]
