#!/usr/bin/env python3
"""Benchmark the two render optimisations against real transcripts.

Measures the render memo caches (``CLAUDE_CODE_LOG_RENDER_CACHE_MB``) and
the intra-project render fan-out (``CLAUDE_CODE_LOG_RENDER_JOBS``), both
documented in ``dev-docs/application_model.md`` §§ 2.9-2.10.

Run it on your own machine because **core count changes the answer**: the
committed numbers come from a 4-core VM, the fan-out's payoff scales with
cores, and its overhead scales with worker count. This is the measurement
that decides whether the fan-out's default should stay off.

Two modes, and they answer different questions:

    # One project — the clearest read on the fan-out itself.
    uv run python scripts/bench_render.py ~/.claude/projects/<project>

    # The whole hierarchy — what a `claude-code-log` run actually costs.
    uv run python scripts/bench_render.py ~/.claude/projects --all-projects

The hierarchy mode matters because the two levels of parallelism trade
off: `process_projects_hierarchy` already runs stale projects
concurrently, and only hands leftover budget to the per-project fan-out.
So it reports two scenarios separately —

    full rebuild   every project stale. The project pool saturates the
                   machine on its own and the fan-out gets nothing; this
                   is a pure read on the memo caches.
    incremental    one project stale, which is what a daily run looks
                   like. Here the project pool has nothing to spread and
                   the fan-out is the only thing that can use the cores.

Everything is done on a **copy** — each run needs its own cache DB, and
the benchmark deletes generated HTML between configurations, which must
never happen to a real projects tree. Check you have the disk space; the
copy is as large as what you point it at.

A note on memory: every render worker holds the project's whole transcript
(~3x its bytes on disk), and under --all-projects that multiplies with the
project pool. The library caps workers against available memory
(``render_pool.memory_capped_workers``), and this script prints what that
cap allows before running, so a big archive degrades to serial rendering
rather than taking the machine into swap.

Every configuration's output is hashed, so a run doubles as an
equivalence check across far more real data than the test fixtures
cover: all configurations must produce byte-identical HTML.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_code_log.render_pool import (  # noqa: E402
    available_memory_bytes,
    memory_capped_workers,
)


# os.times() reports children_user / children_system as 0.0 on Windows —
# there is no child-process CPU accounting there.
CHILD_CPU_UNAVAILABLE = os.name == "nt"


@dataclass
class Result:
    label: str
    wall: float
    cpu: Optional[float]
    digest: str
    files: int


def _cli_command() -> list[str]:
    """Prefer the installed console script; fall back to the entry point.

    The fallback invokes ``cli.main`` directly rather than ``-m
    claude_code_log`` — the package has no ``__main__``, so the module form
    fails outright when the console script isn't on PATH.
    """
    console_script = shutil.which("claude-code-log")
    if console_script:
        return [console_script]
    return [sys.executable, "-c", "from claude_code_log.cli import main; main()"]


def _project_dirs(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if d.is_dir() and any(d.glob("*.jsonl")))


def _transcript_bytes(project: Path) -> int:
    return sum(f.stat().st_size for f in project.glob("*.jsonl"))


def _clear_outputs(target: Path, all_projects: bool) -> None:
    """Delete generated HTML so the next run has work to do.

    Deleting a project's HTML is how staleness is simulated: the cache
    reports a missing output as stale, which is exactly the state a real
    incremental run finds for a project whose transcripts have grown.
    """
    for path in target.glob("*.html"):
        path.unlink()
    if all_projects:
        for project in _project_dirs(target):
            for path in project.glob("*.html"):
                path.unlink()


def _digest_outputs(target: Path, all_projects: bool) -> tuple[str, int]:
    """Hash every generated file so configurations can be compared."""
    hasher = hashlib.sha256()
    count = 0
    roots = [target, *(_project_dirs(target) if all_projects else [])]
    for root in roots:
        for path in sorted(root.glob("*.html")):
            hasher.update(str(path.relative_to(target)).encode())
            hasher.update(path.read_bytes())
            count += 1
    return hasher.hexdigest(), count


def _run(
    target: Path,
    label: str,
    env_overrides: dict[str, str],
    *,
    all_projects: bool,
    stale: Optional[list[Path]] = None,
    jobs: Optional[int] = None,
) -> Result:
    """Time one conversion.

    ``stale`` restricts what gets invalidated beforehand — None means
    "everything", a list means only those projects (plus the index, which
    is always rewritten).
    """
    if stale is None:
        _clear_outputs(target, all_projects)
    else:
        for path in target.glob("*.html"):
            path.unlink()
        for project in stale:
            for path in project.glob("*.html"):
                path.unlink()

    env = dict(os.environ)
    # Start from a known state: an inherited value for either knob would
    # silently contaminate every row.
    env.pop("CLAUDE_CODE_LOG_RENDER_CACHE_MB", None)
    env.pop("CLAUDE_CODE_LOG_RENDER_JOBS", None)
    env.update(env_overrides)

    command = [*_cli_command(), str(target)]
    if all_projects:
        command.append("--all-projects")
    if jobs is not None:
        command += ["-j", str(jobs)]

    # CPU is measured across the whole process tree, which is the number
    # that exposes the fan-out's overhead: wall time can improve while
    # total CPU nearly doubles. Windows has no child-process accounting in
    # os.times() (the children_* fields are always zero there), so the
    # column is reported as unavailable rather than as a misleading 0.0.
    before = os.times()
    start = time.monotonic()
    proc = subprocess.run(command, env=env, capture_output=True, text=True)
    wall = time.monotonic() - start
    after = os.times()
    if proc.returncode != 0:
        sys.exit(f"{label}: conversion failed\n{proc.stderr[-2000:]}")
    cpu: Optional[float] = (after.children_user - before.children_user) + (
        after.children_system - before.children_system
    )
    if CHILD_CPU_UNAVAILABLE:
        cpu = None

    digest, files = _digest_outputs(target, all_projects)
    return Result(label, wall, cpu, digest, files)


def _report(title: str, results: list[Result], baseline_label: str) -> set[str]:
    baseline = next(r for r in results if r.label == baseline_label)
    print(f"\n{title}")
    print(f"{'configuration':<24} {'wall':>8} {'CPU':>8} {'vs ' + baseline_label:>16}")
    print("-" * 60)
    for result in results:
        speedup = baseline.wall / result.wall if result.wall else 0.0
        cpu = f"{result.cpu:7.1f}s" if result.cpu is not None else "    n/a"
        print(f"{result.label:<24} {result.wall:7.1f}s {cpu} {speedup:15.2f}x")
    fastest = min(results, key=lambda r: r.wall)
    print(
        f"fastest: {fastest.label} ({baseline.wall / fastest.wall:.2f}x over "
        f"{baseline_label})"
    )
    return {r.digest for r in results}


def _bench_single(target: Path, sweep: list[int]) -> set[str]:
    print("\nWarming the cache (parse + index, not measured)...", flush=True)
    warm_start = time.monotonic()
    _run(target, "warm", {}, all_projects=False)
    print(f"  cache built in {time.monotonic() - warm_start:.1f}s")

    print("\nRunning configurations...", flush=True)
    # Fan-out-less rows pin RENDER_JOBS=off explicitly: the fan-out is on
    # by default now, so an unset variable means "auto", and the serial
    # baselines would silently run the pool (which is exactly what
    # happened when the default flipped — every row measured the same
    # configuration and the table's labels lied).
    results = [
        _run(
            target,
            "neither",
            {
                "CLAUDE_CODE_LOG_RENDER_CACHE_MB": "0",
                "CLAUDE_CODE_LOG_RENDER_JOBS": "off",
            },
            all_projects=False,
        ),
        _run(
            target,
            "memo only",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "off"},
            all_projects=False,
        ),
        _run(
            target,
            "fan-out only",
            {
                "CLAUDE_CODE_LOG_RENDER_CACHE_MB": "0",
                "CLAUDE_CODE_LOG_RENDER_JOBS": "auto",
            },
            all_projects=False,
        ),
        _run(
            target,
            "both (auto)",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "auto"},
            all_projects=False,
        ),
    ]
    for workers in sweep:
        results.append(
            _run(
                target,
                f"both ({workers} workers)",
                {"CLAUDE_CODE_LOG_RENDER_JOBS": str(workers)},
                all_projects=False,
            )
        )
    return _report(
        f"Single project ({results[0].files} output files)", results, "memo only"
    )


def _bench_hierarchy(target: Path) -> set[str]:
    projects = _project_dirs(target)
    largest = max(projects, key=_transcript_bytes)

    print("\nWarming the cache (parse + index, not measured)...", flush=True)
    warm_start = time.monotonic()
    _run(target, "warm", {}, all_projects=True)
    print(f"  cache built in {time.monotonic() - warm_start:.1f}s")

    digests: set[str] = set()

    print(f"\nScenario 1/2: full rebuild, all {len(projects)} projects", flush=True)
    full = [
        _run(
            target,
            "neither",
            {
                "CLAUDE_CODE_LOG_RENDER_CACHE_MB": "0",
                "CLAUDE_CODE_LOG_RENDER_JOBS": "off",
            },
            all_projects=True,
        ),
        _run(
            target,
            "memo only",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "off"},
            all_projects=True,
        ),
        _run(
            target,
            "both (auto)",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "auto"},
            all_projects=True,
        ),
    ]
    digests |= _report(
        "Full rebuild — every project stale. The project pool already "
        "saturates\nthe machine, so the fan-out gets no leftover budget "
        "and this is a\nread on the memo caches alone.",
        full,
        "memo only",
    )

    size_mb = _transcript_bytes(largest) / 1e6
    print(
        f"\nScenario 2/2: incremental — only {largest.name} stale ({size_mb:.0f}MB)",
        flush=True,
    )
    incremental = [
        _run(
            target,
            "neither",
            {
                "CLAUDE_CODE_LOG_RENDER_CACHE_MB": "0",
                "CLAUDE_CODE_LOG_RENDER_JOBS": "off",
            },
            all_projects=True,
            stale=[largest],
        ),
        _run(
            target,
            "memo only",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "off"},
            all_projects=True,
            stale=[largest],
        ),
        _run(
            target,
            "both (auto)",
            {"CLAUDE_CODE_LOG_RENDER_JOBS": "auto"},
            all_projects=True,
            stale=[largest],
        ),
    ]
    # Not folded into the full-rebuild digest comparison: this scenario
    # regenerates a subset, so its file set legitimately differs from a
    # full rebuild's. The configurations *within* it rebuilt the same
    # subset though, so they must agree with each other.
    incremental_digests = _report(
        "Incremental — one project stale, the shape of a daily run. The\n"
        "project pool has nothing to spread, so the fan-out is the only\n"
        "thing that can use the other cores.",
        incremental,
        "memo only",
    )
    if len(incremental_digests) > 1:
        print(
            "\nMISMATCH — incremental configurations disagreed on the rendered bytes."
        )
        sys.exit(1)
    return digests


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "path",
        type=Path,
        help="A project directory, or the projects root with --all-projects",
    )
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Benchmark the whole hierarchy (full-rebuild + incremental scenarios)",
    )
    parser.add_argument(
        "--projects",
        type=int,
        default=8,
        help=(
            "With --all-projects, copy only the N largest projects "
            "(default: 8; 0 copies everything — check your disk space)"
        ),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Where to copy (default: a temp dir, removed afterwards)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="Keep the working copy afterwards"
    )
    parser.add_argument(
        "--workers",
        default="",
        help="Single-project mode: worker counts to sweep (default: 2,4,CPU count)",
    )
    args = parser.parse_args()

    source: Path = args.path.expanduser().resolve()
    if not source.is_dir():
        sys.exit(f"Not a directory: {source}")

    cpu_count = os.cpu_count() or 1
    if args.workers:
        sweep = [int(v) for v in args.workers.split(",") if v.strip()]
    else:
        sweep = sorted({2, 4, cpu_count})

    if args.all_projects:
        sources = _project_dirs(source)
        if not sources:
            sys.exit(f"No project directories with .jsonl files under {source}")
        if args.projects > 0:
            sources = sorted(sources, key=_transcript_bytes, reverse=True)[
                : args.projects
            ]
    else:
        if not list(source.glob("*.jsonl")):
            sys.exit(
                f"No .jsonl transcripts in {source} "
                f"(for a projects root, pass --all-projects)"
            )
        sources = [source]

    holder = Path(args.work_dir).expanduser() if args.work_dir else None
    temp_holder = None
    if holder is None:
        temp_holder = tempfile.mkdtemp(prefix="ccl-bench-")
        holder = Path(temp_holder)

    # Refuse a work dir that overlaps the source tree before touching the
    # filesystem: the copy loop rmtree's <holder>/<project name> and
    # _clear_outputs sweeps the holder, so an overlapping --work-dir would
    # delete real transcripts rather than scratch copies.
    holder_resolved = holder.resolve()
    for project in sources:
        project_resolved = project.resolve()
        if (
            holder_resolved == project_resolved
            or holder_resolved in project_resolved.parents
            or project_resolved in holder_resolved.parents
        ):
            sys.exit(
                f"--work-dir {holder} overlaps source project {project}; "
                "pick a directory outside the source tree"
            )

    # And it must be new or empty: the copy loop creates <holder>/<project
    # name> and the output sweep clears the holder, so a directory with
    # existing contents — even an unrelated one that merely shares a
    # project's name — would have files this script never created deleted
    # or overwritten. That includes a previous --keep run's copy: delete
    # it yourself to reuse the path.
    if holder.exists():
        if not holder.is_dir():
            sys.exit(f"--work-dir {holder} is not a directory")
        if any(holder.iterdir()):
            sys.exit(f"--work-dir {holder} is not empty; pass a new or empty directory")

    holder.mkdir(parents=True, exist_ok=True)

    try:
        total_mb = sum(_transcript_bytes(p) for p in sources) / 1e6
        print(f"Source:  {source}")
        print(f"Copy to: {holder}")
        print(f"Data:    {len(sources)} project(s), {total_mb:.0f}MB of transcripts")
        print(f"Cores:   {cpu_count}")

        # Show what the memory cap will actually allow, so a run that
        # reports "no speedup" isn't quietly a run that never fanned out.
        # Each worker holds a whole transcript, so this is often the real
        # limit rather than core count.
        largest = max(_transcript_bytes(p) for p in sources)
        available = available_memory_bytes()
        available_note = (
            f"{available / 1e9:.1f}GB reclaimable"
            if available
            else "UNKNOWN (capped at 2 workers)"
        )
        print(f"Memory:  {available_note}; largest project {largest / 1e6:.0f}MB")

        if args.all_projects:
            # The two scenarios differ in how many conversions are resident
            # at once, and therefore in how much is left for render workers.
            concurrent = min(cpu_count, len(sources))
            full = memory_capped_workers(
                cpu_count, largest, concurrent_projects=concurrent
            )
            incremental = memory_capped_workers(cpu_count, largest)
            print(
                f"         full rebuild: {concurrent} concurrent projects "
                f"-> at most {full} render worker(s) each"
            )
            print(
                f"         incremental:  1 project -> at most {incremental} "
                f"render worker(s) of the {cpu_count} requested"
            )
            allowed = max(full, incremental)
        else:
            allowed = memory_capped_workers(cpu_count, largest)
            print(
                f"         at most {allowed} render worker(s) of the "
                f"{cpu_count} requested"
            )
        if allowed <= 1:
            print(
                "         (memory-capped to serial — the fan-out rows below "
                "will match\n          'memo only'. Use a smaller project or "
                "free up RAM.)"
            )

        for project in sources:
            # Keep each project's own directory name: the rendered title is
            # derived from it, so renaming would change output for no reason.
            # The holder was verified new-or-empty above, so the destination
            # cannot pre-exist — copytree fails loudly rather than this loop
            # deleting anything it didn't create.
            shutil.copytree(project, holder / project.name)
        # Don't inherit generated output or a cache from the source tree.
        _clear_outputs(holder, all_projects=True)
        for stale_db in holder.glob("claude-code-log-cache.db*"):
            stale_db.unlink()

        if args.all_projects:
            digests = _bench_hierarchy(holder)
        else:
            digests = _bench_single(holder / sources[0].name, sweep)

        print()
        if len(digests) == 1:
            print("Every configuration produced byte-identical output.")
        else:
            print("MISMATCH — configurations disagreed on the rendered bytes.")
            sys.exit(1)
        print(
            "\nIf a fan-out row beats 'memo only' comfortably — especially in the\n"
            "incremental scenario — that is the argument for flipping the default\n"
            "in render_pool.resolve_render_jobs."
        )
    finally:
        if temp_holder and not args.keep:
            shutil.rmtree(temp_holder, ignore_errors=True)
        elif args.keep:
            print(f"\nWorking copy kept at {holder}")


if __name__ == "__main__":
    main()
