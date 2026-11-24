"""The browser profile a worker keeps between runs starts each run with no
page storage in it (``conftest.clear_stored_page_data``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from test.conftest import clear_stored_page_data

pytestmark = pytest.mark.browser


def test_a_run_starts_without_the_last_ones_page_storage(
    playwright: Any, browser_type_launch_args: Any, tmp_path: Path
) -> None:
    """Checked with a real profile: the folder names are Chromium's own."""
    html = tmp_path / "page.html"
    html.write_text("<!doctype html><title>page</title>", encoding="utf-8")
    profile = tmp_path / "profile"

    def run(script: str) -> Any:
        context = playwright.chromium.launch_persistent_context(
            profile, **browser_type_launch_args
        )
        try:
            page = context.new_page()
            page.goto(html.as_uri())
            return page.evaluate(script)
        finally:
            context.close()

    run("localStorage.setItem('left', 'behind')")
    assert run("localStorage.getItem('left')") == "behind", "nothing to clear"

    clear_stored_page_data(profile)
    assert run("localStorage.getItem('left')") is None
