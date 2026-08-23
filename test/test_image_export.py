"""Tests for image_export.py."""

import pytest
from pathlib import Path

from claude_code_log.image_export import export_image
from claude_code_log.models import ImageContent, ImageSource


@pytest.fixture
def sample_image() -> ImageContent:
    """Create a sample ImageContent for testing."""
    # Minimal valid PNG: 1x1 transparent pixel
    png_data = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAA"
        "BJRU5ErkJggg=="
    )
    return ImageContent(
        type="image",
        source=ImageSource(type="base64", media_type="image/png", data=png_data),
    )


class TestExportImagePlaceholder:
    """Tests for placeholder mode."""

    def test_placeholder_returns_none(self, sample_image: ImageContent):
        """Placeholder mode returns None (caller renders placeholder text)."""
        result = export_image(sample_image, mode="placeholder")
        assert result is None


class TestExportImageEmbedded:
    """Tests for embedded mode."""

    def test_embedded_returns_data_url(self, sample_image: ImageContent):
        """Embedded mode returns data URL."""
        result = export_image(sample_image, mode="embedded")
        assert result is not None
        assert result.startswith("data:image/png;base64,")


class TestExportImageReferenced:
    """Tests for referenced mode."""

    def test_referenced_without_output_dir_returns_none(
        self, sample_image: ImageContent
    ):
        """Referenced mode without output_dir returns None."""
        result = export_image(sample_image, mode="referenced", output_dir=None)
        assert result is None

    def test_referenced_creates_image_file(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        """Referenced mode creates image file and returns relative path."""
        result = export_image(
            sample_image,
            mode="referenced",
            output_dir=tmp_path,
            counter=1,
        )

        assert result == "images/image_0001.png"
        assert (tmp_path / "images" / "image_0001.png").exists()

    def test_referenced_with_different_counter(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        """Referenced mode uses counter for filename."""
        result = export_image(
            sample_image,
            mode="referenced",
            output_dir=tmp_path,
            counter=42,
        )

        assert result == "images/image_0042.png"
        assert (tmp_path / "images" / "image_0042.png").exists()


class TestExportImageUnsupportedMode:
    """Tests for unsupported mode."""

    def test_unsupported_mode_returns_none(self, sample_image: ImageContent):
        """Unsupported mode returns None."""
        result = export_image(sample_image, mode="unknown_mode")
        assert result is None


# Minimal valid PNG (1x1 transparent pixel), shared by the security tests.
_VALID_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAA"
    "BJRU5ErkJggg=="
)


def _image(media_type: str, data: str = _VALID_PNG_B64) -> ImageContent:
    return ImageContent(
        type="image",
        source=ImageSource(type="base64", media_type=media_type, data=data),
    )


class TestExportImageSecurity:
    """Guards against the embedded/referenced-image XSS vector (issue #277).

    ``media_type`` and ``data`` are unvalidated strings parsed from
    transcript JSON, and can carry content that did not originate from
    the user (tool/MCP-returned images, fetched web content). Without
    an allowlist + base64 validation, a crafted media type could break
    out of the ``<img src>`` attribute.
    """

    def test_embedded_rejects_attribute_breakout_media_type(self):
        """A media type crafted to break out of the src attribute yields no data URL."""
        hostile = _image('png"><script>alert(document.domain)</script>')
        assert export_image(hostile, mode="embedded") is None

    def test_embedded_rejects_svg(self):
        """SVG is scriptable; embedded mode must not emit a data:image/svg+xml URL."""
        svg = _image("image/svg+xml")
        assert export_image(svg, mode="embedded") is None

    def test_embedded_rejects_invalid_base64(self):
        """Malformed base64 data is rejected rather than emitted verbatim."""
        bad = _image("image/png", data='not"base64><script>')
        assert export_image(bad, mode="embedded") is None

    def test_embedded_allows_valid_allowlisted_types(self):
        """The four allowlisted types with valid base64 still produce data URLs."""
        for mt in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            result = export_image(_image(mt), mode="embedded")
            assert result is not None, mt
            assert result.startswith(f"data:{mt};base64,")

    def test_referenced_rejects_svg(self, tmp_path: Path):
        """Referenced mode must not write a scriptable SVG to disk and link it."""
        svg = _image("image/svg+xml")
        result = export_image(svg, mode="referenced", output_dir=tmp_path, counter=1)
        assert result is None
        assert not (tmp_path / "images").exists() or not list(
            (tmp_path / "images").iterdir()
        )

    def test_referenced_rejects_invalid_base64(self, tmp_path: Path):
        """Referenced mode rejects malformed base64 instead of writing garbage."""
        bad = _image("image/png", data="!!!not-base64!!!")
        result = export_image(bad, mode="referenced", output_dir=tmp_path, counter=1)
        assert result is None

    def test_html_sink_escapes_hostile_media_type(self):
        """End-to-end: the HTML <img> sink never emits an unescaped breakout.

        Even if a future change let a hostile media type through
        export_image, the HTML formatter must escape the src so no
        ``">`` can close the attribute/tag. Here the hostile type is
        rejected upstream (rendering a placeholder), but we also assert
        no live ``<script>`` or attribute-closing ``">`` leaks.
        """
        from claude_code_log.html import format_image_content

        hostile = _image('png"><script>alert(1)</script>')
        html = format_image_content(hostile)
        assert "<script>" not in html
        assert '"><script' not in html
