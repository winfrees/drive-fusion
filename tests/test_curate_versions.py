"""Version sets (ROADMAP M6.5)."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.curate import versions
from drivefusion.core.store import curation
from drivefusion.core.store.catalog import Catalog
from tests.conftest import _write_dated


@pytest.fixture
def drafts(tmp_path: Path):
    root = tmp_path / "docs"
    day = dt.datetime(2022, 1, 1)
    for i, size in enumerate((20_000, 21_000, 21_500), start=1):            # numbered
        _write_dated(root / "Thesis" / f"thesis_v{i}.docx", size, day + dt.timedelta(days=i))
    _write_dated(root / "Grant" / "aims.docx", 9_000, day + dt.timedelta(days=30))  # newest
    _write_dated(root / "Grant" / "aims final.docx", 9_400, day)
    _write_dated(root / "Grant" / "aims (1).docx", 30_000, day + dt.timedelta(days=2))
    _write_dated(root / "Photos" / "IMG_0001.jpg", 5_000, day)            # not versions
    _write_dated(root / "Photos" / "IMG_0002.jpg", 5_100, day)
    _write_dated(root / "Other" / "thesis_v4.docx", 22_000, day)          # another folder
    shutil.copy2(root / "Thesis" / "thesis_v1.docx", root / "Thesis" / "thesis_v1 copy.docx")
    catalog = tmp_path / "catalog.db"
    for args in (["--accept-disclaimer", "scope", "add", str(root)], ["scan"], ["hash"]):
        assert main(["--catalog", str(catalog), *args]) == 0
    with Catalog(catalog) as cat:
        yield cat, catalog


def _sets(catalog) -> dict[str, dict]:
    out = {}
    for found in versions.find(catalog):
        names = {}
        for m in found["members"]:
            name = catalog.conn.execute("SELECT name FROM file WHERE id = ?",
                                        (m.file_id,)).fetchone()["name"]
            names[name] = m.preferred
        out[found["label"]] = {**found, "names": names}
    return out


def test_names_and_markers() -> None:
    assert versions.split_name("thesis_v2.docx") == ("thesis", "docx", True)
    assert versions.split_name("Report v2 final (1).PDF") == ("report", "pdf", True)
    assert versions.split_name("notes 2021-03-04.md") == ("notes", "md", True)
    assert versions.split_name("IMG_0001.jpg") == ("img_0001", "jpg", False)
    assert versions.version_number("thesis_v12.docx") == 12
    assert versions.version_number("aims final.docx") is None


def test_numbered_versions_prefer_the_highest_number(drafts) -> None:
    catalog, _ = drafts
    thesis = _sets(catalog)["thesis.docx"]
    assert set(thesis["names"]) == {"thesis_v1.docx", "thesis_v2.docx", "thesis_v3.docx"} \
        or set(thesis["names"]) == {"thesis_v1 copy.docx", "thesis_v2.docx", "thesis_v3.docx"}
    assert thesis["names"]["thesis_v3.docx"] is True
    assert "highest version number" in thesis["rationale"]
    assert thesis["confidence"] > 0.9, "sizes close together"


def test_unnumbered_versions_prefer_the_newest_and_drift_lowers_confidence(drafts) -> None:
    catalog, _ = drafts
    aims = _sets(catalog)["aims.docx"]
    assert set(aims["names"]) == {"aims.docx", "aims final.docx", "aims (1).docx"}
    assert aims["names"]["aims.docx"] is True and "most recently modified" in aims["rationale"]
    assert aims["confidence"] < 0.8


def test_what_is_not_a_version(drafts) -> None:
    catalog, _ = drafts
    labels = set(_sets(catalog))
    assert labels == {"thesis.docx", "aims.docx"}, "photos and other folders aren't grouped"
    thesis = _sets(catalog)["thesis.docx"]
    assert len(thesis["names"]) == 3, "an identical copy is a duplicate, not a version"


def test_refresh_stores_and_replaces(drafts, capsys) -> None:
    catalog, path = drafts
    assert versions.refresh(catalog) == 2
    assert versions.refresh(catalog) == 2
    assert len(curation.version_sets(catalog)) == 2
    assert main(["--catalog", str(path), "versions", "list"]) == 0
    out = capsys.readouterr().out
    assert "thesis.docx" in out and "* " in out and "thesis_v3.docx" in out
    assert main(["--catalog", str(path), "versions", "find"]) == 0
    assert "2 version set(s) found" in capsys.readouterr().out
