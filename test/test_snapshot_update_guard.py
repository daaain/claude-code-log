"""The ``--snapshot-update`` × xdist-parallel guard (see test/conftest.py).

syrupy and pytest-xdist race on the shared ``.ambr`` files when snapshots are
updated with more than one worker; it has twice silently corrupted a snapshot
file. ``pyproject.toml`` defaults to ``-n auto``, so the unsafe combination is
the default unless the guard rejects it. These tests pin the decision (pure
function) and prove the end-to-end behaviour by executing real pytest
subprocesses.
"""

import os
import subprocess
import sys
from pathlib import Path

from test.conftest import _snapshot_update_xdist_conflict as _conflict

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Pure-function pins (fast; the mutation-check target)
# ---------------------------------------------------------------------------


def test_blocks_snapshot_update_under_parallel():
    msg = _conflict(is_worker=False, update_snapshots=True, workers=8)
    assert msg is not None
    assert "-n0" in msg  # points the user at the serial fix


def test_allows_snapshot_update_when_serial():
    # -n0 (0 workers, inline) and a single worker both write serially.
    assert _conflict(is_worker=False, update_snapshots=True, workers=0) is None
    assert _conflict(is_worker=False, update_snapshots=True, workers=1) is None


def test_allows_parallel_when_not_updating():
    # The CI path: many workers, no snapshot writes -> must be unaffected.
    assert _conflict(is_worker=False, update_snapshots=False, workers=8) is None


def test_skips_worker_process():
    # Only the controller gates; a worker must not independently error.
    assert _conflict(is_worker=True, update_snapshots=True, workers=8) is None


# ---------------------------------------------------------------------------
# End-to-end execution pins (real pytest subprocesses through the actual hook)
# ---------------------------------------------------------------------------

# The tmp project re-exports the real ``pytest_configure`` guard from
# test/conftest.py, so these exercise the wired hook, not a copy. The trivial
# test carries no snapshot assertions, so ``--snapshot-update`` writes nothing —
# no real ``.ambr`` is touched.
_CONFTEST = "from test.conftest import pytest_configure  # noqa: F401 (real hook)\n"
_TEST = "def test_ok():\n    assert True\n"


def _run_pytest(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_trivial.py").write_text(_TEST, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            *args,
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),  # rootdir = tmp_path, so repo pyproject addopts don't apply
    )


def test_execution_snapshot_update_parallel_errors(tmp_path):
    result = _run_pytest(tmp_path, "--snapshot-update", "-n", "auto")
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Refusing to run --snapshot-update" in output
    assert "-n0" in output


def test_execution_snapshot_update_serial_proceeds(tmp_path):
    result = _run_pytest(tmp_path, "--snapshot-update", "-n0")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output


def test_execution_parallel_without_update_proceeds(tmp_path):
    result = _run_pytest(tmp_path, "-n", "auto")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output
