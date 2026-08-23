"""Image export utilities for Claude Code transcripts.

This module provides format-agnostic image export functionality that can be used
by both HTML and Markdown renderers.
"""

import base64
import binascii
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ImageContent


# Image media types we are willing to emit into a data: URL or write to
# disk. Deliberately excludes ``image/svg+xml`` — SVG can carry inline
# ``<script>`` and event handlers, so a data:image/svg+xml URL is a
# scriptable XSS vector when the generated page is opened under
# ``file://``. Mirrors the allowlist already enforced on the
# tool-result image path in ``html/tool_formatters.py`` (issue #277).
_ALLOWED_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)


def _is_safe_image_source(media_type: str, data: str) -> bool:
    """Whether an image's media type and base64 data are safe to emit.

    Guards the embedded (data: URL) and referenced (write-to-disk)
    paths against two problems:

    - a non-allowlisted / scriptable media type (notably SVG), and
    - malformed base64 (which could smuggle a raw ``"`` /  ``>`` past a
      naive interpolation, or corrupt the written file).

    Returns ``True`` only when the media type is allowlisted *and* the
    data is strictly-valid base64. Callers should fall back to a
    placeholder (``None``) otherwise. Note this does not, on its own,
    make the result safe to drop into HTML unescaped — the HTML sink
    must still ``escape_html`` the final ``src`` (issue #277).
    """
    if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
        return False
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def export_image(
    image: "ImageContent",
    mode: str,
    output_dir: Path | None = None,
    counter: int = 0,
) -> str | None:
    """Export image content and return the source URL/path.

    This is a format-agnostic function that handles image export logic
    and returns just the src. Callers format the result as HTML or Markdown.

    Args:
        image: ImageContent with base64-encoded image data
        mode: Export mode - "placeholder", "embedded", or "referenced"
        output_dir: Output directory for referenced images (required for "referenced" mode)
        counter: Image counter for generating unique filenames

    Returns:
        For "placeholder" mode: None (caller should render placeholder text)
        For "embedded" mode: data URL (e.g., "data:image/png;base64,...")
        For "referenced" mode: relative path (e.g., "images/image_0001.png")
        For unsupported mode: None
    """
    if mode == "placeholder":
        return None

    if mode == "embedded":
        # Reject scriptable / non-allowlisted media types and malformed
        # base64 before building the data: URL. Even with these guards
        # the HTML sink must still escape the returned src (issue #277);
        # returning None here degrades to a placeholder.
        if not _is_safe_image_source(image.source.media_type, image.source.data):
            return None
        return f"data:{image.source.media_type};base64,{image.source.data}"

    if mode == "referenced":
        if output_dir is None:
            return None

        # Same allowlist/validation as embedded mode: don't write a
        # scriptable or malformed image to disk and then reference it.
        if not _is_safe_image_source(image.source.media_type, image.source.data):
            return None

        try:
            # Create images subdirectory
            images_dir = output_dir / "images"
            images_dir.mkdir(exist_ok=True)

            # Generate filename based on media type
            ext = _get_extension(image.source.media_type)
            filename = f"image_{counter:04d}{ext}"
            filepath = images_dir / filename

            # Decode and write image
            image_data = base64.b64decode(image.source.data)
            filepath.write_bytes(image_data)

            return f"images/{filename}"
        except (OSError, binascii.Error, ValueError):
            # Graceful degradation: return None to trigger placeholder rendering
            # Covers: PermissionError (mkdir/write), disk full, malformed base64
            return None

    # Unsupported mode
    return None


def _get_extension(media_type: str) -> str:
    """Get file extension from media type.

    Only the allowlisted media types (``_ALLOWED_IMAGE_MEDIA_TYPES``)
    reach this in referenced mode; ``image/svg+xml`` is intentionally
    absent because SVG is rejected upstream as a scriptable XSS vector
    (issue #277).
    """
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    return ext_map.get(media_type, ".png")
