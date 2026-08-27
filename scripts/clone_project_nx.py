#!/usr/bin/env python3
"""Clone a real project's transcripts N times with rewritten identifiers.

For benchmarking paths that only engage at scale (the render fan-out's
25k-message floor, pagination, the streaming pass) on a machine whose
real archives are too small: point it at a project, get one N times the
size whose every copy renders independently.

    uv run python scripts/clone_project_nx.py <source-project> <dest-dir> 4

Each copy c applies a bijective hex-digit rotation (+c mod 16) to every
UUID in each file (uuid, parentUuid, sessionId, leafUuid, and the
filenames themselves) so copies never collide, and suffixes every
requestId so requestId-based dedup cannot collapse messages across
copies. Copy 0 is byte-identical to the source. Timestamps are left
alone, so all copies share the source's time range.
"""

import re
import shutil
import sys
from pathlib import Path
from typing import Callable

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQ_RE = re.compile(r"req_[A-Za-z0-9]+")

HEX = "0123456789abcdef"

Translator = Callable[["re.Match[str]"], str]


def make_translators(c: int) -> tuple[Translator, Translator]:
    rot = {h: HEX[(i + c) % 16] for i, h in enumerate(HEX)}

    def xlate_uuid(m: "re.Match[str]") -> str:
        return "".join(rot.get(ch, ch) for ch in m.group(0))

    def xlate_req(m: "re.Match[str]") -> str:
        return m.group(0) + f"cp{c}"

    return xlate_uuid, xlate_req


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <source-project> <dest-dir> <copies>")
    src = Path(sys.argv[1]).expanduser().resolve()
    dst = Path(sys.argv[2]).expanduser().resolve()
    copies = int(sys.argv[3])
    files = sorted(src.glob("*.jsonl"))
    if not files:
        sys.exit(f"No .jsonl transcripts in {src}")
    if dst == src or src in dst.parents:
        sys.exit("destination must be outside the source project")
    dst.mkdir(parents=True, exist_ok=True)

    for c in range(copies):
        xlate_uuid, xlate_req = make_translators(c)
        for f in files:
            out = dst / UUID_RE.sub(xlate_uuid, f.name)
            if c == 0:
                shutil.copyfile(f, out)
                continue
            text = f.read_text(encoding="utf-8")
            text = UUID_RE.sub(xlate_uuid, text)
            text = REQ_RE.sub(xlate_req, text)
            out.write_text(text, encoding="utf-8")

    total = sum(f.stat().st_size for f in dst.glob("*.jsonl"))
    print(f"{len(list(dst.glob('*.jsonl')))} files, {total / 1e6:.0f}MB")


if __name__ == "__main__":
    main()
