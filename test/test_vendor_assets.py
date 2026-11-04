"""The timeline's vendored vis-timeline sidecar (issue #278).

Generated pages used to fetch vis-timeline from unpkg, unpinned and without
integrity, the only network request a rendered transcript made. The library
now ships in the package and is copied into an ``assets/`` folder beside every
page written to disk, and the page loads it from there.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest

from claude_code_log.converter import convert_jsonl_to, load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.html.utils import (
    VENDOR_ASSETS_DIRNAME,
    VIS_TIMELINE_CSS,
    VIS_TIMELINE_JS,
    VIS_TIMELINE_LICENSE,
    VIS_TIMELINE_VERSION,
    ensure_vendor_assets,
)

VENDOR_DIR = (
    Path(__file__).parent.parent / "claude_code_log" / "html" / "assets" / "vendor"
)
FIXTURE = Path(__file__).parent / "test_data" / "representative_messages.jsonl"
EDGE_CASES = Path(__file__).parent / "test_data" / "edge_cases.jsonl"
JS_SRC = f"{VENDOR_ASSETS_DIRNAME}/{VIS_TIMELINE_JS}"
CSS_SRC = f"{VENDOR_ASSETS_DIRNAME}/{VIS_TIMELINE_CSS}"


class TestVendoredFiles:
    def test_files_match_provenance_checksums(self):
        """The committed bundle is the one PROVENANCE.md says it is."""
        provenance = (VENDOR_DIR / "PROVENANCE.md").read_text(encoding="utf-8")
        recorded = dict(
            (name, digest)
            for digest, name in re.findall(r"^([0-9a-f]{64})  (\S+)$", provenance, re.M)
        )
        assert set(recorded) == {VIS_TIMELINE_JS, VIS_TIMELINE_CSS}
        for name, digest in recorded.items():
            actual = hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest()
            assert actual == digest, name

    def test_license_travels_with_the_bundle(self):
        """MIT and Apache-2.0 both require the notice to ship with copies."""
        text = (VENDOR_DIR / VIS_TIMELINE_LICENSE).read_text(encoding="utf-8")
        assert "Permission is hereby granted, free of charge" in text  # MIT
        assert "Apache License" in text and "Version 2.0, January 2004" in text
        assert "vis.js contributors" in text

    def test_file_names_carry_the_version(self):
        assert VIS_TIMELINE_VERSION in VIS_TIMELINE_JS
        assert VIS_TIMELINE_VERSION in VIS_TIMELINE_CSS
        assert f"@version {VIS_TIMELINE_VERSION}" in (
            VENDOR_DIR / VIS_TIMELINE_JS
        ).read_text(encoding="utf-8")


class TestEnsureVendorAssets:
    def test_copies_the_bundle(self, tmp_path: Path):
        assert ensure_vendor_assets(tmp_path)
        names = [VIS_TIMELINE_JS, VIS_TIMELINE_CSS, VIS_TIMELINE_LICENSE]
        for name in names:
            copied = tmp_path / VENDOR_ASSETS_DIRNAME / name
            assert copied.read_bytes() == (VENDOR_DIR / name).read_bytes()
        # No temp files left behind.
        assert sorted(p.name for p in (tmp_path / VENDOR_ASSETS_DIRNAME).iterdir()) == (
            sorted(names)
        )

    def test_present_files_are_not_rewritten(self, tmp_path: Path):
        ensure_vendor_assets(tmp_path)
        js = tmp_path / VENDOR_ASSETS_DIRNAME / VIS_TIMELINE_JS
        before = js.stat().st_mtime_ns
        assert ensure_vendor_assets(tmp_path)
        assert js.stat().st_mtime_ns == before

    def test_truncated_file_is_replaced(self, tmp_path: Path):
        """A partial copy (an interrupted run) doesn't count as present."""
        ensure_vendor_assets(tmp_path)
        js = tmp_path / VENDOR_ASSETS_DIRNAME / VIS_TIMELINE_JS
        js.write_bytes(js.read_bytes()[:100])
        ensure_vendor_assets(tmp_path)
        assert js.read_bytes() == (VENDOR_DIR / VIS_TIMELINE_JS).read_bytes()

    def test_unwritable_output_degrades(self, tmp_path: Path):
        """A file where the folder should be: report it, don't raise."""
        (tmp_path / VENDOR_ASSETS_DIRNAME).write_text("not a directory")
        assert ensure_vendor_assets(tmp_path) is False


class TestRenderedPage:
    def test_page_with_output_dir_points_at_the_sidecar(self, tmp_path: Path):
        html = generate_html(load_transcript(FIXTURE), "T", output_dir=tmp_path)
        assert f'data-vis-js="{JS_SRC}"' in html
        assert f'data-vis-css="{CSS_SRC}"' in html
        assert (tmp_path / JS_SRC).is_file()
        assert (tmp_path / CSS_SRC).is_file()

    def test_page_without_output_dir_has_no_sources(self):
        html = generate_html(load_transcript(FIXTURE), "T")
        assert 'data-vis-js=""' in html
        assert 'data-vis-css=""' in html

    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_page_makes_no_cdn_request(self, tmp_path: Path, theme: str):
        html = generate_html(
            load_transcript(FIXTURE), "T", theme=theme, output_dir=tmp_path
        )
        assert "unpkg" not in html
        assert "https://cdn" not in html

    def test_unwritable_output_leaves_sources_empty(self, tmp_path: Path):
        """The page must not point at files that were never written."""
        (tmp_path / VENDOR_ASSETS_DIRNAME).write_text("not a directory")
        html = generate_html(load_transcript(FIXTURE), "T", output_dir=tmp_path)
        assert 'data-vis-js=""' in html


class TestProjectConversion:
    # page_size=1 forces the paginated combined path, which the original PR
    # missed (it rendered pages without an output directory).
    @pytest.mark.parametrize("page_size", [2000, 1])
    def test_every_page_has_its_sidecar(self, tmp_path: Path, page_size: int):
        project = tmp_path / "project"
        project.mkdir()
        # Two sessions, so a tiny page size gives several combined pages.
        shutil.copy(FIXTURE, project / FIXTURE.name)
        shutil.copy(EDGE_CASES, project / EDGE_CASES.name)

        convert_jsonl_to(
            "html",
            project,
            generate_individual_sessions=True,
            silent=True,
            page_size=page_size,
        )

        pages = sorted(project.glob("*.html"))
        assert any(p.name.startswith("session-") for p in pages)
        if page_size == 1:
            assert len([p for p in pages if p.name.startswith("combined")]) > 1
        for page in pages:
            assert f'data-vis-js="{JS_SRC}"' in page.read_text(encoding="utf-8"), (
                page.name
            )
        assert (project / JS_SRC).is_file()
        assert (project / CSS_SRC).is_file()

    def test_single_file_output_gets_a_sibling_sidecar(self, tmp_path: Path):
        out = tmp_path / "elsewhere" / "out.html"
        out.parent.mkdir()
        convert_jsonl_to("html", FIXTURE, out, silent=True)
        assert f'data-vis-js="{JS_SRC}"' in out.read_text(encoding="utf-8")
        assert (out.parent / JS_SRC).is_file()
