"""Tests for ``[Image #N]`` placeholder resolution via ``imagePasteIds``.

Claude Code records the association explicitly: ``imagePasteIds`` runs parallel
to the image content blocks, so ``[Image #N]`` is the block at
``imagePasteIds.index(N)``. N is a paste counter — it resets when the CLI
restarts inside a session that outlives it, and it increments on
delete-and-repaste — so reading it as a position (block ``N-1``) is wrong in
ways that do not look wrong.

All fixtures here are synthetic. The image payloads are a 1x1 PNG so the
rendered output is real image data, distinguishable per block by its alt/data.
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.factories import create_user_message
from claude_code_log.html.renderer import generate_html
from claude_code_log.models import (
    ContentItem,
    ImageContent,
    ImageSource,
    MessageMeta,
    SystemReminderContent,
    TextContent,
    UserTextMessage,
)
from claude_code_log.parser import extract_text_content

# A real 1x1 transparent PNG, base64-encoded.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _image(tag: str) -> ImageContent:
    """An image block tagged so a test can say *which* block was rendered.

    The tag prefixes the payload, which makes it no longer decodable — these
    fixtures test *which* block lands where, not that it displays.
    :meth:`TestFieldSurvivesParsing.test_the_inlined_image_is_renderable`
    covers the payload with an untagged, genuinely valid PNG.
    """
    return ImageContent(
        type="image",
        source=ImageSource(
            type="base64", media_type="image/png", data=f"{tag}:{_PNG_1X1}"
        ),
    )


def _render(
    content_list: list[ContentItem], paste_ids: object = None
) -> UserTextMessage:
    model = create_user_message(
        MessageMeta.empty(uuid="u-1"),
        content_list,
        extract_text_content(content_list),
        image_paste_ids=paste_ids,
    )
    assert isinstance(model, UserTextMessage)
    return model


def _tags(model: UserTextMessage) -> list[str]:
    """The rendered items as a comparable sequence: text verbatim, image blocks
    as ``<tag>``."""
    out: list[str] = []
    for item in model.items:
        if isinstance(item, ImageContent):
            out.append(f"<{item.source.data.split(':', 1)[0]}>")
        elif isinstance(item, TextContent):
            out.append(item.text)
        elif isinstance(item, SystemReminderContent):
            out.append(f"[reminder]{item.reminders[0]}")
        else:  # pragma: no cover - no other item type occurs in these fixtures
            out.append(type(item).__name__)
    return out


class TestPasteIdResolution:
    """The recorded association decides which block a placeholder names."""

    def test_in_range_but_not_positional_is_the_silent_wrong_image(self):
        """Two blocks, paste ids ``[2, 3]``: ``[Image #2]`` is the FIRST block.

        The positional reading picks block 2-1 = the second, which is a
        different image with nothing to mark it as wrong — this is the case
        the fix exists for, and the only one no reader could catch.
        """
        first, second = _image("first"), _image("second")
        model = _render(
            [
                first,
                second,
                TextContent(type="text", text="see [Image #2] and [Image #3]"),
            ],
            paste_ids=[2, 3],
        )

        assert _tags(model) == ["see ", "<first>", " and ", "<second>"]

    def test_paste_ids_may_have_gaps(self):
        """Delete-and-repaste advances the counter, leaving holes: ``[4, 6, 8]``."""
        blocks = [_image("a"), _image("b"), _image("c")]
        model = _render(
            [
                *blocks,
                TextContent(
                    type="text", text="[Image #8] then [Image #4] then [Image #6]"
                ),
            ],
            paste_ids=[4, 6, 8],
        )

        assert _tags(model) == ["<c>", " then ", "<a>", " then ", "<b>"]

    def test_text_order_need_not_be_ascending(self):
        """Where the user clicked when pasting has nothing to do with block order."""
        blocks = [_image("a"), _image("b"), _image("c"), _image("d")]
        model = _render(
            [
                *blocks,
                TextContent(
                    type="text", text="[Image #3][Image #1][Image #2][Image #4]"
                ),
            ],
            paste_ids=[1, 2, 3, 4],
        )

        assert _tags(model) == ["<c>", "<a>", "<b>", "<d>"]

    def test_number_above_the_block_count_still_resolves(self):
        """A counter that has run ahead of this message: ``[Image #29]``, 1 block.

        The positional rule rejected these as out of range, leaving the
        placeholder as text and the image detached — 126 of the 168 real
        placeholders measured on 2026-07-28 took that path.
        """
        model = _render(
            [
                _image("only"),
                TextContent(type="text", text="look at [Image #29] please"),
            ],
            paste_ids=[29],
        )

        assert _tags(model) == ["look at ", "<only>", " please"]

    def test_unreferenced_blocks_keep_their_position(self):
        """A block no placeholder names stays where it sat in the content."""
        model = _render(
            [
                _image("kept"),
                _image("named"),
                TextContent(type="text", text="only [Image #7]"),
            ],
            paste_ids=[5, 7],
        )

        assert _tags(model) == ["<kept>", "only ", "<named>"]

    def test_a_block_is_never_both_inlined_and_appended(self):
        model = _render(
            [_image("a"), TextContent(type="text", text="x [Image #2] y")],
            paste_ids=[2],
        )

        assert _tags(model).count("<a>") == 1


class TestFailClosed:
    """When the association cannot be established, show a gap — never a guess.

    Each condition is separate: they are indistinguishable in the output (a
    literal placeholder plus a detached block) and only the warning tells a
    reader whether they are looking at an old transcript or a broken one.
    """

    def test_paste_id_not_among_the_recorded_ids(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            model = _render(
                [_image("a"), TextContent(type="text", text="see [Image #9]")],
                paste_ids=[3],
            )

        assert _tags(model) == ["<a>", "see [Image #9]"]
        assert "[Image #9] is not among the paste ids [3]" in caplog.text

    def test_length_mismatch_refuses_every_placeholder(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Parallel lists that are not parallel: nothing in them can be trusted,
        including the entries that happen to line up."""
        with caplog.at_level(logging.WARNING):
            model = _render(
                [
                    _image("a"),
                    _image("b"),
                    TextContent(type="text", text="[Image #1] and [Image #2]"),
                ],
                paste_ids=[1],
            )

        assert _tags(model) == ["<a>", "<b>", "[Image #1] and [Image #2]"]
        assert "has 1 entries but the message carries 2 image block(s)" in caplog.text

    def test_malformed_field_is_reported_not_coerced(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            model = _render(
                [_image("a"), TextContent(type="text", text="[Image #1]")],
                paste_ids={"1": 0},
            )

        assert _tags(model) == ["<a>", "[Image #1]"]
        assert "is not a list of integers" in caplog.text

    def test_booleans_are_not_paste_ids(self, caplog: pytest.LogCaptureFixture):
        """``bool`` is an ``int`` in Python; a ``[True]`` field is malformed, and
        letting it through would map ``[Image #1]`` onto the first block by
        accident rather than by evidence."""
        with caplog.at_level(logging.WARNING):
            model = _render(
                [_image("a"), TextContent(type="text", text="[Image #1]")],
                paste_ids=[True],
            )

        assert _tags(model) == ["<a>", "[Image #1]"]
        assert "is not a list of integers" in caplog.text

    def test_repeated_paste_id_is_refused(self, caplog: pytest.LogCaptureFixture):
        """``index()`` would silently pick the first of two blocks claiming the
        same id, and the second would look merely unreferenced."""
        with caplog.at_level(logging.WARNING):
            model = _render(
                [
                    _image("a"),
                    _image("b"),
                    TextContent(type="text", text="[Image #4]"),
                ],
                paste_ids=[4, 4],
            )

        assert _tags(model) == ["<a>", "<b>", "[Image #4]"]
        assert "repeats a paste id" in caplog.text


class TestLegacyTranscripts:
    """Old transcripts recorded nothing, so position is the only evidence —
    and it is evidence only when the numbering could have been positional.
    ``test_data`` still carries 1.0.x sessions of that shape."""

    def test_contiguous_numbering_is_read_positionally(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            model = _render(
                [
                    _image("a"),
                    _image("b"),
                    TextContent(type="text", text="[Image #2] then [Image #1]"),
                ]
            )

        assert _tags(model) == ["<b>", " then ", "<a>"]
        assert caplog.text == ""

    def test_a_subset_starting_at_one_is_still_positional(self):
        """Two blocks, only the first referenced — the numbering is consistent
        with position, and the unreferenced block appends as before."""
        model = _render(
            [
                _image("a"),
                _image("b"),
                TextContent(type="text", text="just [Image #1]"),
            ]
        )

        assert _tags(model) == ["<b>", "just ", "<a>"]

    def test_a_gap_means_the_numbering_is_not_positional(
        self, caplog: pytest.LogCaptureFixture
    ):
        """``[Image #2]`` alone over two blocks: under a paste counter that is
        the FIRST block, under position it is the second. With nothing
        recorded, both readings are guesses."""
        with caplog.at_level(logging.WARNING):
            model = _render(
                [
                    _image("a"),
                    _image("b"),
                    TextContent(type="text", text="see [Image #2]"),
                ]
            )

        assert _tags(model) == ["<a>", "<b>", "see [Image #2]"]
        assert "imagePasteIds is absent" in caplog.text

    def test_more_numbers_than_blocks_is_not_positional(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING):
            model = _render(
                [
                    _image("a"),
                    TextContent(type="text", text="[Image #1] and [Image #2]"),
                ]
            )

        assert _tags(model) == ["<a>", "[Image #1] and [Image #2]"]
        assert "imagePasteIds is absent" in caplog.text


class TestPlaceholdersOutsideRenderedText:
    """A placeholder the inliner never reaches must not cost its block.

    ``referenced_images`` used to be scanned over the raw item text while
    inlining happened over the reminder-split, IDE-peeled segments, so a
    placeholder inside a ``<system-reminder>`` marked its block as spoken for
    and the block was then dropped by a pass that never rendered it. No real
    message measured on 2026-07-28 had that shape; the seam is closed here
    structurally rather than left to the corpus.
    """

    def test_placeholder_inside_a_system_reminder_does_not_drop_the_block(self):
        model = _render(
            [
                _image("a"),
                TextContent(
                    type="text",
                    text="hello <system-reminder>ignore [Image #4]</system-reminder>",
                ),
            ],
            paste_ids=[4],
        )

        assert _tags(model) == ["<a>", "hello ", "[reminder]ignore [Image #4]"]

    def test_placeholder_in_an_ide_notification_prefix_does_not_drop_the_block(self):
        model = _render(
            [
                _image("a"),
                TextContent(
                    type="text",
                    text=(
                        "<ide_selection>The user selected [Image #4] in the "
                        "IDE</ide_selection>after"
                    ),
                ),
            ],
            paste_ids=[4],
        )

        tags = _tags(model)
        assert "<a>" in tags, tags


class TestFieldSurvivesParsing:
    """The field is a sibling of ``message``, so it only reaches the factory if
    the entry model declares it — Pydantic drops unknown fields by default."""

    def _block(self, data: str) -> dict[str, object]:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        }

    def _entry(
        self, paste_ids: object, content: Optional[list[dict[str, object]]] = None
    ) -> dict[str, object]:
        if content is None:
            content = [
                self._block(f"first:{_PNG_1X1}"),
                self._block(f"second:{_PNG_1X1}"),
                {"type": "text", "text": "look at [Image #2]"},
            ]
        return {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "sessionId": "s-1",
            "timestamp": "2026-07-28T10:00:00.000Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "version": "2.1.218",
            "imagePasteIds": paste_ids,
            "message": {"role": "user", "content": content},
        }

    def test_paste_ids_reach_the_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(json.dumps(self._entry([2, 3])) + "\n", encoding="utf-8")
            messages = load_transcript(path)
            html = generate_html(messages, "Paste ids")

        assert getattr(messages[0], "imagePasteIds", None) == [2, 3]

        # [Image #2] is the FIRST block, so "first" renders where the text
        # says it does and "second" appends unreferenced ahead of the text.
        assert "[Image #2]" not in html
        assert html.index("second:") < html.index("look at") < html.index("first:")

    def test_the_inlined_image_is_renderable(self):
        """The resolved block reaches the page as a usable ``data:`` URL — the
        point of the fix is a visible image, not a correct index."""
        entry = self._entry(
            [29],
            content=[
                self._block(_PNG_1X1),
                {"type": "text", "text": "look at [Image #29]"},
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            html = generate_html(load_transcript(path), "Renderable")

        assert f"data:image/png;base64,{_PNG_1X1}" in html
        assert "[Image #29]" not in html

    def test_a_malformed_field_does_not_cost_the_whole_entry(self):
        """A ValidationError here would drop the message, not just its images."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(
                json.dumps(self._entry("not-a-list")) + "\n", encoding="utf-8"
            )
            messages = load_transcript(path)

        assert len(messages) == 1
