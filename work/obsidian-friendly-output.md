# Obsidian-friendly output (issue #151) — open follow-ups

## Status: Main implementation shipped (PR #155); follow-ups below

The core feature — `--output <dir>` + `--expand-paths` +
`--filter-path` + per-project path projection — landed in PR #155
(`8de5b89`). Per-message timestamps for Markdown landed in PR #165
(`68e5bfb`). `--combined yes/no/only` and the bullet-list Markdown
index under `--expand-paths` also shipped.

This file retains the open follow-up items surfaced during the #151
implementation and shakedown. See PR #155 + #165 for the as-built
behaviour.

## Open follow-ups

### Cache-freshness checks resolve against `project_path` (source), not the output destination

`cache.is_html_stale(html_path, ...)` and `cache.is_page_stale(...)` both compute their `actual_file` check as `self.project_path / html_path` — the **source** project dir under `~/.claude/projects/`, not the actual output destination (`dest_dir`). With the legacy in-place behaviour the two are identical, so the check works as intended. With `--output` projecting to a different tree, the source path never has a `combined_transcripts.html`, so `is_html_stale` returns "file_missing" / "stale" on every run.

**Practical implication** — both runs of the same source against two different `--output` dirs both produce correct output (the `not output_path.exists()` term in `process_projects_hierarchy`'s `needs_work` and the per-session-file existence checks force regeneration). But every `--output` switch always re-renders, even when the destination is already up-to-date. JSONL parsing is still cache-hit ("X sessions" instead of "X files updated"), only rendering re-runs.

```
Run 1 (--output /tmp/A):  4.4s  (8 projects updated)
Run 2 (--output /tmp/B):  2.3s  (cache-hit on JSONL parse,
                                  but rendering re-ran)
Run 3 (--output /tmp/A):  ~2.3s (same — A's existing files
                                  are not consulted)
```

**Future optimisation** — make the html-cache row's freshness check destination-aware (e.g. record the absolute destination path when writing, compare against it on next run). Bounded value: only matters when users alternate between several `--output` destinations on the same source. Not worth the complexity until someone hits the slowdown in practice.

### Archived projects with `--output`

Index links point to projected paths whose files won't exist until the user re-renders. Two plausible mitigations: exclude archived projects from the index in `--output` mode, or always link to the original on-disk location regardless of `--output` / `--expand-paths`. (Surfaced by monk; left for follow-up.)

### Absolute `--filter-path` without `--expand-paths` silently excludes everything

Symmetric inverse of the relative-`--filter-path`-with-`--expand-paths` footgun (which IS now rejected at click parse time). Reproduced empirically:

```
$ uv run claude-code-log -o .examples/.../ccl --all-projects \
      --filter-path /home/cboos/Workspace/github/daain \
      --detail low --compact --format md
Processed 665 projects in 1.3s
  Index regenerated
$ ls .examples/.../ccl
index.md   # ← no per-project output
```

Without `--expand-paths` the filter matches against the encoded flat dir name (`-home-cboos-...`). An absolute path starting with `/` matches no encoded name, so all 665 projects filter out. No error, no warning — only the index lands.

Two fixes to consider:

- **(A) Reject** at click parse time when `--filter-path.startswith("/")` and `--expand-paths` is unset. Symmetric with the relative-filter rejection that already exists.
- **(B) Auto-imply `--expand-paths`** when `--filter-path` is absolute. Friendlier; encoded-form filtering is the niche case.

Lean toward (B).

### `--filter-path` should imply `--all-projects`

Filtering only makes sense over a set of projects — without `--all-projects` there's nothing for `--filter-path` to filter. Currently warned-about-and-ignored; auto-imply would be friendlier.

**Asymmetry note** (worth recording): `--expand-paths` *cannot* safely imply `--all-projects` because the flag has independent meaning in single-session / single-project mode (next item — project one artefact under `<output>/<real-path>/<filename>`). Implying `--all-projects` from `--expand-paths` would silently switch from "expand this one input" to "scan ~/.claude/projects/", which is a much bigger surprise than `--filter-path` could ever be. So the auto-imply is `--filter-path` only; `--expand-paths` keeps the current behaviour matrix.

### `--expand-paths` for single-session / single-project mode

Today `--expand-paths` is wired only through `process_projects_hierarchy`. Reasonable extension: when a single-session or single-project export is requested with `--output <dir>` and `--expand-paths`, project that one artefact into `<output>/<real-path>/<filename>` using the same path-projection helper. Same convention, same matrix shape — just narrower scope.

### `--dry-run` mode

Show what would be generated (projected destinations, filter selections) without actually rendering or writing. Useful for sanity-checking a flag combination — especially with the path-projection logic where the destination depends on cache state and JSONL peek results. Pairs naturally with `--filter-path` + `--expand-paths` exploration.

Implementation sketch: a top-level CLI flag that, when set, prints the per-project decision (`source -> dest` or `<source>: filter excluded`) and exits before any file I/O. Cheap to implement on top of `project_destination()` since the helper is already pure.

### Other open items (mention for completeness)

- **Obsidian-specific frontmatter** — YAML at top of each `.md` for tags / links. Could be a follow-up `--obsidian-frontmatter` flag.
- **Wikilink generation** (`[[…]]`) for cross-references between sessions. Same shape — follow-up.
- **`_peek_jsonl_for_cwd` debug logging** — current shape is silent on tier-2→tier-3 fallthroughs; a `logger.debug(...)` would help when someone is debugging an unexpected naive-tier hit. Zero-noise default kept.
- **Symlink-based projection** (write once, link from many places). The current write-then-copy model is fine for Obsidian; symlinks would complicate cache invalidation. Probably never.
