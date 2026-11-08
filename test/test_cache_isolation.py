"""Every test gets a cache database of its own (test/conftest.py).

Without that, a project made directly in the system temp dir puts its cache
at `<tmp>/claude-code-log-cache.db`, which every such test, every run and
every checkout on the machine shares.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from claude_code_log.cache import CacheManager

SHARED = Path(tempfile.gettempdir()) / "claude-code-log-cache.db"


def test_a_project_in_the_temp_dir_gets_a_cache_of_its_own(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with tempfile.TemporaryDirectory() as project:
        cache = CacheManager(Path(project), "1.0.0-test")

    assert cache.db_path != SHARED
    assert tmp_path_factory.getbasetemp() in cache.db_path.parents


def test_a_subprocess_inherits_the_test_cache() -> None:
    """CLI and render-worker subprocesses resolve the same per-test path."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path;"
            "from claude_code_log.cache import get_cache_db_path;"
            "print(get_cache_db_path(Path('.')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert out == os.environ["CLAUDE_CODE_LOG_CACHE_PATH"]
    assert Path(out) != SHARED


@pytest.mark.default_cache_path
def test_the_marker_leaves_the_default_location(tmp_path: Path) -> None:
    assert "CLAUDE_CODE_LOG_CACHE_PATH" not in os.environ
    project = tmp_path / "project"
    project.mkdir()

    assert CacheManager(project, "1.0.0-test").db_path == (
        tmp_path / "claude-code-log-cache.db"
    )
