"""YAML front matter at the start of a Markdown document.

A document such as an auto-memory file, a skill or a plan may open with a
YAML block between two ``---`` fences. Read as plain Markdown, the closing
fence is a setext underline and the whole block becomes one heading. Both
renderers take it out of the body instead and show it as data: the HTML
renderer through wenmode's ``frontmatter`` plugin, the Markdown renderer
through :func:`split_frontmatter`. The two share :func:`is_frontmatter_start`
and :func:`load_frontmatter`, so they agree on what is front matter and what
it holds.

Front matter in a transcript is untrusted: it is loaded with PyYAML's safe
loader only, aliases refused, and its values reach the page through the
same escaping as tool parameters.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any, Optional, cast

import yaml
from wenmode.plugins.frontmatter import FRONTMATTER_FENCE_RE

from .json_depth import exceeds_depth

# Lines as wenmode's block parser sees them: split on ``\n`` only, each
# keeping its terminator.
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+$")


@dataclass(frozen=True)
class RawFrontmatter:
    """A fenced block that does not load as a non-empty YAML mapping.

    It is shown as a YAML code block, so its content is never lost.
    """

    source: str


def _is_fence(line: str) -> bool:
    return FRONTMATTER_FENCE_RE.fullmatch(line) is not None


def _split_lines(text: str) -> tuple[list[str], Optional[int]]:
    """The text's lines and the index of the closing fence, if it has one.

    Mirrors wenmode's ``FrontmatterRule`` (an opening fence on the first
    line, the next fence closes it), with one more condition: the line
    after the opening fence must hold something. ``---`` followed by a
    blank line, or by a second ``---``, is a horizontal rule opening the
    text, not front matter.
    """
    if not text.startswith("---"):
        return [], None  # every body pays this check: keep it cheap
    lines = _LINE_RE.findall(text)
    if len(lines) < 3 or not _is_fence(lines[0]):
        return lines, None
    if not lines[1].strip() or _is_fence(lines[1]):
        return lines, None
    for index in range(2, len(lines)):
        if _is_fence(lines[index]):
            return lines, index
    return lines, None


def is_frontmatter_start(text: str) -> bool:
    """True when ``text`` opens with a front-matter block."""
    return _split_lines(text)[1] is not None


def split_frontmatter(text: str) -> Optional[tuple[str, str]]:
    """Split ``text`` into its front-matter source and the body after it.

    ``None`` when the text does not open with front matter.
    """
    lines, closing = _split_lines(text)
    if closing is None:
        return None
    return "".join(lines[1:closing]), "".join(lines[closing + 1 :])


class _NoAliasSafeLoader(yaml.SafeLoader):
    """PyYAML's safe loader, refusing aliases.

    An alias shares a node, so nested aliases describe a structure
    exponentially larger than its source; walking it to render a table
    would expand every reference.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):  # pyright: ignore[reportUnknownMemberType]
            raise yaml.YAMLError("aliases are not supported")
        return super().compose_node(parent, index)


# Bounds on what is loaded as a mapping; past them the block shows as raw
# YAML. PyYAML is pure Python here and recursive: a deeply nested block
# raises RecursionError, and a huge one costs seconds to load. Real front
# matter is a few lines deep and a few KiB long. The depth bound is the one
# every transcript JSON decode uses (``json_depth.MAX_DATA_DEPTH``): it also
# bounds the recursion of the params table that renders the result.
MAX_FRONTMATTER_BYTES = 64 * 1024


def _plain(value: Any) -> Any:
    """Turn a loaded YAML value into str/number/bool/None, lists and dicts.

    Only called on a value within the depth bound, so the recursion is
    bounded too.
    """
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        return {str(key): _plain(item) for key, item in mapping.items()}
    if isinstance(value, list):
        return [_plain(item) for item in cast("list[Any]", value)]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def load_frontmatter(source: str) -> "dict[str, Any] | RawFrontmatter":
    """Load front matter as a mapping, or keep it as raw YAML source.

    Anything but a non-empty mapping (a YAML error, a scalar, a list, an
    empty block) is returned as :class:`RawFrontmatter`, and so is a block
    past the size or depth bounds: a transcript must render whatever its
    front matter holds.
    """
    if len(source.encode("utf-8", "surrogatepass")) > MAX_FRONTMATTER_BYTES:
        return RawFrontmatter(source)
    try:
        value = yaml.load(source, Loader=_NoAliasSafeLoader)  # noqa: S506 - safe loader subclass
    except (yaml.YAMLError, RecursionError):
        return RawFrontmatter(source)
    if isinstance(value, dict) and value and not exceeds_depth(value):
        return _plain(value)
    return RawFrontmatter(source)


def yaml_fence(source: str) -> str:
    """``source`` as a fenced ``yaml`` code block that it cannot close early."""
    longest = max((len(run) for run in re.findall(r"`+", source)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}yaml\n{source.rstrip()}\n{fence}"
