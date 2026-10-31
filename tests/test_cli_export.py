"""`drivefusion export` (ROADMAP M8.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store import curation
from tests.test_planner_units import Fleet


def build(path: Path) -> None:
    with Catalog(path) as catalog:
        f = Fleet(catalog)
        f.drive("A", nickname="ARCHIVE-1")
        f.drive("B", nickname="ARCHIVE-2")
        project = f.mkdir("A", "Project")
        f.file("A", project, 5 << 20)
        curation.create_collection(catalog, "Project", dir_ids=[project])


def test_export_all_then_again_to_the_remembered_folder(tmp_path: Path, capsys) -> None:
    db = tmp_path / "catalog.db"
    build(db)
    out = tmp_path / "exports"
    out.mkdir()
    run = lambda *a: main(["--catalog", str(db), *a])  # noqa: E731

    assert run("export", "reports") == 1
    assert "choose an export folder" in capsys.readouterr().err
    assert run("export", "all", "--to", str(out)) == 0
    text = capsys.readouterr().out
    for kind in ("manifests", "collections", "mappings", "reports"):
        assert f"{kind}: {out}" in text
    assert "nothing on any scanned drive was changed" in text
    bundles = sorted(p for p in out.iterdir())
    assert len(bundles) == 4
    record = json.loads((bundles[0] / "_EXPORT.json").read_text(encoding="utf-8"))
    assert record["standards"]["rubrics"]["fair"]["version"] == "fair-2016"

    assert run("export", "manifests") == 0                  # the remembered folder
    assert len(list(out.iterdir())) == 5
    capsys.readouterr()
    assert run("export", "list") == 0
    listed = capsys.readouterr().out.splitlines()
    assert len([line for line in listed if "files" in line]) == 5
    assert run("export", "mappings", "--template", "{nope}") == 1
    with pytest.raises(SystemExit):
        run("export")
