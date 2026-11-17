"""Image export utilities for Claude Code transcripts.

This module provides format-agnostic image export functionality that can be used
by both HTML and Markdown renderers.
"""

import base64
import binascii
import hashlib
import io
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from PIL import Image

    from .models import ImageContent


# Image media types we are willing to emit into a data: URL or write to
# disk. Deliberately excludes ``image/svg+xml`` — SVG can carry inline
# ``<script>`` and event handlers, so a data:image/svg+xml URL is a
# scriptable XSS vector when the generated page is opened under
# ``file://``. Shared by every image sink: user/assistant images here,
# tool-result images in ``html/tool_formatters.py``, and plugins via
# ``claude_code_log.plugins.is_safe_image_source`` (issue #277).
ALLOWED_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)


def is_safe_image_source(media_type: str, data: str) -> bool:
    """Whether an image's media type and base64 data are safe to emit.

    Guards the embedded (data: URL) and referenced (write-to-disk)
    paths, the tool-result image path, and plugin image sinks (it is
    re-exported from ``claude_code_log.plugins``) against two problems:

    - a non-allowlisted / scriptable media type (notably SVG), and
    - malformed base64 (which could smuggle a raw ``"`` /  ``>`` past a
      naive interpolation, or corrupt the written file).

    Returns ``True`` only when the media type is allowlisted *and* the
    data is strictly-valid base64. Callers should fall back to a
    placeholder (``None``) otherwise. Note this does not, on its own,
    make the result safe to drop into HTML unescaped — the HTML sink
    must still ``escape_html`` the final ``src`` (issue #277).
    """
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        return False
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Compressed referenced images (the HTML default)
# ---------------------------------------------------------------------------
#
# Inline base64 made image-heavy sessions' pages hundreds of MB: every
# screenshot the Read tool opened was embedded at full size, a third
# larger than its bytes, once in the combined page and again in its
# session file. HTML's referenced mode instead writes each image once,
# re-encoded as AVIF and capped in size, under a name derived from the
# *source* bytes and the encoder settings — so an image already exported
# by an earlier pass, run or worker is found without decoding it again,
# and changing a setting below renames every file rather than leaving
# files encoded under the old one behind a name that says otherwise.

#: Longest side, in pixels, an exported image is scaled down to.
MAX_IMAGE_DIMENSION = 2000
AVIF_QUALITY = 50
AVIF_SPEED = 8
#: Used only where Pillow was built without AVIF support.
WEBP_QUALITY = 75
WEBP_METHOD = 4
#: Bump to rename every compressed image (e.g. after an encoder change
#: that the settings above don't capture). 2: colour-keyed transparency
#: kept as alpha.
_ENCODING_REVISION = 2


@dataclass(frozen=True)
class ExportedImage:
    """Where a compressed export landed, and its pixel size if known.

    ``src`` is relative to the page's output directory. ``width`` and
    ``height`` are the displayed (EXIF-oriented, scaled) size, given to
    the ``<img>`` so the page reserves the box before the lazily-loaded
    image arrives; ``None`` when Pillow could not read the image.
    """

    src: str
    width: Optional[int] = None
    height: Optional[int] = None


_encoder_cache: list[Optional[tuple[str, str]]] = []


def _encoder() -> Optional[tuple[str, str]]:
    """``(Pillow format, extension)`` to encode with, or None for none.

    AVIF where this Pillow build has it (the PyPI wheels do from 11.3),
    WebP otherwise, and None (images are written as they came) if
    neither is available.
    """
    if not _encoder_cache:
        choice: Optional[tuple[str, str]] = None
        try:
            from PIL import features

            if features.check("avif"):
                choice = ("AVIF", ".avif")
            elif features.check("webp"):
                choice = ("WEBP", ".webp")
        except Exception:
            choice = None
        _encoder_cache.append(choice)
    return _encoder_cache[0]


def _encoding_tag() -> bytes:
    encoder = _encoder()
    fmt = encoder[0] if encoder else "none"
    if fmt == "AVIF":
        settings = f"q{AVIF_QUALITY}-s{AVIF_SPEED}"
    elif fmt == "WEBP":
        settings = f"q{WEBP_QUALITY}-m{WEBP_METHOD}"
    else:
        settings = ""
    return (f"{fmt}-{settings}-max{MAX_IMAGE_DIMENSION}-r{_ENCODING_REVISION}").encode()


def _oriented_size(im: "Image.Image") -> tuple[int, int]:
    """The size a browser displays *im* at, honouring EXIF orientation."""
    width, height = im.size
    try:
        orientation = im.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def _compress(
    raw: bytes, media_type: str
) -> tuple[bytes, str, Optional[int], Optional[int]]:
    """Re-encode *raw*: ``(bytes, extension, width, height)``.

    Falls back to the original bytes and extension whenever re-encoding
    would not help — an animated image, an encoding no smaller than the
    source, no encoder — or Pillow cannot read the image at all (then
    without dimensions).
    """
    original_ext = _get_extension(media_type)
    try:
        from PIL import Image, ImageOps

        im = Image.open(io.BytesIO(raw))
        with im:
            size = _oriented_size(im)
            encoder = _encoder()
            if encoder is None or getattr(im, "is_animated", False):
                return raw, original_ext, size[0], size[1]
            img = ImageOps.exif_transpose(im)
        if "transparency" in img.info:
            # A colour key (PNG tRNS) on an RGB, L or P image. AVIF has no
            # colour key, and Pillow's encoder keeps an RGB or L image's
            # mode, so the keyed pixels would turn opaque: make the key a
            # real alpha channel first.
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
        fmt, ext = encoder
        buf = io.BytesIO()
        if fmt == "AVIF":
            options: dict[str, object] = {"quality": AVIF_QUALITY, "speed": AVIF_SPEED}
            if media_type != "image/jpeg" and img.mode in ("RGB", "RGBA"):
                # Lossless sources are mostly screenshots: full chroma
                # keeps coloured text sharp, for ~1% more bytes.
                options["subsampling"] = "4:4:4"
        else:
            options = {"quality": WEBP_QUALITY, "method": WEBP_METHOD}
        img.save(buf, fmt, **options)
        if buf.tell() >= len(raw):
            return raw, original_ext, size[0], size[1]
        return buf.getvalue(), ext, img.width, img.height
    except Exception:
        # Pillow raises a variety of errors on undecodable or hostile
        # input (DecompressionBombError among them); the image still
        # gets exported, as it came.
        return raw, original_ext, None, None


def _image_size_on_disk(path: Path) -> tuple[Optional[int], Optional[int]]:
    """The displayed size of an already-exported file, from its header."""
    try:
        from PIL import Image

        im = Image.open(path)
        with im:
            return _oriented_size(im)
    except Exception:
        return None, None


def _install(tmp_path: Path, path: Path) -> None:
    """``os.replace(tmp_path, path)``, allowing for a concurrent writer.

    An exported image's name is its content's digest, so once ``path``
    exists another writer has put exactly these bytes there. On Windows,
    that writer's replace can refuse ours (PermissionError, WinError 5)
    while it lands: then the job is done, and our temp file is removed.
    Until ``path`` exists the replace is retried briefly
    (`utils.retry_on_sharing_violation`), and its last error propagates.
    """
    from .utils import retry_on_sharing_violation

    def replace() -> None:
        try:
            os.replace(tmp_path, path)
        except PermissionError:
            try:
                landed = path.exists()
            except OSError:  # mid-swap, even a stat can be refused
                landed = False
            if not landed:
                raise
            tmp_path.unlink(missing_ok=True)

    retry_on_sharing_violation(replace)


def _temp_name(path: Path) -> Path:
    # Unique per process *and* thread: render workers and a watch loop's
    # threads may export the same image at once.
    return path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    # Racing writers replace identical bytes (see `_install`).
    tmp_path = _temp_name(path)
    try:
        tmp_path.write_bytes(data)
        _install(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# (images dir, digest) -> result, so the second render pass over a
# message (combined page, then session file) costs one stat, not a
# header parse. Bounded so a long ``serve --watch`` can't grow it without
# limit.
_exported: dict[tuple[Path, str], ExportedImage] = {}
_exported_lock = threading.Lock()
_EXPORTED_MAX = 65536


def export_compressed_image(
    media_type: str, data: str, output_dir: Path
) -> Optional[ExportedImage]:
    """Write a base64 image as a compressed, content-addressed file.

    The file goes to ``<output_dir>/images/image_<digest><ext>``, where
    the digest covers the decoded source bytes and the encoder settings
    and ``<ext>`` is the encoder's (``.avif``) or, when re-encoding was
    declined, the source's own. Returns None — render a placeholder —
    for an unsafe or malformed source or when the file can't be written.
    """
    if not is_safe_image_source(media_type, data):
        return None
    try:
        raw = base64.b64decode(data)
    except (binascii.Error, ValueError):
        return None
    hasher = hashlib.blake2b(_encoding_tag(), digest_size=8)
    hasher.update(b"\0")
    hasher.update(raw)
    digest = hasher.hexdigest()
    images_dir = output_dir / "images"
    key = (images_dir, digest)

    with _exported_lock:
        known = _exported.get(key)
    if known is not None and (output_dir / known.src).exists():
        return known

    stem = f"image_{digest}"
    encoder = _encoder()
    candidates = ([stem + encoder[1]] if encoder else []) + [
        stem + _get_extension(media_type)
    ]
    result: Optional[ExportedImage] = None
    for name in candidates:
        path = images_dir / name
        if path.exists():
            width, height = _image_size_on_disk(path)
            result = ExportedImage(f"images/{name}", width, height)
            break
    if result is None:
        payload, ext, width, height = _compress(raw, media_type)
        name = stem + ext
        try:
            images_dir.mkdir(exist_ok=True)
            _atomic_write(images_dir / name, payload)
        except OSError:
            return None
        result = ExportedImage(f"images/{name}", width, height)

    with _exported_lock:
        if len(_exported) >= _EXPORTED_MAX:
            _exported.clear()
        _exported[key] = result
    return result


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
    identical content (on Windows, one may refuse another's: `_install`).

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
        # Reject scriptable / non-allowlisted media types and malformed
        # base64 before building the data: URL. Even with these guards
        # the HTML sink must still escape the returned src (issue #277);
        # returning None here degrades to a placeholder.
        if not is_safe_image_source(image.source.media_type, image.source.data):
            return None
        return f"data:{image.source.media_type};base64,{image.source.data}"

    if mode == "referenced":
        if output_dir is None:
            return None

        # Same allowlist/validation as embedded mode: don't write a
        # scriptable or malformed image to disk and then reference it.
        if not is_safe_image_source(image.source.media_type, image.source.data):
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
                tmp_path = _temp_name(filepath)
                tmp_path.write_bytes(image_data)
                _install(tmp_path, filepath)
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
    """Get file extension from media type.

    Only the allowlisted media types (``ALLOWED_IMAGE_MEDIA_TYPES``)
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
