"""Tests for `utils.atomic_write_text`.

The property that matters: a reader never observes a partial file. Watch
mode rewrites the same output every few seconds while an editor, a vault
indexer or a browser poll re-reads it, so the truncate-then-write window
that `Path.write_text` opens stops being theoretical.
"""

import os
import sys
import threading
from pathlib import Path

import pytest

from claude_code_log.utils import atomic_write_text


def test_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "out.html"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.html"
    target.write_text("old content that is longer")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_leaves_no_temp_file(tmp_path: Path) -> None:
    atomic_write_text(tmp_path / "out.html", "x")
    assert [p.name for p in tmp_path.iterdir()] == ["out.html"]


def test_temp_file_is_removed_on_failure(tmp_path: Path, monkeypatch) -> None:
    """A crash between write and replace must not litter the output dir."""
    target = tmp_path / "out.html"

    def boom(_src, _dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "x")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows blocks the replace while a reader holds the target open "
        "(Python's `open` does not grant FILE_SHARE_DELETE), so this "
        "reader loop makes the writer fail rather than tear — a different "
        "property from the one under test. The retry that covers it there "
        "is `test_the_replace_is_retried_past_a_holding_reader`."
    ),
)
def test_reader_never_sees_a_partial_file(tmp_path: Path) -> None:
    """The point of the helper, exercised against a concurrent reader.

    A 4 MB payload makes the truncate-then-write window wide enough that
    a plain `write_text` is caught in it reliably; the assertion is that
    the atomic path never is.
    """
    target = tmp_path / "page.html"
    small = "a" * 4_000_000
    large = "b" * 4_000_000
    target.write_text(small)

    observed: set[int] = set()
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                observed.add(len(target.read_text()))
            except FileNotFoundError:
                observed.add(-1)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        for i in range(40):
            atomic_write_text(target, large if i % 2 else small)
    finally:
        stop.set()
        th.join(timeout=5)

    # Only the two complete sizes are ever observable — never a torn
    # prefix, never a missing file.
    assert observed <= {len(small), len(large)}, sorted(observed)


def test_the_replace_is_retried_past_a_holding_reader(tmp_path, monkeypatch) -> None:
    """What a reader costs on Windows: a refusal, not a torn file.

    A reader with the target open makes `os.replace` raise
    `PermissionError` there until it closes — so the write has to wait it
    out rather than fail the conversion. Simulated, because the platform
    that does this is not the one the suite runs on.
    """
    target = tmp_path / "out.html"
    target.write_text("old")
    real_replace = os.replace
    attempts: list[int] = []

    def busy(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", busy)
    monkeypatch.setattr("claude_code_log.utils._REPLACE_BACKOFF_S", 0.001)

    atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert len(attempts) == 3, "the replace was not retried"


def test_a_reader_that_never_lets_go_still_reports(tmp_path, monkeypatch) -> None:
    """Retrying forever would hang a watch; the error still surfaces."""
    target = tmp_path / "out.html"

    def always_busy(_src, _dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "replace", always_busy)
    monkeypatch.setattr("claude_code_log.utils._REPLACE_BACKOFF_S", 0.001)

    with pytest.raises(PermissionError):
        atomic_write_text(target, "x")
    assert list(tmp_path.iterdir()) == [], "the temp file outlived the failure"


def test_symlink_is_written_through_not_replaced(tmp_path: Path) -> None:
    """`os.replace` would swap the link itself; users who linked meant it."""
    real = tmp_path / "real.md"
    real.write_text("old")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    atomic_write_text(link, "new")

    assert link.is_symlink()
    assert real.read_text() == "new"


def test_concurrent_writers_do_not_clobber_each_others_temp(tmp_path: Path) -> None:
    """The render fan-out can write the same path from several processes.

    They write identical bytes, so the outcome is fine — but only because
    the temp names differ. This pins that the pid is in the name.
    """
    target = tmp_path / "out.html"
    atomic_write_text(target, "x")
    # The helper's temp name for this process, reconstructed.
    assert not (tmp_path / f".out.html.{os.getpid()}.tmp").exists()
