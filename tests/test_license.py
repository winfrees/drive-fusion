"""The license and the terms of use exist, agree with the package metadata, and are pointed to."""

from pathlib import Path

from drivefusion.core import disclaimer

ROOT = Path(__file__).resolve().parent.parent


def test_the_license_is_mit_as_the_package_says():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert "Copyright (c) 2026 The Drive Fusion authors" in license_text
    assert 'license = { text = "MIT" }' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_terms_cover_what_they_promise():
    terms = (ROOT / "DISCLAIMER.md").read_text(encoding="utf-8")
    for heading in ("Use at your own risk", "No warranty", "No particular purpose",
                    "No guarantee of results", "Not for regulated data",
                    "Limitation of liability", "Indemnification"):
        assert f". {heading}\n" in terms, heading


def test_the_first_run_disclaimer_points_to_the_terms():
    assert "DISCLAIMER.md" in disclaimer.TEXT
    assert "at your own risk" in disclaimer.TEXT
    assert "without warranty" in disclaimer.TEXT


def test_the_build_ships_both_files():
    build = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    assert "Copy-Item LICENSE" in build and "Copy-Item DISCLAIMER.md" in build
