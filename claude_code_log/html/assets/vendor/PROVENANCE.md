# Vendored third-party assets

These files are vendored (checked in) rather than loaded from a CDN so the
generated HTML works fully offline and does not leak a request to a third
party when a user opens a transcript rendered from their private logs
(issue #278).

## vis-timeline

- **Package:** `vis-timeline` (vis-timeline and vis-graph2d)
- **Version:** 8.5.1
- **Homepage:** https://visjs.github.io/vis-timeline/
- **License:** MIT / Apache-2.0 (dual) — see the upstream project.
- **Source URLs (unpkg, pinned):**
  - `https://unpkg.com/vis-timeline@8.5.1/standalone/umd/vis-timeline-graph2d.min.js`
  - `https://unpkg.com/vis-timeline@8.5.1/styles/vis-timeline-graph2d.min.css`

### SHA-256 checksums

```
484c8f44cb2be213922ca9537022e9a87eb85d5981a2064e369da91bd2150298  vis-timeline-graph2d.min.js
67273f3f1bfc5fee585494e8b8818d7cad98c229737488bc56f140547badc1f5  vis-timeline-graph2d.min.css
```

### Re-verifying / upgrading

```bash
curl -sL https://unpkg.com/vis-timeline@8.5.1/standalone/umd/vis-timeline-graph2d.min.js \
  | shasum -a 256   # must match the .js checksum above
curl -sL https://unpkg.com/vis-timeline@8.5.1/styles/vis-timeline-graph2d.min.css \
  | shasum -a 256   # must match the .css checksum above
```

To upgrade, bump the version in the URLs, re-download both files into this
directory, and update the version + checksums above.
