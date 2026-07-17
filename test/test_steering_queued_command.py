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


def _queued_command(uuid, parent, prompt, *, version=_MODERN, sid=_SID):
    """Modern in-DAG queued_command attachment paired with a 'remove'."""
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
        "attachment": {
            "type": "queued_command",
            "prompt": prompt,
            "commandMode": "prompt",
            "origin": {"kind": "human"},
            "timestamp": "2026-07-11T07:00:00.000Z",
        },
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
                _remove("[testhook] monk blocked — permission prompt"),
                _user("u2", "a1", "next real prompt"),
                _queued_command(
                    "qc1", "u2", "[testhook] monk blocked — permission prompt"
                ),
                _assistant("a2", "qc1", "ok"),
            ]
        )

        # Demoted → rendered as the marker, present exactly once, and NOT
        # promoted to a (would-be empty) steering card.
        assert "monk blocked — permission prompt" in html
        assert html.count("User (steering)") == 0


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
