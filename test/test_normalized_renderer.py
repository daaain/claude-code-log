"""Contract for rendering already-normalized provider entries."""

from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import render_normalized_session_file
from claude_code_log.models import DetailLevel


class FakeRenderer:
    def __init__(self, result: str | None = "rendered") -> None:
        self.result = result
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def generate_session(self, *args: Any, **kwargs: Any) -> str | None:
        self.calls.append((args, kwargs))
        return self.result


def test_normalized_renderer_dispatches_options_title_and_creates_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = FakeRenderer()
    renderer_options: list[tuple[Any, ...]] = []

    def fake_get_renderer(*args: Any, **kwargs: Any) -> FakeRenderer:
        renderer_options.append((args, kwargs))
        return renderer

    monkeypatch.setattr("claude_code_log.converter.get_renderer", fake_get_renderer)
    output = tmp_path / "nested" / "session.md"

    returned = render_normalized_session_file(
        [],
        "abcdef123456",
        output,
        format="md",
        detail=DetailLevel.MINIMAL,
        compact=True,
        no_timestamps=True,
        no_recaps=True,
    )

    assert returned == output
    assert output.read_text(encoding="utf-8") == "rendered"
    assert renderer_options == [
        (
            ("md", None),
            {
                "detail": DetailLevel.MINIMAL,
                "compact": True,
                "no_timestamps": True,
                "no_recaps": True,
            },
        )
    ]
    assert renderer.calls[0][0][1:3] == ("abcdef123456", "Session abcdef12")
    assert renderer.calls[0][1]["output_dir"] == output.parent


def test_normalized_renderer_uses_explicit_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = FakeRenderer()

    def fake_get_renderer(*args: Any, **kwargs: Any) -> FakeRenderer:
        return renderer

    monkeypatch.setattr("claude_code_log.converter.get_renderer", fake_get_renderer)

    render_normalized_session_file([], "session", tmp_path / "out.html", title="Title")

    assert renderer.calls[0][0][2] == "Title"


def test_normalized_renderer_rejects_invalid_renderer_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get_renderer(*args: Any, **kwargs: Any) -> FakeRenderer:
        return FakeRenderer(None)

    monkeypatch.setattr("claude_code_log.converter.get_renderer", fake_get_renderer)

    with pytest.raises(AssertionError):
        render_normalized_session_file([], "session", tmp_path / "out.html")
