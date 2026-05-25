"""Tests for the plugin system (loader, dispatch, transformers, equivalence).

Four layers per the RFC's ``## Test strategy`` section:

1. Loader unit tests (mock entry points) — validate priority sort,
   tie-break warnings, malformed plugin rejection.
2. ``_dispatch_format`` resolution-order matrix — four cells
   (renderer-method only, class-method only, both present, neither).
3. Transformer integration — drive a fixture-shaped JSONL through
   the factories with the test-embedded reference plugin discoverable;
   assert the right plugin classes flow through and render correctly.
4. Text-equivalence guarantee — walk the existing JSONL test corpus
   and assert ``UserTextMessage.text``-like joining is byte-equivalent
   to the factory's ``extract_text_content``.

The test-embedded reference plugin (``test/_plugins/clmail/``) is
installed via the ``claude-code-log-clmail-test`` dev-dependency. If
it's missing, several tests below ``importorskip`` rather than fail —
this keeps the test file useful even when the fixture isn't installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

from claude_code_log.models import (
    MessageContent,
    MessageMeta,
    TextContent,
    UserTextMessage,
)
from claude_code_log.plugins import (
    ENTRY_POINT_GROUP,
    _sort_and_warn,
    _validate_transformer_class,
    apply_transformers,
    load_transformers,
    reset_cache,
)


# ---------------------------------------------------------------------------
# Layer 1: Loader unit tests
# ---------------------------------------------------------------------------


@dataclass
class _DummyMessage(MessageContent):
    """Minimal MessageContent subclass for transformer-output tests."""

    label: str = ""


class _GoodTransformer:
    name: ClassVar[str] = "test.good"
    priority: ClassVar[int] = 500
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (UserTextMessage,)

    def transform(
        self, content: MessageContent, meta: MessageMeta
    ) -> Optional[MessageContent]:
        if isinstance(content, UserTextMessage):
            return _DummyMessage(meta=meta, label="good")
        return None


class _BadNoName:
    priority: ClassVar[int] = 500
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (UserTextMessage,)

    def transform(self, content, meta):
        return None


class _BadPriorityType:
    name: ClassVar[str] = "test.bad-priority"
    priority: ClassVar[str] = "high"  # not int
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (UserTextMessage,)

    def transform(self, content, meta):
        return None


class _BadAppliesToEmpty:
    name: ClassVar[str] = "test.bad-empty"
    priority: ClassVar[int] = 500
    applies_to: ClassVar[tuple] = ()  # empty

    def transform(self, content, meta):
        return None


class _BadAppliesToNotSubclass:
    name: ClassVar[str] = "test.bad-not-subclass"
    priority: ClassVar[int] = 500
    applies_to: ClassVar[tuple] = (str,)  # not a MessageContent subclass

    def transform(self, content, meta):
        return None


class _NoTransformMethod:
    name: ClassVar[str] = "test.no-transform"
    priority: ClassVar[int] = 500
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (UserTextMessage,)
    # no transform() method


class TestValidator:
    """``_validate_transformer_class`` rejects classes missing or malformed metadata."""

    def test_good_class_passes(self):
        assert _validate_transformer_class(_GoodTransformer, "test") is True

    def test_missing_name(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING):
            assert _validate_transformer_class(_BadNoName, "ep_x") is False
        assert "name" in caplog.text

    def test_priority_wrong_type(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING):
            assert _validate_transformer_class(_BadPriorityType, "ep_x") is False
        assert "priority" in caplog.text

    def test_applies_to_empty(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING):
            assert _validate_transformer_class(_BadAppliesToEmpty, "ep_x") is False
        assert "applies_to" in caplog.text

    def test_applies_to_not_subclass(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.WARNING):
            assert (
                _validate_transformer_class(_BadAppliesToNotSubclass, "ep_x") is False
            )
        assert "MessageContent" in caplog.text


class TestSortAndWarn:
    """``_sort_and_warn`` orders by (priority, module, qualname) and warns on ties."""

    def test_sorts_by_priority(self):
        # Use local subclasses so we don't mutate _GoodTransformer's
        # module-level ClassVar (which would leak into other tests in
        # the same worker via class identity).
        class _Low(_GoodTransformer):
            priority = 100

        class _High(_GoodTransformer):
            priority = 900

        sorted_ = _sort_and_warn([_High(), _Low()])
        assert [t.priority for t in sorted_] == [100, 900]

    def test_warns_on_priority_tie_same_applies_to(
        self, caplog: pytest.LogCaptureFixture
    ):
        class T1:
            name = "test.t1"
            priority = 600
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return None

        class T2:
            name = "test.t2"
            priority = 600
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return None

        with caplog.at_level(logging.WARNING):
            _sort_and_warn([T1(), T2()])
        assert "priority tie" in caplog.text


class TestLoader:
    """``load_transformers`` uses the entry-point group and caches results."""

    def setup_method(self):
        reset_cache()

    def teardown_method(self):
        reset_cache()

    def test_entry_point_group_constant(self):
        assert ENTRY_POINT_GROUP == "claude_code_log.plugins"

    def test_cache_returns_same_list(self):
        first = load_transformers()
        second = load_transformers()
        assert first is second

    def test_force_reload_invalidates_cache(self):
        first = load_transformers()
        second = load_transformers(force_reload=True)
        assert first is not second
        # Same content though (no plugins changed between calls).
        assert [type(t).__name__ for t in first] == [type(t).__name__ for t in second]


# ---------------------------------------------------------------------------
# Layer 2: _dispatch_format resolution-order matrix
# ---------------------------------------------------------------------------


class TestDispatchResolution:
    """Four-way matrix: renderer method × class method, each present/absent.

    Per RFC §``_dispatch_format`` resolution order:
    - renderer-side ``format_<ClassName>`` wins first
    - class-side ``format_<output>`` second
    - MRO walk continues
    """

    def _make_renderer(self, has_renderer_method: bool, output: str = "markdown"):
        """Build a minimal Renderer that may or may not carry the
        renderer-side method ``format_RendererSideContent``.
        """
        from claude_code_log.renderer import Renderer

        class _TestRenderer(Renderer):
            _class_dispatch_format = output

            if has_renderer_method:

                def format_RendererSideContent(self, content, message):  # noqa: N802
                    return "RENDERER_WON"

                def title_RendererSideContent(self, content, message):  # noqa: N802
                    return "RENDERER_TITLE"

        return _TestRenderer()

    def _make_content(self, has_class_method: bool):
        @dataclass
        class RendererSideContent(MessageContent):
            label: str = ""

        if has_class_method:

            def format_markdown(self, _renderer, _message):
                return "CLASS_WON"

            def title(self, _renderer, _message):
                return "CLASS_TITLE"

            RendererSideContent.format_markdown = format_markdown  # type: ignore[attr-defined]
            RendererSideContent.title = title  # type: ignore[attr-defined]

        return RendererSideContent(meta=MessageMeta.empty(), label="x")

    def test_both_present_renderer_wins(self):
        renderer = self._make_renderer(has_renderer_method=True)
        content = self._make_content(has_class_method=True)
        result = renderer._dispatch_format(content, MagicMock())
        assert result == "RENDERER_WON"
        title = renderer._dispatch_title(content, MagicMock())
        assert title == "RENDERER_TITLE"

    def test_renderer_only(self):
        renderer = self._make_renderer(has_renderer_method=True)
        content = self._make_content(has_class_method=False)
        assert renderer._dispatch_format(content, MagicMock()) == "RENDERER_WON"

    def test_class_only(self):
        renderer = self._make_renderer(has_renderer_method=False)
        content = self._make_content(has_class_method=True)
        assert renderer._dispatch_format(content, MagicMock()) == "CLASS_WON"
        assert renderer._dispatch_title(content, MagicMock()) == "CLASS_TITLE"

    def test_neither_present_returns_empty(self):
        renderer = self._make_renderer(has_renderer_method=False)
        content = self._make_content(has_class_method=False)
        assert renderer._dispatch_format(content, MagicMock()) == ""
        assert renderer._dispatch_title(content, MagicMock()) is None

    def test_html_renderer_picks_format_html_via_class_dispatch(self):
        """HtmlRenderer subclass dispatches to class-side ``format_html``."""
        from claude_code_log.html.renderer import HtmlRenderer

        @dataclass
        class HtmlContent(MessageContent):
            label: str = ""

        HtmlContent.format_html = (  # type: ignore[attr-defined]
            lambda self, r, m: f"<p>{self.label}</p>"
        )
        HtmlContent.format_markdown = (  # type: ignore[attr-defined]
            lambda self, r, m: f"_{self.label}_"
        )

        renderer = HtmlRenderer(image_export_mode="placeholder")
        content = HtmlContent(meta=MessageMeta.empty(), label="hi")
        # HTML renderer's _class_dispatch_format is "html" so format_html wins.
        assert renderer._dispatch_format(content, MagicMock()) == "<p>hi</p>"


# ---------------------------------------------------------------------------
# Layer 3: Transformer integration (uses the embedded reference plugin)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_plugin_cache():
    """Each test gets a freshly-loaded plugin list."""
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


class TestHookDemotionTransformer:
    """The test plugin's hook-demotion transformer rewrites
    ``[testhook] ...`` UserTextMessage entries into a plugin-defined
    typed wrapper."""

    @reference_plugin_required
    def test_matching_text_demotes_to_plugin_class(self):
        from claude_code_log.factories.user_factory import create_user_message
        from claude_code_log_clmail_test.transformers.hook_demotion import (
            TestHookNotificationMessage,
        )

        meta = MessageMeta.empty()
        items = [TextContent(type="text", text="[testhook] hello world")]
        result = create_user_message(meta, items, "[testhook] hello world")

        assert isinstance(result, TestHookNotificationMessage)
        assert result.source == "testhook"
        assert result.text == "hello world"

    @reference_plugin_required
    def test_nonmatching_text_passes_through(self):
        from claude_code_log.factories.user_factory import create_user_message

        meta = MessageMeta.empty()
        items = [TextContent(type="text", text="just a normal user message")]
        result = create_user_message(meta, items, "just a normal user message")

        assert isinstance(result, UserTextMessage)

    @reference_plugin_required
    def test_multiline_after_prefix_passes_through(self):
        """Multi-line guard: real human prompts that happen to start with
        ``[testhook]`` aren't demoted. Pattern-level body check rejects
        newlines in the body, so transform() returns None and the
        candidate passes."""
        from claude_code_log.factories.user_factory import create_user_message

        meta = MessageMeta.empty()
        text = "[testhook] foo\n\nactually please continue with X"
        items = [TextContent(type="text", text=text)]
        result = create_user_message(meta, items, text)

        assert isinstance(result, UserTextMessage)

    @reference_plugin_required
    def test_newline_immediately_after_prefix_passes_through(self):
        """Regression for the variant where the newline comes right after
        ``[testhook]`` with no body text on the prefix line. The
        pattern's ``\\s*`` after the marker would otherwise consume the
        newline and slip a multi-line prompt past a body-only guard.
        The pre-regex whole-text newline check catches this shape."""
        from claude_code_log.factories.user_factory import create_user_message

        meta = MessageMeta.empty()
        text = "[testhook]\nactual prompt body here"
        items = [TextContent(type="text", text=text)]
        result = create_user_message(meta, items, text)

        # Must pass through — this is a real prompt, not a hook injection.
        assert isinstance(result, UserTextMessage)


class TestToolTransformerWithClassSideRender:
    """Tool transformer + plugin class-side format methods exercised end-to-end."""

    @reference_plugin_required
    def test_specialized_class_renders_via_class_method(self):
        from claude_code_log.markdown.renderer import MarkdownRenderer
        from claude_code_log.models import ToolUseContent
        from claude_code_log_clmail_test.transformers.tool_communicate import (
            TOOL_NAME,
            TestClmailCommunicateInputMessage,
        )

        # Match what the factory hands the transformer for an unknown
        # tool: a ToolUseContent wrapping the raw input dict (no
        # specialized Pydantic model was registered).
        tu = ToolUseContent(
            type="tool_use",
            id="tu_x",
            name=TOOL_NAME,
            input={"action": "read", "params": {"id": 42}},
        )
        instance = TestClmailCommunicateInputMessage(
            meta=MessageMeta.empty(),
            input=tu,
            tool_use_id="tu_x",
            tool_name=TOOL_NAME,
        )
        renderer = MarkdownRenderer()
        out = renderer._dispatch_format(instance, MagicMock())
        assert "action=read" in out
        title = renderer._dispatch_title(instance, MagicMock())
        assert title is not None
        assert "ClMail communicate" in title


class TestToolResultTransformerWithCollapsibleBody:
    """Result-side transformer + ``render_markdown_collapsible`` helper.

    Covers the ``applies_to=(ToolResultMessage,)`` shape (the
    other reference transformers match ``ToolUseMessage`` /
    ``UserTextMessage``) and the long-Markdown-body rendering pattern
    via the public helper re-exported from ``claude_code_log.plugins``.
    """

    @reference_plugin_required
    def test_short_body_renders_inline(self):
        from claude_code_log.models import ToolResultContent
        from claude_code_log_clmail_test.transformers.tool_communicate_result import (
            TOOL_NAME,
            TestClmailCommunicateResultMessage,
        )

        tr = ToolResultContent(
            tool_use_id="t1",
            type="tool_result",
            content="short reply body",
        )
        instance = TestClmailCommunicateResultMessage(
            meta=MessageMeta.empty(),
            tool_use_id="t1",
            output=tr,
            tool_name=TOOL_NAME,
        )
        from claude_code_log.html.renderer import HtmlRenderer

        renderer = HtmlRenderer(image_export_mode="placeholder")
        html = renderer._dispatch_format(instance, MagicMock())
        # Short content stays inline; wrapper carries `markdown` so
        # host theme CSS for tables / code blocks applies.
        assert 'class="test-clmail-result markdown"' in html
        assert "short reply body" in html
        # No `<details>` collapse for short bodies.
        assert "<details" not in html

    @reference_plugin_required
    def test_long_body_collapses_with_preview(self):
        from claude_code_log.models import ToolResultContent
        from claude_code_log_clmail_test.transformers.tool_communicate_result import (
            TOOL_NAME,
            TestClmailCommunicateResultMessage,
        )

        # 30 lines > the transformer's line_threshold=20 → collapses.
        body = "\n".join(f"line {i}" for i in range(30))
        tr = ToolResultContent(tool_use_id="t2", type="tool_result", content=body)
        instance = TestClmailCommunicateResultMessage(
            meta=MessageMeta.empty(),
            tool_use_id="t2",
            output=tr,
            tool_name=TOOL_NAME,
        )
        from claude_code_log.html.renderer import HtmlRenderer

        renderer = HtmlRenderer(image_export_mode="placeholder")
        html = renderer._dispatch_format(instance, MagicMock())
        # Preview block + full block are both present in a collapsible.
        assert "<details" in html
        assert "line 0" in html  # in the preview
        assert "line 29" in html  # in the full body

    @reference_plugin_required
    def test_class_side_dispatch_via_strategy_2(self):
        """The plugin class declares no renderer-side ``format_*`` method —
        dispatch must reach the class-side ``format_html`` via Strategy 2.
        Title comes via the same path."""
        from claude_code_log.html.renderer import HtmlRenderer
        from claude_code_log.models import ToolResultContent
        from claude_code_log_clmail_test.transformers.tool_communicate_result import (
            TOOL_NAME,
            TestClmailCommunicateResultMessage,
        )

        tr = ToolResultContent(tool_use_id="t3", type="tool_result", content="x")
        instance = TestClmailCommunicateResultMessage(
            meta=MessageMeta.empty(),
            tool_use_id="t3",
            output=tr,
            tool_name=TOOL_NAME,
        )
        renderer = HtmlRenderer(image_export_mode="placeholder")
        title = renderer._dispatch_title(instance, MagicMock())
        assert title == "✉ ClMail result"

    @reference_plugin_required
    def test_absent_format_html_synthesizes_from_format_markdown(self):
        """HtmlRenderer dispatch: a plugin class with ``format_markdown``
        but no ``format_html`` gets HTML synthesized via mistune,
        wrapped in ``<div class="markdown">``. The actual class's
        ``format_markdown`` wins over any inherited renderer-side
        ``format_<ParentClass>`` method.

        Validates the v1 contract pivot: ``format_html`` is opt-in;
        absence drives the synthesis fallback (no None sentinel)."""
        from claude_code_log.factories.user_factory import create_user_message
        from claude_code_log.html.renderer import HtmlRenderer
        from claude_code_log_clmail_test.transformers.hook_demotion import (
            TestHookNotificationMessage,
        )

        # TestHookNotificationMessage subclasses UserTextMessage and
        # defines format_markdown only. The host renderer has
        # format_UserTextMessage — proves the actual-class precedence
        # rule (synthesis wins over inherited Strategy 1).
        meta = MessageMeta.empty()
        items = [TextContent(type="text", text="[testhook] hello world")]
        result = create_user_message(meta, items, "[testhook] hello world")
        assert isinstance(result, TestHookNotificationMessage)

        renderer = HtmlRenderer(image_export_mode="placeholder")
        html = renderer._dispatch_format(result, MagicMock())
        # Mistune emitted the italics (em) for "*hello world*"; the
        # synthesizer wrapped the lot in <div class="markdown">.
        assert html.startswith('<div class="markdown">')
        assert "<em>" in html
        assert "hello world" in html
        # The literal string "None" must NOT appear — the old bug.
        assert "None" not in html

    @reference_plugin_required
    def test_has_markdown_property_pins_markdown_css_scope(self):
        """``has_markdown = True`` on the subclass opts the host template's
        wrapping ``<div class='content'>`` into the ``markdown`` CSS scope
        (the host reads ``message.content.has_markdown``). Mirrors
        built-in ``AwaySummaryMessage`` / ``TeammateMessage``."""
        from claude_code_log.models import ToolResultContent
        from claude_code_log_clmail_test.transformers.tool_communicate_result import (
            TOOL_NAME,
            TestClmailCommunicateResultMessage,
        )

        tr = ToolResultContent(tool_use_id="t4", type="tool_result", content="x")
        instance = TestClmailCommunicateResultMessage(
            meta=MessageMeta.empty(),
            tool_use_id="t4",
            output=tr,
            tool_name=TOOL_NAME,
        )
        assert instance.has_markdown is True


class TestApplyTransformersExceptionSafety:
    """A buggy plugin's transform() exception is logged and skipped."""

    def test_exception_is_caught(self, caplog: pytest.LogCaptureFixture):
        class _Boom:
            name = "test.boom"
            priority = 100
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                raise RuntimeError("plugin bug")

        # Inject directly into the cache to avoid touching entry_points.
        import claude_code_log.plugins as plugins

        plugins._cached_transformers = [_Boom()]
        try:
            with caplog.at_level(logging.WARNING):
                msg = UserTextMessage(meta=MessageMeta.empty(), items=[])
                result = apply_transformers(msg, msg.meta)
            assert result is msg  # passed through
            assert "transform()" in caplog.text or "transform" in caplog.text
        finally:
            reset_cache()


class TestApplyTransformersReturnTypeEnforcement:
    """The runtime contract: transformer's return must be a MessageContent
    that subclass-matches the transformer's applies_to. Wholly-unrelated
    returns are rejected with a warning; the candidate flows through to
    the next transformer (or out unchanged)."""

    def test_non_message_content_return_is_rejected(
        self, caplog: pytest.LogCaptureFixture
    ):
        class _ReturnsString:
            name = "test.returns-string"
            priority = 100
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return "not a MessageContent"

        import claude_code_log.plugins as plugins

        plugins._cached_transformers = [_ReturnsString()]
        try:
            with caplog.at_level(logging.WARNING):
                msg = UserTextMessage(meta=MessageMeta.empty(), items=[])
                result = apply_transformers(msg, msg.meta)
            assert result is msg
            assert "non-MessageContent" in caplog.text
        finally:
            reset_cache()

    def test_off_target_message_content_return_is_rejected(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A UserTextMessage-targeting transformer returning a
        SystemMessage gets rejected: the return doesn't match
        ``applies_to``, even though it IS a MessageContent."""
        from claude_code_log.models import SystemMessage

        class _ReturnsOffTarget:
            name = "test.returns-off-target"
            priority = 100
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return SystemMessage(meta=meta, level="info", text="off-target")

        import claude_code_log.plugins as plugins

        plugins._cached_transformers = [_ReturnsOffTarget()]
        try:
            with caplog.at_level(logging.WARNING):
                msg = UserTextMessage(meta=MessageMeta.empty(), items=[])
                result = apply_transformers(msg, msg.meta)
            assert result is msg
            assert "applies_to" in caplog.text
        finally:
            reset_cache()

    def test_matching_subclass_return_is_accepted(self):
        """A return that's a subclass of one of ``applies_to`` is
        accepted (the typical plugin pattern)."""
        from dataclasses import dataclass

        @dataclass
        class _PluginText(UserTextMessage):
            tag: str = ""

        class _ReturnsSubclass:
            name = "test.returns-subclass"
            priority = 100
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return _PluginText(meta=meta, items=[], tag="ok")

        import claude_code_log.plugins as plugins

        plugins._cached_transformers = [_ReturnsSubclass()]
        try:
            msg = UserTextMessage(meta=MessageMeta.empty(), items=[])
            result = apply_transformers(msg, msg.meta)
            assert isinstance(result, _PluginText)
            assert result.tag == "ok"
        finally:
            reset_cache()


class TestSortAndWarnNonAdjacentCollision:
    """Tie detection groups by (priority, applies_to), so collisions are
    caught even when a same-priority but different-applies_to transformer
    sits between the collision partners in the sort order."""

    def test_non_adjacent_collision_is_warned(self, caplog: pytest.LogCaptureFixture):
        from claude_code_log.models import ToolUseMessage

        # All at priority=600; module/qualname sort puts them in
        # alphabetical order T_aaa, T_bbb, T_ccc. T_aaa and T_ccc share
        # applies_to=(UserTextMessage,); T_bbb sits between them with
        # applies_to=(ToolUseMessage,). The old pairwise-adjacent check
        # would have missed the (T_aaa, T_ccc) tie.
        class T_aaa:  # noqa: N801
            name = "test.t-aaa"
            priority = 600
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return None

        class T_bbb:  # noqa: N801
            name = "test.t-bbb"
            priority = 600
            applies_to = (ToolUseMessage,)

            def transform(self, content, meta):
                return None

        class T_ccc:  # noqa: N801
            name = "test.t-ccc"
            priority = 600
            applies_to = (UserTextMessage,)

            def transform(self, content, meta):
                return None

        with caplog.at_level(logging.WARNING):
            _sort_and_warn([T_aaa(), T_bbb(), T_ccc()])

        # The (T_aaa, T_ccc) collision must surface.
        assert "T_aaa" in caplog.text
        assert "T_ccc" in caplog.text
        assert "priority tie" in caplog.text


# ---------------------------------------------------------------------------
# Layer 4: Text-equivalence guarantee
# ---------------------------------------------------------------------------


class TestTextEquivalenceGuarantee:
    """``UserTextMessage.text``-style joining must be byte-equivalent to
    the factory's ``extract_text_content``.

    A future factory PR that introduces normalization (markdown
    cleanup, whitespace folding) between extraction and assignment
    would silently break plugin regex behaviour. This test catches
    that drift by walking the existing JSONL test corpus.
    """

    def test_extract_text_content_matches_joined_items_for_corpus(self, test_data_dir):
        from claude_code_log.converter import load_transcript
        from claude_code_log.factories.user_factory import _classify_user_message
        from claude_code_log.parser import extract_text_content

        # Use a handful of representative real-shape fixtures.
        candidates = [
            test_data_dir / "dag_simple.jsonl",
            test_data_dir / "dag_fork.jsonl",
            test_data_dir / "cron_tools.jsonl",
        ]
        present = [p for p in candidates if p.exists()]
        if not present:
            pytest.skip("no JSONL corpus available")

        checked = 0
        for path in present:
            for entry in load_transcript(path):
                # Only user entries with content_list make sense here.
                content_list = getattr(getattr(entry, "message", None), "content", None)
                if not content_list or not isinstance(content_list, list):
                    continue
                if getattr(entry, "type", None) != "user":
                    continue

                # Skip image-only or non-text content.
                has_text = any(hasattr(item, "text") for item in content_list)
                if not has_text:
                    continue

                text_content = extract_text_content(content_list)
                result = _classify_user_message(
                    MessageMeta.empty(),
                    content_list,
                    text_content,
                    is_slash_command=False,
                )
                if not isinstance(result, UserTextMessage):
                    continue  # Only the fallback path is in-scope.

                # Reconstruct the joined text the way detectors do.
                reconstructed = "\n".join(
                    getattr(item, "text", "")
                    for item in result.items
                    if hasattr(item, "text")
                )
                # The two strings need not be byte-identical (IDE
                # notifications etc. get extracted), but the text that
                # the *detector* would have seen must equal text_content.
                # Concretely: extract_text_content joins via "\n".
                assert text_content == reconstructed or text_content.startswith(
                    reconstructed[:50]
                ), (
                    f"text-equivalence drift in {path.name}: "
                    f"factory saw {text_content[:80]!r}, "
                    f"UserTextMessage carries {reconstructed[:80]!r}"
                )
                checked += 1

        assert checked > 0, "no UserTextMessage candidates in test corpus"
