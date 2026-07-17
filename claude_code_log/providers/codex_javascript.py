"""Bounded JavaScript parsing for Codex ``exec`` tool wrappers.

Transcript JavaScript is untrusted input.  This module only builds a concrete
syntax tree; it never evaluates source code.  Callers receive no tree when the
source is malformed or exceeds the configured parsing limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript


MAX_SOURCE_BYTES = 64 * 1024
MAX_SYNTAX_NODES = 8_192

_JAVASCRIPT = Language(tree_sitter_javascript.language())


@dataclass(frozen=True)
class JavaScriptSyntax:
    """A JavaScript tree paired with the UTF-8 bytes that define its ranges."""

    source: bytes
    root: Node

    def text(self, node: Node) -> str:
        """Return the exact source represented by *node*."""
        return self.source[node.start_byte : node.end_byte].decode("utf-8")


def parse_javascript(
    source: str,
    *,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_syntax_nodes: int = MAX_SYNTAX_NODES,
) -> Optional[JavaScriptSyntax]:
    """Parse a bounded, syntactically complete JavaScript program."""
    encoded = source.encode("utf-8")
    if len(encoded) > max_source_bytes:
        return None

    root = Parser(_JAVASCRIPT).parse(encoded).root_node
    if root.has_error or _node_count_exceeds(root, max_syntax_nodes):
        return None
    return JavaScriptSyntax(source=encoded, root=root)


def _node_count_exceeds(root: Node, limit: int) -> bool:
    remaining = [root]
    count = 0
    while remaining:
        node = remaining.pop()
        count += 1
        if count > limit:
            return True
        remaining.extend(node.children)
    return False
