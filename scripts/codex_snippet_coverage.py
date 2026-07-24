#!/usr/bin/env python3
"""Dev-only coverage probe for the Codex QuickJS snippet analyzer.

Runs :func:`claude_code_log.providers.codex_quickjs.analyze_javascript_tools`
over a local corpus of ``exec`` snippets and reports how many decode into a
tool batch versus fail closed to the raw-script fallback. Used during the
tree-sitter → QuickJS migration to confirm coverage does not regress against
the tree-sitter baseline (which decoded ~91% of the 2,265-snippet evidence
corpus; QuickJS execution reached ~99.96%).

This script reads *local* rollout files only and prints aggregate counts plus
truncated fail-closed examples for inspection. It commits nothing private —
point it at your own rollouts:

    # A directory / glob of rollout JSONLs (custom_tool_call exec events):
    python scripts/codex_snippet_coverage.py ~/.codex/sessions

    # Compare against the tree-sitter analyzer while it still exists:
    python scripts/codex_snippet_coverage.py ~/.codex/sessions --compare

    # No path given → self-check on the synthetic spec fixtures below.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from claude_code_log.providers.codex_quickjs import analyze_javascript_tools

# A handful of synthetic snippets so the script is runnable with no corpus.
# These mirror shapes the analyzer is expected to decode; they contain no
# private data. Not a substitute for a real corpus run.
_SELFCHECK_SNIPPETS: list[str] = [
    'const r = await tools.exec_command({cmd: "git status"}); text(r.output);',
    'const p = "*** Begin Patch\\n*** End Patch"; text(await tools.apply_patch(p));',
    "const results = await Promise.all(["
    'tools.exec_command({cmd: "one"}), tools.exec_command({cmd: "two"})]); '
    "results.forEach((r, i) => { text(`RESULT_${i + 1}`); text(r.output); });",
    "for (const id of [1, 2, 3]) { "
    'const r = await tools.exec_command({cmd: "echo", id}); text(r.output); }',
    "while (true) {}",  # expected fail-closed (time limit)
]


def _iter_exec_snippets(paths: list[Path]) -> Iterator[str]:
    """Yield ``exec`` snippet sources from rollout JSONL files under paths.

    Each argument may be a file, a directory (searched recursively for
    ``*.jsonl``), or a shell-style glob pattern (``**`` supported). A pattern or
    path that resolves to nothing is reported on stderr rather than silently
    skipped, so ``'sessions/**/*.jsonl'`` matching zero files is not mistaken
    for a real 0% run.
    """
    files: list[Path] = []
    for path in paths:
        pattern = str(path)
        if glob.has_magic(pattern):
            expanded = [Path(match) for match in glob.glob(pattern, recursive=True)]
            if not expanded:
                print(f"warning: glob {pattern!r} matched nothing", file=sys.stderr)
        else:
            expanded = [path]
        for candidate in expanded:
            if candidate.is_dir():
                files.extend(sorted(candidate.rglob("*.jsonl")))
            elif candidate.is_file():
                files.append(candidate)
            else:
                print(
                    f"warning: {str(candidate)!r} is not a file or directory",
                    file=sys.stderr,
                )
    for file in files:
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record = cast("dict[str, Any]", record)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = record
            payload = cast("dict[str, Any]", payload)
            if (
                payload.get("type") != "custom_tool_call"
                or payload.get("name") != "exec"
            ):
                continue
            source = payload.get("input")
            if isinstance(source, str) and source.strip():
                yield source


def _dedupe(snippets: Iterator[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for source in snippets:
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        out.append(source)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path, help="rollout JSONL files or directories"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="also run the tree-sitter analyzer (if still importable) and "
        "report snippets one decodes but the other does not",
    )
    parser.add_argument(
        "--examples", type=int, default=8, help="fail-closed examples to print"
    )
    args = parser.parse_args(argv)

    if args.paths:
        snippets = _dedupe(_iter_exec_snippets(args.paths))
        origin = f"{len(snippets)} unique exec snippets from {len(args.paths)} path(s)"
    else:
        snippets = _SELFCHECK_SNIPPETS
        origin = f"{len(snippets)} synthetic self-check snippets (no corpus given)"

    if not snippets:
        print("No exec snippets found.", file=sys.stderr)
        return 1

    legacy = None
    if args.compare:
        try:
            from claude_code_log.providers.codex_javascript import (  # type: ignore
                analyze_javascript_tools as legacy,
            )
        except Exception as exc:  # noqa: BLE001 - tree-sitter analyzer may be gone
            print(f"--compare unavailable ({exc}); running QuickJS only.")

    decoded = 0
    fail_closed: list[str] = []
    only_new: list[str] = []
    only_old: list[str] = []
    for source in snippets:
        new_ok = analyze_javascript_tools(source) is not None
        if new_ok:
            decoded += 1
        elif len(fail_closed) < args.examples:
            fail_closed.append(source)
        if legacy is not None:
            old_ok = legacy(source) is not None
            if new_ok and not old_ok:
                only_new.append(source)
            elif old_ok and not new_ok:
                only_old.append(source)

    total = len(snippets)
    print(f"Corpus: {origin}")
    print(f"Decoded : {decoded}/{total} ({decoded / total:.2%})")
    print(f"Fallback: {total - decoded}/{total} ({(total - decoded) / total:.2%})")
    if legacy is not None:
        print(f"QuickJS-only gains vs tree-sitter: {len(only_new)}")
        print(f"tree-sitter-only (potential regressions): {len(only_old)}")
        for source in only_old[: args.examples]:
            print("  REGRESSION:", source[:120].replace("\n", " "))
    for source in fail_closed:
        print("  fail-closed:", source[:120].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
