"""Assert on what a rendered page's cards carry, not on its own code."""

import re

# Case-insensitive, and the end tag may carry anything up to `>`
# (`</script >`, `</SCRIPT foo>`), as browsers accept.
_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\b[^>]*>", re.IGNORECASE | re.DOTALL
)


def rendered_markup(html: str) -> str:
    """Return *html* without its ``<script>`` and ``<style>`` blocks.

    The page's own scripts and styles mention most class names and labels,
    so a bare ``"..." in html`` passes whether or not any card renders them.
    """
    return _SCRIPT_OR_STYLE.sub("", html)
