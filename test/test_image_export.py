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
    """Tests for referenced mode.

    Filenames are content-addressed (a digest of the decoded bytes), so a
    given image maps to exactly one file regardless of which render pass,
    run, or worker exports it. That property is what fixes the historical
    combined-vs-session name collision (a per-render counter assigned the
    same ``image_NNNN`` names to different images across the two passes)
    and what makes the mode safe under the render fan-out.
    """

    def test_referenced_without_output_dir_returns_none(
        self, sample_image: ImageContent
    ):
        """Referenced mode without output_dir returns None."""
        result = export_image(sample_image, mode="referenced", output_dir=None)
        assert result is None

    def test_referenced_creates_content_addressed_file(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        """Referenced mode writes the decoded bytes under a digest name."""
        import base64
        import re

        result = export_image(sample_image, mode="referenced", output_dir=tmp_path)

        assert result is not None
        assert re.fullmatch(r"images/image_[0-9a-f]{16}\.png", result)
        exported = tmp_path / result
        assert exported.read_bytes() == base64.b64decode(sample_image.source.data)

    def test_same_image_maps_to_same_file_across_calls(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        """Re-exporting an image is idempotent: same name, one file.

        This is the collision fix: the combined-page pass and the session
        pass both export the image, and both must land on the same file
        instead of the second pass overwriting the first's names.
        """
        first = export_image(sample_image, mode="referenced", output_dir=tmp_path)
        second = export_image(sample_image, mode="referenced", output_dir=tmp_path)

        assert first == second
        assert len(list((tmp_path / "images").iterdir())) == 1

    def test_distinct_images_get_distinct_names(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        """Different bytes never share a filename (the overwrite bug)."""
        import base64

        other_bytes = base64.b64decode(sample_image.source.data) + b"\x00"
        other = ImageContent(
            type="image",
            source=ImageSource(
                type="base64",
                media_type="image/png",
                data=base64.b64encode(other_bytes).decode("ascii"),
            ),
        )

        first = export_image(sample_image, mode="referenced", output_dir=tmp_path)
        second = export_image(other, mode="referenced", output_dir=tmp_path)

        assert first is not None and second is not None
        assert first != second
        assert len(list((tmp_path / "images").iterdir())) == 2

    def test_malformed_base64_degrades_to_none(self, tmp_path: Path):
        """Bad data returns None (placeholder) and leaves no temp litter."""
        bad = ImageContent(
            type="image",
            source=ImageSource(
                type="base64", media_type="image/png", data="%%%not-base64%%%"
            ),
        )

        result = export_image(bad, mode="referenced", output_dir=tmp_path)

        assert result is None
        images_dir = tmp_path / "images"
        assert not images_dir.exists() or not list(images_dir.iterdir())


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
        result = export_image(svg, mode="referenced", output_dir=tmp_path)
        assert result is None
        assert not (tmp_path / "images").exists() or not list(
            (tmp_path / "images").iterdir()
        )

    def test_referenced_rejects_invalid_base64(self, tmp_path: Path):
        """Referenced mode rejects malformed base64 instead of writing garbage."""
        bad = _image("image/png", data="!!!not-base64!!!")
        result = export_image(bad, mode="referenced", output_dir=tmp_path)
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

    _BREAKOUT_SRC = 'data:image/png;base64,AAAA"><script>alert(1)</script>'

    def test_assistant_sink_escapes_src(self, monkeypatch: pytest.MonkeyPatch):
        """The escaping layer on its own: with the gate bypassed, a quote in
        the src still cannot close the attribute."""
        import claude_code_log.html.assistant_formatters as formatters

        monkeypatch.setattr(
            formatters, "export_image", lambda *a, **k: self._BREAKOUT_SRC
        )
        html = formatters.format_image_content(_image("image/png"))
        assert "<script>" not in html
        assert 'src="data:image/png;base64,AAAA&quot;&gt;&lt;script&gt;' in html

    def test_renderer_sink_escapes_src(self, monkeypatch: pytest.MonkeyPatch):
        """Same for the renderer's export-mode-aware sink."""
        import claude_code_log.image_export as image_export
        from claude_code_log.html.renderer import HtmlRenderer

        monkeypatch.setattr(
            image_export, "export_image", lambda *a, **k: self._BREAKOUT_SRC
        )
        html = HtmlRenderer()._format_image(_image("image/png"))
        assert "<script>" not in html
        assert 'src="data:image/png;base64,AAAA&quot;&gt;&lt;script&gt;' in html


class TestReferencedModeAcrossRenderPasses:
    """Regression: the combined and per-session passes share one images/ dir.

    Under the old per-render counter, the combined pass named its images
    ``image_0001…N`` and each session pass restarted at ``image_0001`` —
    assigning the same names to *different* images, so whichever pass ran
    last overwrote the other's files and the survivor pages showed the
    wrong images. Content-addressed names make both passes converge on
    the same file per image.
    """

    @staticmethod
    def _image_entry(session_id: str, uuid: str, ts: str, png_b64: str) -> str:
        import json

        return json.dumps(
            {
                "type": "user",
                "timestamp": ts,
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/tmp",
                "sessionId": session_id,
                "version": "1.0.0",
                "uuid": uuid,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"screenshot in {session_id}"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": png_b64,
                            },
                        },
                    ],
                },
            }
        )

    # page_size=1 forces the paginated combined path, which used to drop
    # the image mode entirely (pages always embedded); 2000 keeps the
    # single-file combined path.
    @pytest.mark.parametrize("page_size", [2000, 1])
    def test_combined_and_session_files_reference_intact_images(
        self, sample_image: ImageContent, tmp_path: Path, page_size: int
    ):
        import base64
        import hashlib
        import re

        from claude_code_log.converter import convert_jsonl_to

        png_a = sample_image.source.data
        png_b = base64.b64encode(base64.b64decode(png_a) + b"\x00").decode("ascii")

        project = tmp_path / "project"
        project.mkdir()
        (project / "aaaaaaaa-0000-0000-0000-000000000001.jsonl").write_text(
            self._image_entry(
                "aaaaaaaa-0000-0000-0000-000000000001",
                "aaaaaaaa-0000-0000-0000-00000000000a",
                "2025-07-01T10:00:00Z",
                png_a,
            )
            + "\n",
            encoding="utf-8",
        )
        (project / "bbbbbbbb-0000-0000-0000-000000000002.jsonl").write_text(
            self._image_entry(
                "bbbbbbbb-0000-0000-0000-000000000002",
                "bbbbbbbb-0000-0000-0000-00000000000b",
                "2025-07-01T11:00:00Z",
                png_b,
            )
            + "\n",
            encoding="utf-8",
        )

        convert_jsonl_to(
            "html",
            project,
            image_export_mode="referenced",
            generate_individual_sessions=True,
            silent=True,
            page_size=page_size,
        )

        html_files = sorted(project.glob("*.html"))
        session_files = [f for f in html_files if f.name.startswith("session-")]
        combined_files = [f for f in html_files if f.name.startswith("combined")]
        assert combined_files and len(session_files) == 2

        src_re = re.compile(r"images/image_[0-9a-f]{16}\.png")
        referenced: set[str] = set()
        for html_file in html_files:
            srcs = set(src_re.findall(html_file.read_text(encoding="utf-8")))
            assert srcs, f"{html_file.name} references no exported images"
            referenced |= srcs

        # Two distinct images → two files, every reference resolvable, and
        # every file's bytes still match the digest in its own name (the
        # old bug left files whose content belonged to a different name).
        assert len(referenced) == 2
        for rel in referenced:
            exported = project / rel
            assert exported.exists(), f"{rel} referenced but missing"
            digest = hashlib.blake2b(exported.read_bytes(), digest_size=8).hexdigest()
            assert exported.name == f"image_{digest}.png"
