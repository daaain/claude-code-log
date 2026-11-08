"""The refresh spool file: its name, and the sweep of orphaned ones."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_code_log.refresh_spool import (
    RefreshSpool,
    is_spool_path,
    spool_path,
    sweep_orphan_spools,
)

# A pid no process can have: past Linux's default pid_max (4194304), and
# past any Windows pid in practice (they are DWORDs but allocated low).
DEAD_PID = 4_200_000


def test_a_spool_is_named_after_the_cache_and_this_process(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    path = spool_path(db)
    assert path.parent == tmp_path
    assert path.name.startswith(f"cache.db.spool-{os.getpid()}-")
    assert is_spool_path(path, db)
    assert not is_spool_path(db, db)
    # Each call names a new file.
    assert spool_path(db) != path


def test_the_sweep_removes_a_dead_processs_spool_and_keeps_a_live_ones(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cache.db"
    dead = tmp_path / f"cache.db.spool-{DEAD_PID}-1"
    dead.write_bytes(b"left behind")
    alive = RefreshSpool(db)  # this process's own, mid refresh
    other = tmp_path / "cache.db.spool-notapid-1"
    other.write_bytes(b"not a spool name")
    unrelated = tmp_path / "other.db.spool-4200000-1"
    unrelated.write_bytes(b"another cache's")

    removed = sweep_orphan_spools(db)

    assert removed == [dead]
    assert not dead.exists()
    assert alive.path.exists() and other.exists() and unrelated.exists()
    alive.close()
    alive.unlink()


def test_a_pass_sweeps_orphaned_spools_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_log.converter import process_projects_hierarchy

    projects_dir = tmp_path / "projects"
    project = projects_dir / "-proj"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text("", encoding="utf-8")
    db = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db))
    dead = tmp_path / f"cache.db.spool-{DEAD_PID}-7"
    dead.write_bytes(b"left behind")

    process_projects_hierarchy(
        projects_dir, output_format="md", write_combined=False, jobs=1
    )

    assert not dead.exists()
