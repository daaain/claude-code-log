"""A fork point whose node is normally dropped must still be anchorable (#233).

When the fork point is a content-less / unsupported system entry (the real
example: ``system/turn_duration``), the factory drops it, yet the DAG still
sees it as a within-session fork. Pre-fix, the fork's nav item and the branch
back-links fell back to the parent **session header** (``#msg-d-2``) — a
confusing anchor that jumped to the wrong place.

The fix synthesizes a minimal ``SystemMessage`` placeholder for any
content-less *fork-point* system node so it becomes a real ⟂ landmark, and the
existing fork machinery resolves to it. As a belt-and-suspenders, when the fork
point is genuinely unresolvable (e.g. ghosted at reduced detail), the branch
back-link renders "from ⟂ Fork point" as plain text rather than a wrong anchor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import re

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import HtmlRenderer, generate_html
from claude_code_log.html.system_formatters import format_session_header_content
from claude_code_log.models import (
    DetailLevel,
    MessageMeta,
    SessionHeaderMessage,
    SystemTranscriptEntry,
)
from claude_code_log.renderer import (
    RenderingContext,
    _fork_placeholder_content,
    _is_unrendered_within_session_fork,
)


# ----------------------------- unit: detection -------------------------------


class TestIsUnrenderedWithinSessionFork:
    def _ctx(self, targets: dict[str, list[str]]) -> RenderingContext:
        ctx = RenderingContext()
        ctx.junction_targets = targets
        return ctx

    def test_within_session_branch_targets_match(self):
        # Branch session ids carry an '@' ({line}@{uuid12}).
        ctx = self._ctx({"fork": ["s1@aaaaaaaaaaaa", "s1@bbbbbbbbbbbb"]})
        assert _is_unrendered_within_session_fork("fork", ctx) is True

    def test_cross_session_continuation_does_not_match(self):
        # A continuation (no '@') is not a within-session fork.
        ctx = self._ctx({"fork": ["s2", "s3"]})
        assert _is_unrendered_within_session_fork("fork", ctx) is False

    def test_unknown_uuid_does_not_match(self):
        ctx = self._ctx({"fork": ["s1@aaaaaaaaaaaa"]})
        assert _is_unrendered_within_session_fork("other", ctx) is False


# ----------------------------- unit: placeholder -----------------------------


def _system_entry(subtype: str | None) -> SystemTranscriptEntry:
    raw: dict[str, Any] = {
        "type": "system",
        "uuid": "sd",
        "parentUuid": "a1",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/x",
        "sessionId": "s1",
        "version": "1.0",
        "timestamp": "2025-01-01T00:00:02Z",
    }
    if subtype is not None:
        raw["subtype"] = subtype
    return SystemTranscriptEntry.model_validate(raw)


class TestForkPlaceholderContent:
    def test_label_is_raw_subtype(self):
        content = _fork_placeholder_content(_system_entry("turn_duration"))
        assert content.text == "(turn_duration)"
        assert content.level == "info"
        assert content.meta.uuid == "sd"

    def test_label_falls_back_to_system(self):
        content = _fork_placeholder_content(_system_entry(None))
        assert content.text == "(system)"


# ----------------------------- unit: branch back-link ------------------------


def _branch_header(parent_message_index: int | None) -> SessionHeaderMessage:
    return SessionHeaderMessage(
        title="Branch • abcdef01",
        session_id="s1@abcdef012345",
        is_branch=True,
        parent_session_id="s1",
        parent_message_index=parent_message_index,
        meta=MessageMeta(uuid="b1", session_id="s1@abcdef012345", timestamp="t"),
    )


class TestBranchBacklinkRendering:
    def test_resolved_fork_renders_anchor(self):
        html = format_session_header_content(_branch_header(42))
        assert '<a href="#msg-d-42" class="branch-backlink">' in html
        assert "Fork point" in html

    def test_unresolved_fork_renders_plain_text_not_dangling_anchor(self):
        # parent_message_index None → plain <span>, never an anchor to the
        # session header (the #233 bug was '#msg-d-2').
        html = format_session_header_content(_branch_header(None))
        assert '<span class="branch-backlink">' in html
        assert "Fork point" in html
        assert "<a href=" not in html


# ----------------------------- integration: render ---------------------------


def _raw_user(uuid: str, parent: str | None, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/x",
        "sessionId": "s1",
        "version": "1.0",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _raw_assistant(uuid: str, parent: str, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/x",
        "sessionId": "s1",
        "version": "1.0",
        "timestamp": ts,
        "requestId": "r-" + uuid,
        "message": {
            "id": "m-" + uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": text}],
        },
    }


def _raw_turn_duration(uuid: str, parent: str, ts: str) -> dict[str, Any]:
    # A content-less system fork node — mirrors the real turn_duration entry.
    return {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 1000,
        "messageCount": 3,
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/x",
        "sessionId": "s1",
        "version": "1.0",
        "timestamp": ts,
    }


def _write_fork_jsonl(tmp_path: Path) -> Path:
    # u0 → a1 → sd(turn_duration) ⇒ { u1 (rewind A), u2 (rewind B) }
    # Two human prompts at different timestamps = a genuine within-session fork
    # whose fork point is the content-less system node sd.
    entries = [
        _raw_user("u0", None, "go", "2025-01-01T00:00:00Z"),
        _raw_assistant("a1", "u0", "working on it", "2025-01-01T00:00:01Z"),
        _raw_turn_duration("sd", "a1", "2025-01-01T00:00:02Z"),
        _raw_user("u1", "sd", "actually try approach A", "2025-01-01T00:05:00Z"),
        _raw_assistant("a2", "u1", "doing A", "2025-01-01T00:05:01Z"),
        _raw_user("u2", "sd", "no, approach B instead", "2025-01-01T00:09:00Z"),
        _raw_assistant("a3", "u2", "doing B", "2025-01-01T00:09:01Z"),
    ]
    fixture = tmp_path / "fork.jsonl"
    with open(fixture, "w") as f:
        import json

        for e in entries:
            f.write(json.dumps(e) + "\n")
    return fixture


def _build_fork_fixture(tmp_path: Path) -> str:
    return generate_html(load_transcript(_write_fork_jsonl(tmp_path)), "fork")


def _render_fork_at_detail(tmp_path: Path, detail: DetailLevel) -> str:
    messages = load_transcript(_write_fork_jsonl(tmp_path))
    renderer = HtmlRenderer()
    renderer.detail = detail
    return renderer.generate(messages, f"fork {detail.value}")


class TestForkInvisibleNodeIntegration:
    def test_placeholder_landmark_rendered(self, tmp_path: Path):
        html = _build_fork_fixture(tmp_path)
        # The dropped fork node now renders as a minimal system landmark whose
        # label is the raw subtype.
        assert "(turn_duration)" in html
        # …and it is a *real node* carrying the fork node's identity: the
        # debug-info shows the placeholder's own uuid → its parent. The exact
        # fragment (not a bare "sd" substring, which matches unrelated markup)
        # proves the synthesized node rendered with the fork uuid.
        assert "<div class='debug-info'>sd &rarr; a1</div>" in html

    def test_fork_box_and_anchors_resolve_to_placeholder_not_session_header(
        self, tmp_path: Path
    ):
        html = _build_fork_fixture(tmp_path)
        # The body fork-point box renders (it attaches to the now-visible node).
        assert "fork-point-header" in html
        # The branch back-links must NOT fall back to the session header (d-2).
        backlinks = re.findall(r'class="branch-from">from <a href="(#msg-d-\d+)"', html)
        assert len(backlinks) == 2, backlinks
        assert "#msg-d-2" not in backlinks
        # All back-links point at one and the same fork landmark.
        assert len(set(backlinks)) == 1, backlinks
        # The placeholder card carries the fork node's uuid in debug-info, and
        # the back-link target index matches the placeholder's own anchor id.
        fork_idx = backlinks[0].rsplit("-", 1)[-1]
        assert f"id='msg-d-{fork_idx}'" in html

    def test_nav_fork_item_has_working_anchor(self, tmp_path: Path):
        html = _build_fork_fixture(tmp_path)
        nav = re.search(r"<a href='(#msg-d-\d+)' class='fork-link'>", html)
        assert nav is not None, "nav fork item should have a real anchor, not a span"
        assert nav.group(1) != "#msg-d-2"


class TestForkSurvivesDetailFiltering:
    """When the fork node's own message is filtered by --detail, the fork
    point must still render as a landmark at its position, and the branch
    back-links must stay active (#233 follow-up — fork points get the same
    always-visible treatment as the branches they connect)."""

    # The placeholder (a content-less ``turn_duration`` SystemMessage) has
    # detail_visibility=FULL, so it's filtered at every level below FULL —
    # exercising the fork_only landmark path at all reduced levels.
    REDUCED = [
        DetailLevel.HIGH,
        DetailLevel.LOW,
        DetailLevel.MINIMAL,
        DetailLevel.USER_ONLY,
    ]

    def test_fork_box_survives_but_body_suppressed_at_reduced_detail(
        self, tmp_path: Path
    ):
        for detail in self.REDUCED:
            html = _render_fork_at_detail(tmp_path, detail)
            # The fork node's own message body is filtered out…
            assert "(turn_duration)" not in html, detail
            assert "<div class='debug-info'>sd &rarr; a1</div>" not in html, detail
            # …but the fork-point box still renders as a landmark.
            assert "fork-point-header" in html, detail

    def test_backlinks_reactivate_to_the_landmark_at_reduced_detail(
        self, tmp_path: Path
    ):
        for detail in self.REDUCED:
            html = _render_fork_at_detail(tmp_path, detail)
            backlinks = re.findall(
                r'class="branch-from">from <a href="(#msg-d-\d+)"', html
            )
            # Both branch back-links are ACTIVE links (not the plain-text span
            # fallback), pointing at one landmark, never the session header.
            assert len(backlinks) == 2, (detail, backlinks)
            assert len(set(backlinks)) == 1, (detail, backlinks)
            assert "#msg-d-2" not in backlinks, (detail, backlinks)
            # The landmark anchor (on the fork-point box) matches the target.
            fork_idx = backlinks[0].rsplit("-", 1)[-1]
            assert f"class='fork-point' id='msg-d-{fork_idx}'" in html, detail

    def test_no_dead_anchors_at_reduced_detail(self, tmp_path: Path):
        # Every #msg-d-N href must have a matching id in the same document.
        for detail in self.REDUCED:
            html = _render_fork_at_detail(tmp_path, detail)
            ids = set(re.findall(r"id=['\"]msg-d-(\d+)['\"]", html))
            hrefs = set(re.findall(r"href=['\"]#msg-d-(\d+)['\"]", html))
            assert not (hrefs - ids), (detail, sorted(hrefs - ids))

    def test_branch_headers_always_visible_keeps_fork_anchored(self):
        """Pin the load-bearing invariant the fork_only no-dead-anchor
        guarantee rests on (monk review note): branch session headers must
        survive detail filtering at EVERY level. If they could be ghosted, a
        2-branch fork could drop below 2 survivors, the fork-point box would be
        suppressed, and a kept fork_only slot would render with no anchor id —
        silently dangling a sibling branch's back-link.

        ``SessionHeaderMessage`` declares no ``detail_visibility``, so
        ``visible_at`` is True at every level. Assert that directly so a future
        ``detail_visibility`` added to the class trips HERE rather than as a
        downstream dead anchor.
        """
        branch = SessionHeaderMessage(
            title="Branch • abcdef01",
            session_id="s1@abcdef012345",
            is_branch=True,
            meta=MessageMeta(uuid="b1", session_id="s1@abcdef012345", timestamp="t"),
        )
        for detail in (
            DetailLevel.FULL,
            DetailLevel.HIGH,
            DetailLevel.LOW,
            DetailLevel.MINIMAL,
            DetailLevel.USER_ONLY,
        ):
            assert branch.visible_at(detail) is True, (
                f"branch SessionHeaderMessage must stay visible at {detail} — "
                "fork_only's no-dead-anchor guarantee depends on a 2-branch "
                "fork keeping >=2 visible branches. A detail_visibility on "
                "SessionHeaderMessage would break it; update the fork_only "
                "ghost-pass logic (renderer.py) accordingly."
            )
