#!/usr/bin/env python3
"""Steering messages: render from ``queued_command`` attachments.

Modern Claude Code (≳2.1.101) writes an in-DAG ``queued_command``
attachment for every steering delivery, 1:1 with the chain-less legacy
queue-operation ``remove``. This suite covers the three-part rework
(spec: ``work/steering-queued-command.md``):

(a) ``queued_command`` promoted to a steering card in
    ``attachment_factory``, routed through the user-message
    classification + plugin-transformer pass;
(b) the paired legacy ``remove`` suppressed per inferred harness
    version (per-session scoped);
(c) exact-type hardening of the legacy steering conversion so a
    transformer-demoted ``UserTextMessage`` subclass is never rebuilt
    into an empty-items steering card (defect 1).

The demotion cases exercise the in-repo reference plugin's
``[testhook]`` hook-demotion transformer (``test/_plugins/clmail/``),
so they skip when it isn't installed.
"""

import json
import tempfile
from pathlib import Path

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.plugins import reset_cache


@pytest.fixture(autouse=True)
def _reset_plugin_cache():
    """Load the real entry-point transformers fresh for each test.

    Other suites inject fakes into ``plugins._cached_transformers``;
    resetting guards against cross-file ordering under xdist.
    """
    reset_cache()
    yield
    reset_cache()


def _have_embedded_plugin() -> bool:
    try:
        import claude_code_log_clmail_test  # noqa: F401
    except ImportError:
        return False
    return True


reference_plugin_required = pytest.mark.skipif(
    not _have_embedded_plugin(),
    reason="test-embedded reference plugin not installed; run `uv sync`",
)


# --------------------------------------------------------------------------
# Synthetic transcript builders (no private data — see spec)
# --------------------------------------------------------------------------
_SID = "sess-1"
_MODERN = "2.1.207"  # a version that writes queued_command
_LEGACY = "2.1.29"  # a version that predates queued_command


def _user(uuid, parent, text, *, version=_MODERN, sid=_SID):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": sid,
        "version": version,
        "timestamp": "2026-07-11T07:00:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(uuid, parent, text, *, version=_MODERN, sid=_SID):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": sid,
        "version": version,
        "timestamp": "2026-07-11T07:00:01.000Z",
        "message": {
            "id": "msg-" + uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _remove(text, *, sid=_SID):
    """Legacy queue-operation 'remove' — chain-less, uuid-less."""
    return {
        "type": "queue-operation",
        "operation": "remove",
        "timestamp": "2026-07-11T07:00:02.000Z",
        "content": text,
        "sessionId": sid,
    }


def _queued_command(uuid, parent, prompt, *, version=_MODERN, sid=_SID, paste_ids=None):
    """Modern in-DAG queued_command attachment paired with a 'remove'.

    ``prompt=None`` omits the prompt key entirely (a promptless, non-
    renderable attachment). ``paste_ids`` sets ``imagePasteIds`` *inside the
    attachment payload* — this record is its own carrier, unlike a user entry
    where the field sits at top level beside ``message``.
    """
    attachment = {
        "type": "queued_command",
        "commandMode": "prompt",
        "origin": {"kind": "human"},
        "timestamp": "2026-07-11T07:00:00.000Z",
    }
    if prompt is not None:
        attachment["prompt"] = prompt
    if paste_ids is not None:
        attachment["imagePasteIds"] = paste_ids
    return {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": sid,
        "version": version,
        "timestamp": "2026-07-11T07:00:03.000Z",
        "attachment": attachment,
    }


def _render(entries, title="Steering Test"):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        path = Path(f.name)
    try:
        messages = load_transcript(path)
        return generate_html(messages, title)
    finally:
        path.unlink()


# --------------------------------------------------------------------------
# (a)+(b) Modern transcripts: promote queued_command, suppress the remove
# --------------------------------------------------------------------------
class TestModernQueuedCommand:
    def test_plain_prompt_renders_as_steering_and_remove_suppressed(self):
        """A paired remove + queued_command yields exactly ONE steering
        card (from the attachment), carrying a uuid → parentUuid debug
        line the chain-less remove could never have."""
        html = _render(
            [
                _user("u1", None, "start"),
                _assistant("a1", "u1", "working"),
                _remove("please focus on the parser"),
                _user("u2", "a1", "next real prompt"),
                _queued_command("qc1", "u2", "please focus on the parser"),
                _assistant("a2", "qc1", "ok"),
            ]
        )

        # Exactly one steering card (the remove is suppressed, not
        # rendered alongside the attachment → no duplicate).
        assert html.count("User (steering)") == 1
        assert "please focus on the parser" in html
        # Anchored in the DAG: the promoted card carries the attachment's
        # uuid → parentUuid debug line (the legacy remove is uuid-less).
        assert "qc1 &rarr; u2" in html

    @reference_plugin_required
    def test_demoted_prompt_renders_marker_not_empty_card(self):
        """A ``[testhook]`` steering injection is demoted by the plugin
        transformer to the same marker as its idle-delivered siblings —
        NOT an empty steering card (defect 1, dissolved on modern
        transcripts by promoting the attachment)."""
        html = _render(
            [
                _user("u1", None, "start"),
                _assistant("a1", "u1", "working"),
                _remove("[testhook] deploy blocked — approval required"),
                _user("u2", "a1", "next real prompt"),
                _queued_command(
                    "qc1", "u2", "[testhook] deploy blocked — approval required"
                ),
                _assistant("a2", "qc1", "ok"),
            ]
        )

        # Demoted → rendered as the marker, present exactly once, and NOT
        # promoted to a (would-be empty) steering card.
        assert "deploy blocked — approval required" in html
        assert html.count("User (steering)") == 0

    def test_promptless_queued_command_does_not_suppress_remove(self):
        """A queued_command with no usable prompt renders nothing, so it
        must NOT seed the version-suppression set — otherwise the paired
        `remove` (which still carries the steering text) would be dropped
        and the steering content lost entirely."""
        html = _render(
            [
                _user("u1", None, "start"),
                _assistant("a1", "u1", "working"),
                _remove("do not lose this steering text"),
                _user("u2", "a1", "next real prompt"),
                _queued_command("qc1", "u2", None),  # promptless → renders nothing
                _assistant("a2", "qc1", "ok"),
            ]
        )

        # The remove is the only carrier of the text → it must still render.
        assert "do not lose this steering text" in html
        assert html.count("User (steering)") == 1


# --------------------------------------------------------------------------
# (b) Legacy transcripts (no queued_command anywhere): unchanged behaviour
# --------------------------------------------------------------------------
class TestLegacyRemove:
    def test_remove_without_attachment_still_renders_as_steering(self):
        """Old transcripts (≤2.1.29) have no queued_command → the
        version set is empty → removes render exactly as before."""
        html = _render(
            [
                _user("u1", None, "start", version=_LEGACY),
                _assistant("a1", "u1", "working", version=_LEGACY),
                _remove("steer the ongoing turn"),
                _assistant("a2", "a1", "ok", version=_LEGACY),
            ]
        )

        assert html.count("User (steering)") == 1
        assert "steer the ongoing turn" in html

    @reference_plugin_required
    def test_legacy_demoted_remove_is_not_empty_card(self):
        """Defect-1 hardening (c): a legacy remove whose single-line text
        is demoted by a transformer must render the marker, NOT be
        rebuilt into an empty steering card.

        Mutation-check (feedback_test_must_reach_its_target_guard):
        reverting the ``type(...) is UserTextMessage`` guard at
        ``renderer.py`` to ``isinstance(...)`` makes this RED — the
        marker text disappears and a bare ``User (steering)`` card with
        no body appears (verified manually against this fixture)."""
        html = _render(
            [
                _user("u1", None, "start", version=_LEGACY),
                _assistant("a1", "u1", "working", version=_LEGACY),
                _remove("[testhook] steering injection body"),
                _assistant("a2", "a1", "ok", version=_LEGACY),
            ]
        )

        # The demoted marker text survives (the guard did NOT clobber it).
        assert "steering injection body" in html
        # And it was not turned into a steering card at all.
        assert html.count("User (steering)") == 0


# --------------------------------------------------------------------------
# (b) Mixed-version session (spans a harness upgrade)
# --------------------------------------------------------------------------
class TestMixedVersion:
    def test_only_versions_with_queued_command_suppress_removes(self):
        """A resumed session can span an upgrade: a remove under a
        version that never wrote queued_command must still render, while
        a remove under a version that did is suppressed (its attachment
        renders instead). Version is inferred from the last
        version-bearing entry in file order."""
        html = _render(
            [
                _user("u1", None, "start", version=_LEGACY),
                _assistant("a1", "u1", "working", version=_LEGACY),
                _remove("legacy-steer"),  # infers 2.1.29 (no qc) → renders
                _assistant("a2", "a1", "upgraded", version=_MODERN),
                _remove("modern-steer"),  # infers 2.1.207 (qc set) → suppressed
                _user("u2", "a2", "next", version=_MODERN),
                _queued_command("qc1", "u2", "modern-steer", version=_MODERN),
                _assistant("a3", "qc1", "ok", version=_MODERN),
            ]
        )

        # Two steering cards: the legacy remove + the modern attachment
        # (NOT three — the modern remove is suppressed, not duplicated).
        assert html.count("User (steering)") == 2
        assert "legacy-steer" in html
        assert "modern-steer" in html
        # The modern steering card is the attachment (has a debug line);
        # the legacy one is the uuid-less remove (no debug line for it).
        assert "qc1 &rarr; u2" in html

    @staticmethod
    def _imbalance_warnings(caplog):
        """Orphan-remove warnings emitted by the suppression pass.

        Matches on ``has no counted queued_command card`` — the clause
        that states what the renderer observed. The message deliberately
        no longer claims the archive's remove↔queued_command pairing is
        "violated": in #294 it was intact and the pre-pass was at fault.
        """
        import logging

        return [
            rec
            for rec in caplog.records
            if "has no counted queued_command card" in rec.message
            and rec.levelno == logging.WARNING
        ]

    def test_orphan_removes_render_losslessly(self, caplog):
        """When a (session, version) has MORE removes than countable
        queued_command cards, suppression must be lossless: hide exactly
        the removes with a matching card (no duplicate), and RENDER the
        orphan removes rather than drop their steering text. The orphan is
        logged once per (session, version).

        Whether a real archive's remove↔queued_command pairing can break
        on its own is unsettled — the "2.1.160 file with 34 removes / 29
        qc" once cited here counted null-content removes that never reach
        the suppression pass (see the note in renderer.py). Losslessness
        is required regardless: an uncounted card produces the same shape,
        and that is what #294 turned out to be."""
        import logging

        with caplog.at_level(logging.WARNING, logger="claude_code_log.renderer"):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("first-steer"),  # paired → suppressed (spends the card)
                    _remove("second-steer-ORPHAN"),  # orphan → rendered, not dropped
                    _user("u2", "a1", "next"),
                    _queued_command("qc1", "u2", "first-steer"),  # only ONE card
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        # Lossless: exactly two steering cards — the qc card + the orphan
        # remove — with NO duplicate for the paired one.
        assert html.count("User (steering)") == 2
        # Both texts survive; nothing silently dropped.
        assert "first-steer" in html
        assert "second-steer-ORPHAN" in html
        # The paired text renders exactly once (via the qc card), NOT twice.
        # Each rendered message text appears twice in the HTML (card body +
        # timeline data), so a single render == 2 occurrences; a duplicate
        # (paired remove ALSO rendered) would be 4.
        assert html.count("first-steer") == 2
        assert len(self._imbalance_warnings(caplog)) == 1

    def test_orphan_remove_before_paired_remove_is_lossless(self, caplog):
        """Order-independence (CodeRabbit #284): suppression must pair a
        ``remove`` to its ``queued_command`` by PROMPT CONTENT, not arrival
        order. With an order-only budget, an orphan ``remove`` arriving
        BEFORE the paired one wrongly spends the budget → the orphan's text
        is dropped and the paired text is duplicated (rendered by both the
        qc card and the un-suppressed paired remove).

        Mutation-check: key the budget on ``(session, version)`` only
        (drop ``remove_text`` from the key in renderer.py) and this test
        goes RED — ``orphan-first-STEER`` disappears and ``paired-STEER``
        renders twice (verified against this fixture)."""
        import logging

        with caplog.at_level(logging.WARNING, logger="claude_code_log.renderer"):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("orphan-first-STEER"),  # orphan, arrives FIRST
                    _remove("paired-STEER"),  # paired (qc below has this text)
                    _user("u2", "a1", "next"),
                    _queued_command("qc1", "u2", "paired-STEER"),
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        # Two cards: the qc (paired-STEER) + the orphan remove (rendered).
        assert html.count("User (steering)") == 2
        # The orphan survives — NOT consumed by the paired card's budget.
        assert "orphan-first-STEER" in html
        # The paired text renders exactly once (via the qc), NOT duplicated.
        assert html.count("paired-STEER") == 2
        assert len(self._imbalance_warnings(caplog)) == 1


# --------------------------------------------------------------------------
# (d) Non-string prompt shapes (#294)
# --------------------------------------------------------------------------
# A steering delivery that carries an image is written with ``prompt`` as a
# LIST of content blocks rather than a string. Both the suppression pre-pass
# and the attachment factory used to accept only ``str``, so such a card was
# dropped (losing the image outright) while its paired legacy ``remove``
# rendered the text alone — and the renderer then blamed the archive for a
# broken 1:1 pairing that was in fact intact.

# Smallest valid PNG (1x1, transparent) — synthetic, no real capture.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _image_block(data=_PNG_1X1, media_type="image/png"):
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


class TestNonStringPromptShapes:
    @staticmethod
    def _shape_warnings(caplog):
        import logging

        return [
            rec
            for rec in caplog.records
            if "is not pairable" in rec.message and rec.levelno == logging.WARNING
        ]

    def test_image_prompt_renders_image_and_suppresses_remove(self, caplog):
        """A text+image ``prompt`` list pairs with its legacy ``remove``
        exactly like a string prompt: ONE steering card, carrying the
        image, and no orphan-remove warning.

        This is the #294 defect. Mutation-check: restore the old guard in
        ``attachment_factory.queued_command_prompt_items`` (return ``None``
        unless ``isinstance(prompt, str)``) and this test goes RED three
        ways — the ``<img`` disappears, the card count stays 1 but comes
        from the *remove* instead of the attachment (``qc1 &rarr; u2``
        vanishes), and an orphan warning fires.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("look at this screenshot"),
                    _user("u2", "a1", "next real prompt"),
                    _queued_command(
                        "qc1",
                        "u2",
                        [
                            {"type": "text", "text": "look at this screenshot"},
                            _image_block(),
                        ],
                    ),
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        # Exactly one steering card — the remove was suppressed, so the
        # text is not duplicated.
        assert html.count("User (steering)") == 1
        assert html.count("look at this screenshot") == 2  # card + timeline
        # It is the ATTACHMENT's card (DAG-anchored), not the uuid-less remove.
        assert "qc1 &rarr; u2" in html
        # The image survives to the page — the content the old guard dropped.
        assert f'<img src="data:image/png;base64,{_PNG_1X1}"' in html
        # The pairing is intact, so nothing is reported.
        assert self._shape_warnings(caplog) == []
        assert TestMixedVersion._imbalance_warnings(caplog) == []

    def test_image_only_prompt_is_not_pairable_and_keeps_remove_visible(self, caplog):
        """An image with no text renders, but cannot seed the budget: the
        budget keys on text, and there is none. Failing closed means the
        paired ``remove`` (which does carry text) stays visible rather
        than being suppressed by a card that cannot be shown to match it.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("do not lose this text"),
                    _user("u2", "a1", "next real prompt"),
                    _queued_command("qc1", "u2", [_image_block()]),
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        # Both survive: the image card AND the text-bearing remove.
        assert f'<img src="data:image/png;base64,{_PNG_1X1}"' in html
        assert "do not lose this text" in html
        assert html.count("User (steering)") == 2
        # Said out loud, naming the shape.
        warnings = self._shape_warnings(caplog)
        assert len(warnings) == 1
        assert "list[image]" in warnings[0].message

    def test_unknown_prompt_shape_renders_and_names_the_shape(self, caplog):
        """A shape we do not understand must fail closed *visibly*: render
        the payload rather than drop it, name the shape in the log, and
        leave the paired ``remove`` alone.

        Silently dropping is the failure mode this guards against — it is
        indistinguishable from there having been no steering delivery.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("legacy carrier text"),
                    _user("u2", "a1", "next real prompt"),
                    # A dict: not a shape Claude Code writes today.
                    _queued_command("qc1", "u2", {"unexpected": "payload-42"}),
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        # Nothing vanished: the odd payload is on the page, and so is the
        # remove's text.
        assert "payload-42" in html
        assert "legacy carrier text" in html
        # Diagnosable from the log line alone — the shape is named.
        warnings = self._shape_warnings(caplog)
        assert len(warnings) == 1
        assert "dict" in warnings[0].message

    def test_promptless_attachment_stays_silent(self, caplog):
        """A ``queued_command`` with no ``prompt`` key at all renders
        nothing and warns nothing — there is no content to lose, so it is
        not a shape problem. Pins the boundary of the new warning so it
        cannot start firing on the ordinary promptless attachment.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            html = _render(
                [
                    _user("u1", None, "start"),
                    _assistant("a1", "u1", "working"),
                    _remove("carried by the remove"),
                    _user("u2", "a1", "next real prompt"),
                    _queued_command("qc1", "u2", None),
                    _assistant("a2", "qc1", "ok"),
                ]
            )

        assert "carried by the remove" in html
        assert self._shape_warnings(caplog) == []

    @reference_plugin_required
    def test_transformed_list_prompt_matches_the_string_path(self, caplog):
        """A list prompt whose text a transformer rewrites must behave
        exactly like the same text as a plain string.

        This is the discriminating case for drift between the budget key
        and the rendered card: the key is built from the RAW extracted
        text, before any transformer runs, and the legacy ``remove``
        carries that same raw text — so the pair still matches even though
        what renders is the demoted ``[testhook]`` marker rather than a
        steering card. Asserting the two paths produce the SAME counts
        pins that, where a fixed expected number would only pin today's
        template.

        If the list path ever hand-built its items past
        ``create_user_message``, the marker would become a steering card
        here and the two paths would diverge.

        Note: the demotion drops the image along with the rest of the
        original content — the transformer returns its own rendering, and
        the string path has always been returned unchanged in the same
        way. That is the plugin's call, not the steering path's.
        """
        import logging

        prompt_text = "[testhook] deploy blocked — approval required"
        marker = "deploy blocked — approval required"

        def build(prompt):
            return [
                _user("u1", None, "start"),
                _assistant("a1", "u1", "working"),
                _remove(prompt_text),
                _user("u2", "a1", "next real prompt"),
                _queued_command("qc1", "u2", prompt),
                _assistant("a2", "qc1", "ok"),
            ]

        with caplog.at_level(logging.WARNING):
            as_string = _render(build(prompt_text))
            as_list = _render(
                build([{"type": "text", "text": prompt_text}, _image_block()])
            )

        # Demoted to the marker, not promoted to a steering card — and the
        # remove paired away, so the marker is not rendered twice.
        assert marker in as_list
        assert as_list.count("User (steering)") == 0
        # The list path is indistinguishable from the string path.
        assert as_list.count(marker) == as_string.count(marker)
        assert as_list.count("User (steering)") == as_string.count("User (steering)")
        assert TestMixedVersion._imbalance_warnings(caplog) == []


class TestSteeringPasteIds:
    """A steering delivery is its own paste-id carrier.

    ``imagePasteIds`` sits *inside the attachment payload* here, not at entry
    top level as it does beside a user entry's ``message``. Nothing declares
    it on the model — ``AttachmentTranscriptEntry.attachment`` is a
    ``dict[str, Any]`` passthrough — so the only thing that can go wrong is
    the factory forgetting to hand it to ``create_user_message``, which is
    exactly what this pins.
    """

    def test_non_contiguous_paste_ids_resolve_by_id_not_position(self):
        """Two blocks, ids ``[4, 6]``: ``[Image #6]`` is the SECOND block and
        ``[Image #4]`` the first.

        The numbering is deliberately non-contiguous so the recorded reading
        and the positional fallback *disagree*. Under the fallback, `4` and `6`
        are not 1..k, so neither placeholder resolves at all and both stay as
        literal text — a contiguous fixture would pass either way and pin
        nothing.
        """
        first, second = _image_block("first"), _image_block("second")
        html = _render(
            [
                _user("u1", None, "start"),
                _assistant("a1", "u1", "working"),
                _user("u2", "a1", "next real prompt"),
                _queued_command(
                    "qc1",
                    "u2",
                    [
                        {"type": "text", "text": "compare [Image #6] with [Image #4]"},
                        first,
                        second,
                    ],
                    paste_ids=[4, 6],
                ),
                _assistant("a2", "qc1", "ok"),
            ]
        )

        # Both placeholders resolved — neither survives as literal text.
        assert "[Image #6]" not in html
        assert "[Image #4]" not in html
        # And they resolved BY ID: #6 is the second block, #4 the first, so
        # "second" precedes "first" in the rendered card.
        assert html.index("second") < html.index("first"), (
            "placeholders resolved positionally, not from the recorded ids"
        )
