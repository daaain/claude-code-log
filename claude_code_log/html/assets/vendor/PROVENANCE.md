# Vendored third-party assets

These files are vendored (checked in) rather than loaded from a CDN so the
generated HTML works fully offline and does not leak a request to a third
party when a user opens a transcript rendered from their private logs
(issue #278).

The HTML renderer copies them into an `assets/` folder beside the pages it
writes (`ensure_vendor_assets` in `claude_code_log/html/utils.py`), and the
timeline loads them from there the first time it is opened.

## vis-timeline

- **Package:** `vis-timeline` (vis-timeline and vis-graph2d)
- **Version:** 8.5.1
- **Homepage:** https://visjs.github.io/vis-timeline/
- **License:** MIT / Apache-2.0 (dual). Upstream's `LICENSE.md`,
  `LICENSE.MIT.txt` and `LICENSE.Apache-2.0.txt` (tag `v8.5.1`) are bundled,
  concatenated, as `vis-timeline-8.5.1.LICENSE.txt`, which is copied into
  every `assets/` folder with the library.
- **Source URLs (unpkg, pinned):**
  - `https://unpkg.com/vis-timeline@8.5.1/standalone/umd/vis-timeline-graph2d.min.js`
  - `https://unpkg.com/vis-timeline@8.5.1/styles/vis-timeline-graph2d.min.css`

The files are saved under versioned names, so an upgrade adds new files to an
output folder instead of overwriting the old ones, and a page rendered by an
older release keeps loading the build it was rendered against.

### SHA-256 checksums

```
484c8f44cb2be213922ca9537022e9a87eb85d5981a2064e369da91bd2150298  vis-timeline-graph2d-8.5.1.min.js
67273f3f1bfc5fee585494e8b8818d7cad98c229737488bc56f140547badc1f5  vis-timeline-graph2d-8.5.1.min.css
```

`test/test_vendor_assets.py` checks the committed files against these, and
`.gitattributes` keeps Git for Windows from converting their line endings.

### Re-verifying / upgrading

```bash
curl -sL https://unpkg.com/vis-timeline@8.5.1/standalone/umd/vis-timeline-graph2d.min.js \
  | shasum -a 256   # must match the .js checksum above
curl -sL https://unpkg.com/vis-timeline@8.5.1/styles/vis-timeline-graph2d.min.css \
  | shasum -a 256   # must match the .css checksum above
```

To upgrade: download both files for the new version into this directory under
the new versioned names, rebuild the license file from the new tag, delete the old ones, bump `VIS_TIMELINE_VERSION` in
`claude_code_log/html/utils.py`, and update the version, URLs and checksums
above.
