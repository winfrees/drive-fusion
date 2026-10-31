"""Per-drive manifests (ROADMAP M8.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.export import manifests, writer
from drivefusion.core.store import curation
from drivefusion.core.store.catalog import Catalog
from tests.test_planner_units import Fleet


@pytest.fixture
def fleet(tmp_path: Path):
    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        f = Fleet(catalog)
        f.a = f.drive("A", nickname="ARCHIVE-1", disaster_zone="home")
        f.b = f.drive("B", nickname="ARCHIVE-2")
        f.project = f.mkdir("A", "Project")
        shared = f.content(300)
        catalog.conn.execute("UPDATE content SET full_hash = ?, hash_algo = 'blake3' "
                             "WHERE id = ?", (bytes(range(32)), shared))
        f.file("A", f.project, 300, shared)
        f.file("B", f.root["B"], 300, shared)
        f.file("A", f.project, 77)
        f.file("A", f.root["A"], 5)
        catalog.conn.execute("INSERT INTO volume (volume_guid, label) VALUES ('loose', 'USB')")
        loose = catalog.conn.execute("SELECT id FROM volume WHERE volume_guid='loose'"
                                     ).fetchone()[0]
        f.vol["U"] = loose
        f.root["U"] = f.mkdir("U", "/U", None)
        f.file("U", f.root["U"], 300, shared)
        curation.create_collection(catalog, "Project", dir_ids=[f.project])
        f.out = tmp_path / "exports"
        f.out.mkdir()
        yield f


def test_one_manifest_per_drive_then_per_unconfirmed_volume(fleet) -> None:
    made = manifests.build(fleet.c)
    assert [(m["kind"], m["label"]) for m in made] == [
        ("drive", "ARCHIVE-1"), ("drive", "ARCHIVE-2"), ("volume", "unconfirmed volume USB")]
    a = made[0]
    assert a["drive"]["disaster_zone"] == "home"
    assert a["totals"] == {"files": 3, "bytes": 382, "only_here": 2}
    assert len({m["folder"] for m in made}) == 3


def test_each_file_names_its_copies_elsewhere(fleet) -> None:
    a, b, u = manifests.build(fleet.c)
    shared = next(f for f in a["files"] if f["bytes"] == 300)
    assert shared["hash"] == "blake3:" + bytes(range(32)).hex()
    assert shared["collection"] == "Project"
    where = sorted((s["drive"] if s["drive_id"] else s["volume"]) for s in
                   shared["copies_elsewhere"])
    assert where == ["ARCHIVE-2", "USB"]
    unique = next(f for f in a["files"] if f["bytes"] == 77)
    assert unique["hash"] is None and unique["copies_elsewhere"] == []
    on_usb = u["files"][0]
    assert {s.get("drive") for s in on_usb["copies_elsewhere"]} == {"ARCHIVE-1", "ARCHIVE-2"}


def test_written_into_a_bundle_with_the_markdown_beside(fleet) -> None:
    bundle = writer.open_bundle(fleet.c, fleet.out, "manifests")
    summary = manifests.write(bundle, fleet.c)
    bundle.close(summary=summary)
    assert summary == {"manifests": 3, "drives": 2, "files": 5}
    folders = sorted(p.name for p in bundle.path.iterdir() if p.is_dir())
    assert len(folders) == 3
    for folder in folders:
        data = json.loads((bundle.path / folder / "_DRIVE-FUSION.json").read_text(encoding="utf-8"))
        text = (bundle.path / folder / "_CATALOG.md").read_text(encoding="utf-8")
        assert data["format"] == "drive-fusion-manifest/1"
        assert data["label"] in text and "changed nothing on this drive" in text
    a = bundle.path / next(f for f in folders if f.startswith("ARCHIVE-1"))
    assert "| Project | 2 | 377 B |" in (a / "_CATALOG.md").read_text(encoding="utf-8")
