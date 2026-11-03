"""Image export utilities for Claude Code transcripts.

This module provides format-agnostic image export functionality that can be used
by both HTML and Markdown renderers.
"""

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ImageContent


def export_image(
    image: "ImageContent",
    mode: str,
    output_dir: Path | None = None,
) -> str | None:
    """Export image content and return the source URL/path.

    This is a format-agnostic function that handles image export logic
    and returns just the src. Callers format the result as HTML or Markdown.

    Referenced-mode filenames are content-addressed — a digest of the
    decoded image bytes — so a given image maps to exactly one file no
    matter which render pass, run, or worker process exports it. That is
    a correctness requirement, not a convenience: a project conversion
    renders every message twice (combined page + its session file), and
    the per-render counter this replaced assigned the same
    ``image_NNNN`` names to *different* images across those passes,
    silently overwriting one pass's files with the other's. Content
    addressing also makes the write idempotent (an existing file is
    already the right bytes) and safe under the render fan-out, where
    concurrent workers may export the same image: writers go through a
    unique temp file + atomic ``os.replace``, and racing writers replace
    identical content.

    Args:
        image: ImageContent with base64-encoded image data
        mode: Export mode - "placeholder", "embedded", or "referenced"
        output_dir: Output directory for referenced images (required for "referenced" mode)

    Returns:
        For "placeholder" mode: None (caller should render placeholder text)
        For "embedded" mode: data URL (e.g., "data:image/png;base64,...")
        For "referenced" mode: relative path (e.g., "images/image_2f7a91c4d3b8e6a0.png")
        For unsupported mode: None
    """
    if mode == "placeholder":
        return None

    if mode == "embedded":
        return f"data:{image.source.media_type};base64,{image.source.data}"

    if mode == "referenced":
        if output_dir is None:
            return None

        tmp_path: Path | None = None
        try:
            image_data = base64.b64decode(image.source.data)

            # Create images subdirectory
            images_dir = output_dir / "images"
            images_dir.mkdir(exist_ok=True)

            digest = hashlib.blake2b(image_data, digest_size=8).hexdigest()
            ext = _get_extension(image.source.media_type)
            filename = f"image_{digest}{ext}"
            filepath = images_dir / filename

            if not filepath.exists():
                tmp_path = images_dir / f".{filename}.{os.getpid()}.tmp"
                tmp_path.write_bytes(image_data)
                os.replace(tmp_path, filepath)
                tmp_path = None

            return f"images/{filename}"
        except (OSError, binascii.Error, ValueError):
            # Graceful degradation: return None to trigger placeholder rendering
            # Covers: PermissionError (mkdir/write), disk full, malformed base64
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return None

    # Unsupported mode
    return None


def _get_extension(media_type: str) -> str:
    """Get file extension from media type."""
    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }
    return ext_map.get(media_type, ".png")
