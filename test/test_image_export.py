"""Tests for image_export.py."""

import pytest
from pathlib import Path
from typing import cast

from claude_code_log.image_export import (
    _encoding_tag,
    export_compressed_image,
    export_image,
)
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

        # A 1x1 PNG is smaller than any re-encoding, so it is kept as-is.
        src_re = re.compile(r"images/image_[0-9a-f]{16}\.png")
        referenced: set[str] = set()
        for html_file in html_files:
            srcs = set(src_re.findall(html_file.read_text(encoding="utf-8")))
            assert srcs, f"{html_file.name} references no exported images"
            referenced |= srcs

        # Two distinct images → two files, every reference resolvable, and
        # every file holds the image its name was derived from (the old bug
        # left files whose content belonged to a different name). Names
        # hash the source bytes under the encoder settings.
        assert len(referenced) == 2
        sources = [base64.b64decode(png_a), base64.b64decode(png_b)]
        for rel in referenced:
            exported = project / rel
            assert exported.exists(), f"{rel} referenced but missing"
            content = exported.read_bytes()
            assert content in sources
            hasher = hashlib.blake2b(_encoding_tag(), digest_size=8)
            hasher.update(b"\0")
            hasher.update(content)
            assert exported.name == f"image_{hasher.hexdigest()}.png"


def _b64_image(fmt: str, size: tuple[int, int], **save: object) -> str:
    """A noisy (incompressible-ish) image of *size*, base64-encoded."""
    import base64
    import io
    import random

    from PIL import Image

    rng = random.Random(f"{size}")
    img = Image.new("RGB", size)
    img.putdata(
        [
            (rng.randrange(256), (x * 7) % 256, (x * 3) % 256)
            for x in range(size[0] * size[1])
        ]
    )
    buf = io.BytesIO()
    img.save(buf, fmt, **save)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class TestExportCompressedImage:
    """HTML's referenced mode: compressed, capped, content-addressed files."""

    def test_large_image_is_capped_and_reencoded(self, tmp_path: Path):
        from claude_code_log.image_export import (
            MAX_IMAGE_DIMENSION,
            _encoder,
            export_compressed_image,
        )

        encoder = _encoder()
        assert encoder is not None, "the Pillow wheel ships AVIF and WebP"
        data = _b64_image("PNG", (2400, 600))
        result = export_compressed_image("image/png", data, tmp_path)
        assert result is not None
        assert result.src.endswith(encoder[1])
        assert (result.width, result.height) == (MAX_IMAGE_DIMENSION, 500)
        written = tmp_path / result.src
        assert written.stat().st_size < len(data) * 3 // 4

    def test_second_export_reuses_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import claude_code_log.image_export as image_export

        data = _b64_image("PNG", (300, 200))
        first = image_export.export_compressed_image("image/png", data, tmp_path)
        assert first is not None
        inode = (tmp_path / first.src).stat().st_ino
        # A fresh process (empty memo) finds the file on disk, with its size,
        # without encoding the source again.
        image_export._exported.clear()

        def no_encode(*_args: object) -> None:
            raise AssertionError("re-encoded an exported image")

        monkeypatch.setattr(image_export, "_compress", no_encode)
        second = image_export.export_compressed_image("image/png", data, tmp_path)
        assert second == first
        assert (tmp_path / first.src).stat().st_ino == inode
        assert len(list((tmp_path / "images").iterdir())) == 1

    def test_tiny_image_keeps_its_original_bytes(
        self, sample_image: ImageContent, tmp_path: Path
    ):
        import base64

        from claude_code_log.image_export import export_compressed_image

        result = export_compressed_image(
            "image/png", sample_image.source.data, tmp_path
        )
        assert result is not None
        assert result.src.endswith(".png")
        assert (result.width, result.height) == (1, 1)
        assert (tmp_path / result.src).read_bytes() == base64.b64decode(
            sample_image.source.data
        )

    def test_exif_rotated_jpeg_reports_displayed_size(self, tmp_path: Path):
        from PIL import Image

        from claude_code_log.image_export import export_compressed_image

        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° CW on display
        data = _b64_image("JPEG", (400, 100), exif=exif.tobytes())
        result = export_compressed_image("image/jpeg", data, tmp_path)
        assert result is not None
        assert (result.width, result.height) == (100, 400)

    @pytest.mark.parametrize("mode", ["RGB", "L"])
    def test_colour_key_transparency_survives_reencoding(
        self, tmp_path: Path, mode: str
    ):
        import base64
        import io
        import random

        from PIL import Image

        from claude_code_log.image_export import _encoder, export_compressed_image

        encoder = _encoder()
        assert encoder is not None
        # Left half noise (so the re-encode beats the PNG), right half the
        # key colour, which a PNG tRNS chunk marks transparent.
        rng = random.Random("tRNS")
        key = (0, 255, 0) if mode == "RGB" else 255
        img = Image.new(mode, (300, 200))
        img.putdata(
            [
                key
                if i % 300 >= 150
                else (
                    (rng.randrange(255), 0, 0) if mode == "RGB" else rng.randrange(255)
                )
                for i in range(300 * 200)
            ]
        )
        buf = io.BytesIO()
        img.save(buf, "PNG", transparency=key)
        data = base64.b64encode(buf.getvalue()).decode("ascii")

        result = export_compressed_image("image/png", data, tmp_path)
        assert result is not None
        assert result.src.endswith(encoder[1]), "expected a re-encode, not the source"
        with Image.open(tmp_path / result.src) as out:
            alpha = out.convert("RGBA").getchannel("A")
            assert cast(int, alpha.getpixel((250, 100))) < 16  # keyed: transparent
            assert cast(int, alpha.getpixel((50, 100))) > 240  # noise: opaque

    def test_animated_gif_is_kept_as_is(self, tmp_path: Path):
        import base64
        import io

        from PIL import Image

        from claude_code_log.image_export import export_compressed_image

        frames = [Image.new("RGB", (64, 64), c) for c in ("red", "blue")]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        result = export_compressed_image("image/gif", data, tmp_path)
        assert result is not None
        assert result.src.endswith(".gif")
        assert (result.width, result.height) == (64, 64)

    def test_undecodable_image_is_written_without_size(self, tmp_path: Path):
        import base64

        from claude_code_log.image_export import export_compressed_image

        data = base64.b64encode(b"not really a png").decode("ascii")
        result = export_compressed_image("image/png", data, tmp_path)
        assert result is not None
        assert result.src.endswith(".png")
        assert (result.width, result.height) == (None, None)
        assert (tmp_path / result.src).read_bytes() == b"not really a png"

    def test_unsafe_sources_are_refused(self, tmp_path: Path):
        import base64

        from claude_code_log.image_export import export_compressed_image

        svg = base64.b64encode(b"<svg onload='alert(1)'/>").decode("ascii")
        assert export_compressed_image("image/svg+xml", svg, tmp_path) is None
        assert export_compressed_image("image/png", "@@not base64@@", tmp_path) is None
        assert not (tmp_path / "images").exists()


class TestOnlyTheDeclaredDecoder:
    """Pillow decodes an image only as the format its media type declares,
    so hostile bytes never reach its other decoders (TIFF, FlashPix, ...)."""

    def test_a_tiff_declared_as_png_is_not_decoded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import base64
        import io

        from PIL import Image

        import claude_code_log.image_export as image_export

        buf = io.BytesIO()
        Image.new("RGB", (300, 200), "red").save(buf, "TIFF")
        raw = buf.getvalue()
        opened: list[str] = []
        real_open = Image.open

        def spy(fp: object, mode: str = "r", formats: object = None) -> object:
            image = real_open(fp, mode, formats)  # type: ignore[arg-type]
            opened.append(image.format or "")
            return image

        monkeypatch.setattr(Image, "open", spy)
        result = image_export.export_compressed_image(
            "image/png", base64.b64encode(raw).decode("ascii"), tmp_path
        )

        assert opened == []
        assert result is not None and result.width is None
        assert (tmp_path / result.src).read_bytes() == raw

    def test_the_declared_format_still_compresses(self, tmp_path: Path):
        from claude_code_log.image_export import export_compressed_image

        result = export_compressed_image(
            "image/png", _b64_image("PNG", (300, 200)), tmp_path
        )
        assert result is not None and result.width == 300


class TestHtmlRendererImageModes:
    """The HTML renderer's images, user and tool-result alike, follow the mode."""

    @staticmethod
    def _tool_result_html(renderer: object) -> str:
        from claude_code_log.models import ToolResultContent

        result = ToolResultContent(
            type="tool_result",
            tool_use_id="t1",
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _b64_image("PNG", (320, 240)),
                    },
                }
            ],
        )
        return renderer.format_ToolResultContent(result, None)  # type: ignore[attr-defined]

    def test_referenced_tool_result_image_is_a_lazy_sized_file(self, tmp_path: Path):
        import re

        from claude_code_log.html.renderer import HtmlRenderer

        renderer = HtmlRenderer()  # referenced is the default
        renderer._output_dir = tmp_path
        html = self._tool_result_html(renderer)
        match = re.search(r'<img src="(images/image_[0-9a-f]{16}\.\w+)"', html)
        assert match, html
        assert (tmp_path / match.group(1)).exists()
        assert 'width="320" height="240"' in html
        assert 'loading="lazy"' in html
        assert 'class="tool-result-image"' in html
        assert "data:image" not in html

    def test_referenced_without_output_dir_embeds(self):
        from claude_code_log.html.renderer import HtmlRenderer

        html = self._tool_result_html(HtmlRenderer())
        assert 'src="data:image/png;base64,' in html

    def test_placeholder_marks_tool_result_images(self, tmp_path: Path):
        from claude_code_log.html.renderer import HtmlRenderer

        renderer = HtmlRenderer(image_export_mode="placeholder")
        renderer._output_dir = tmp_path
        html = self._tool_result_html(renderer)
        assert "[Image]" in html and "<img" not in html


class TestInlineDataImagesInText:
    """A ``data:`` image written into Markdown text (a fetched page, a pasted
    snippet) is exported like any other image in referenced mode."""

    @staticmethod
    def _page(tmp_path: Path, mode: str) -> str:
        import json

        from claude_code_log.converter import convert_jsonl_to

        data = _b64_image("PNG", (300, 200))
        entry = {
            "type": "assistant",
            "timestamp": "2025-07-01T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": "s1",
            "version": "2.1.0",
            "uuid": "a1",
            "requestId": "req_1",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "text",
                        "text": f"The logo: ![logo](data:image/png;base64,{data})",
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        transcript = tmp_path / "s1.jsonl"
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        page = convert_jsonl_to(
            "html", transcript, use_cache=False, silent=True, image_export_mode=mode
        )
        return Path(page).read_text(encoding="utf-8")

    def test_referenced_moves_it_into_a_file(self, tmp_path: Path):
        import re

        html = self._page(tmp_path, "referenced")
        match = re.search(
            r'<img src="(images/image_[0-9a-f]{16}\.\w+)" width="300" '
            r'height="200" loading="lazy" decoding="async" alt="logo"',
            html,
        )
        assert match, re.findall(r"<img[^>]*>", html)
        assert (tmp_path / match.group(1)).exists()
        assert "data:image/png" not in html

    def test_embedded_keeps_it_inline(self, tmp_path: Path):
        html = self._page(tmp_path, "embedded")
        assert '<img src="data:image/png;base64,' in html
        assert not (tmp_path / "images").exists()


# ---------------------------------------------------------------------------
# Concurrent writers on Windows
# ---------------------------------------------------------------------------


def _export(writer: str, data: str, output_dir: Path) -> "str | None":
    """Export *data* (a PNG) with one of the two referenced-mode writers."""
    if writer == "compressed":
        exported = export_compressed_image("image/png", data, output_dir)
        return None if exported is None else exported.src
    return export_image(_image("image/png", data), "referenced", output_dir)


@pytest.mark.usefixtures("windows_retries")
@pytest.mark.parametrize("writer", ["compressed", "plain"])
class TestConcurrentWriters:
    """Render workers and watch threads may export one image at once.

    Image files are named after their content, so whichever writer lands
    first writes exactly the bytes the others would. On Windows, though,
    ``os.replace`` onto a file another process is replacing at that moment
    raises PermissionError (WinError 5), which used to read as an
    unwritable folder: the image rendered as a placeholder. Simulated
    here, as the suite doesn't run on the platform that does it.
    """

    def _data(self) -> str:
        # Big enough that the compressed writer re-encodes it.
        return _b64_image("PNG", (64, 64))

    def _assert_no_temp_files(self, output_dir: Path) -> None:
        images = output_dir / "images"
        assert [p.name for p in images.iterdir() if p.name.startswith(".")] == []

    def test_losing_the_race_to_another_writer_is_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
    ):
        import os
        import shutil

        calls: list[Path] = []

        def winner_got_there_first(src: Path, dst: Path) -> None:
            calls.append(Path(dst))
            shutil.copyfile(src, dst)  # the other writer's identical bytes
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(os, "replace", winner_got_there_first)
        src = _export(writer, self._data(), tmp_path)
        assert src is not None and (tmp_path / src).is_file()
        assert len(calls) == 1  # accepted at once, no retry spent
        self._assert_no_temp_files(tmp_path)

    def test_a_transient_refusal_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
    ):
        import os

        real_replace = os.replace
        refused: list[Path] = []

        def busy_once(src: Path, dst: Path) -> None:
            if not refused:
                refused.append(Path(dst))
                raise PermissionError(13, "Access is denied")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", busy_once)
        src = _export(writer, self._data(), tmp_path)
        assert src is not None and (tmp_path / src).is_file()
        self._assert_no_temp_files(tmp_path)

    def test_a_refusal_that_never_clears_still_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
    ):
        import os

        calls: list[int] = []

        def always_denied(src: Path, dst: Path) -> None:
            calls.append(1)
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(os, "replace", always_denied)
        assert _export(writer, self._data(), tmp_path) is None
        assert len(calls) == 10  # the retry budget, then a placeholder
        assert list((tmp_path / "images").iterdir()) == []


# ---------------------------------------------------------------------------
# Removing images no page shows
# ---------------------------------------------------------------------------


class TestPruneUnreferencedImages:
    @staticmethod
    def _image(output_dir: Path, digest: str, ext: str = ".avif", age: float = 7200):
        import os
        import time

        images = output_dir / "images"
        images.mkdir(exist_ok=True)
        path = images / f"image_{digest}{ext}"
        path.write_bytes(b"x")
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def test_keeps_what_a_page_shows_and_removes_the_rest(self, tmp_path: Path):
        from claude_code_log.image_export import prune_unreferenced_images

        shown = self._image(tmp_path, "a" * 16)
        in_markdown = self._image(tmp_path, "b" * 16, ".png")
        orphan = self._image(tmp_path, "c" * 16)
        (tmp_path / "session-1.html").write_text(
            f'<img src="images/{shown.name}" />', encoding="utf-8"
        )
        (tmp_path / "session-1.md").write_text(
            f"![image](images/{in_markdown.name})", encoding="utf-8"
        )

        assert prune_unreferenced_images(tmp_path) == 1
        assert shown.exists() and in_markdown.exists()
        assert not orphan.exists()

    def test_keeps_a_recent_image_and_other_files(self, tmp_path: Path):
        from claude_code_log.image_export import prune_unreferenced_images

        recent = self._image(tmp_path, "d" * 16, age=10)
        other = tmp_path / "images" / "notes.txt"
        other.write_text("mine", encoding="utf-8")
        temp = tmp_path / "images" / f".image_{'e' * 16}.avif.tmp-1"
        temp.write_bytes(b"x")

        assert prune_unreferenced_images(tmp_path) == 0
        assert recent.exists() and other.exists() and temp.exists()
        assert prune_unreferenced_images(tmp_path, grace_seconds=0) == 1
        assert not recent.exists() and other.exists()

    def test_finds_a_reference_across_a_read_chunk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import claude_code_log.image_export as image_export

        shown = self._image(tmp_path, "f" * 16)
        monkeypatch.setattr(image_export, "_SCAN_CHUNK", 7)
        (tmp_path / "page.html").write_text(
            "padding " * 3 + f'<img src="images/{shown.name}" />', encoding="utf-8"
        )

        assert image_export.prune_unreferenced_images(tmp_path) == 0
        assert shown.exists()

    def test_sweeps_once_per_version(self, tmp_path: Path):
        from claude_code_log.image_export import SWEEP_MARKER, sweep_images_once

        self._image(tmp_path, "1" * 16)
        assert sweep_images_once(tmp_path, "1.9.0") == 1
        assert (tmp_path / "images" / SWEEP_MARKER).read_text().strip() == "1.9.0"
        self._image(tmp_path, "2" * 16)
        assert sweep_images_once(tmp_path, "1.9.0") == 0
        assert sweep_images_once(tmp_path, "1.9.1") == 1

    def test_an_unfinished_scan_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import claude_code_log.image_export as image_export

        self._image(tmp_path, "3" * 16)
        (tmp_path / "page.html").write_text("", encoding="utf-8")

        def unreadable(_page: Path) -> set[bytes]:
            raise PermissionError("locked")

        monkeypatch.setattr(image_export, "_references_in", unreadable)
        assert image_export.sweep_images_once(tmp_path, "1.9.0") == 0
        assert not (tmp_path / "images" / image_export.SWEEP_MARKER).exists()
        monkeypatch.undo()
        assert image_export.sweep_images_once(tmp_path, "1.9.0") == 1

    def test_reusing_an_image_marks_it_used(self, tmp_path: Path):
        import time

        from claude_code_log import image_export

        data = _b64_image("PNG", (40, 30))
        first = image_export.export_compressed_image("image/png", data, tmp_path)
        assert first is not None
        path = tmp_path / first.src
        then = time.time() - 7200
        import os

        os.utime(path, (then, then))
        image_export._exported.clear()  # a later process
        assert image_export.export_compressed_image("image/png", data, tmp_path)
        assert path.stat().st_mtime > then + 3600
        # And from this process's memo, an hour on (a long watch).
        os.utime(path, (then, then))
        assert image_export.export_compressed_image("image/png", data, tmp_path)
        assert path.stat().st_mtime > then + 3600


def test_the_first_conversion_after_an_upgrade_removes_old_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A version change re-renders the pages; the conversion that does
    removes the images they no longer show, and later ones don't look."""
    import json
    import os
    import time

    from click.testing import CliRunner

    import claude_code_log.converter as converter
    from claude_code_log.cli import main

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    project = tmp_path / "project"
    project.mkdir()
    entry = {
        "type": "user",
        "timestamp": "2025-07-01T10:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.0",
        "uuid": "u1",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _b64_image("PNG", (64, 48)),
                    },
                },
            ],
        },
    }
    (project / "s1.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    old = project / "images" / ("image_" + "0" * 16 + ".avif")
    old.parent.mkdir()
    old.write_bytes(b"from an older encoder")
    then = time.time() - 7200
    os.utime(old, (then, then))

    monkeypatch.setattr(converter, "get_library_version", lambda: "9.9.0")
    result = CliRunner().invoke(main, [str(project)])
    assert result.exit_code == 0, result.output

    assert not old.exists()
    shown = [p for p in (project / "images").iterdir() if p.name.startswith("image_")]
    assert len(shown) == 1
    assert shown[0].name in (project / "session-s1.html").read_text(encoding="utf-8")


def test_clear_output_removes_the_images_only_its_pages_showed(tmp_path: Path):
    from click.testing import CliRunner

    from claude_code_log.cli import main

    project = tmp_path / "project"
    project.mkdir()
    (project / "s1.jsonl").write_text("", encoding="utf-8")
    images = project / "images"
    images.mkdir()
    html_only = images / ("image_" + "a" * 16 + ".avif")
    in_markdown = images / ("image_" + "b" * 16 + ".png")
    for path in (html_only, in_markdown):
        path.write_bytes(b"x")
    (project / "combined_transcripts.html").write_text(
        f'<img src="images/{html_only.name}">', encoding="utf-8"
    )
    (project / "combined_transcripts.md").write_text(
        f"![image](images/{in_markdown.name})", encoding="utf-8"
    )

    result = CliRunner().invoke(main, [str(project), "--clear-output"])
    assert result.exit_code == 0, result.output

    assert not (project / "combined_transcripts.html").exists()
    assert not html_only.exists()
    assert in_markdown.exists()
    assert "Removed 1 images" in result.output


def test_session_pages_beside_the_project_get_their_own_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`-o page.html` writes the combined page elsewhere and the session
    pages in the project: a fragment formatted for one must not lend the
    other an image that was only exported beside the first."""
    import json
    import re

    from claude_code_log.converter import convert_jsonl_to

    monkeypatch.setenv("CLAUDE_CODE_LOG_FRAGMENT_STORE", "1")
    project = tmp_path / "project"
    project.mkdir()
    entry = {
        "type": "user",
        "timestamp": "2025-07-01T10:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.0",
        "uuid": "u1",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _b64_image("PNG", (64, 48)),
                    },
                },
            ],
        },
    }
    (project / "s1.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere" / "page.html"
    elsewhere.parent.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    convert_jsonl_to("html", project, elsewhere, silent=True)

    for page in (elsewhere, project / "session-s1.html"):
        srcs = re.findall(r'src="(images/[^"]+)"', page.read_text(encoding="utf-8"))
        assert srcs, page
        for src in srcs:
            assert (page.parent / src).exists(), (page, src)
