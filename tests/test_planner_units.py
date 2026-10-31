"""Placement units, footprint and capacity (ROADMAP M7.2-M7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.planner import capacity, units
from drivefusion.core.store import curation
from drivefusion.core.store.catalog import Catalog

GiB = 1 << 30


class Fleet:
    def __init__(self, catalog: Catalog) -> None:
        self.c = catalog
        self.vol, self.root, self.n = {}, {}, 0

    def drive(self, name, *, cluster=4096, capacity=100 * GiB, free=50 * GiB, **cols):
        d = self.c.upsert_drive(serial=name, media="hdd")
        for k, v in cols.items():
            self.c.conn.execute(f"UPDATE drive SET {k} = ? WHERE id = ?", (v, d))
        v = self.c.conn.execute(
            "INSERT INTO volume (volume_guid, drive_id, cluster_bytes, capacity_bytes, "
            "free_bytes) VALUES (?, ?, ?, ?, ?)", (name, d, cluster, capacity, free)).lastrowid
        self.vol[name] = v
        self.root[name] = self.mkdir(name, f"/{name}", None)
        return d

    def mkdir(self, drive, name, parent="root"):
        parent_id = self.root[drive] if parent == "root" else parent
        return self.c.conn.execute(
            "INSERT INTO dir (volume_id, parent_id, name, depth) VALUES (?, ?, ?, 0)",
            (self.vol[drive], parent_id, name)).lastrowid

    def file(self, drive, dir_id, size, content=None):
        self.n += 1
        return self.c.conn.execute(
            "INSERT INTO file (volume_id, dir_id, name, size_bytes, content_id) "
            "VALUES (?, ?, ?, ?, ?)", (self.vol[drive], dir_id, f"f{self.n}", size, content)).lastrowid

    def content(self, size):
        return self.c.conn.execute("INSERT INTO content (size_bytes) VALUES (?)",
                                   (size,)).lastrowid


@pytest.fixture
def fleet(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        f = Fleet(catalog)
        f.a = f.drive("A")
        f.b = f.drive("B", cluster=131072)
        f.project = f.mkdir("A", "Project")
        f.misc = f.mkdir("A", "Misc")
        f.sub = [f.mkdir("A", f"s{i}", f.misc) for i in range(3)]
        shared = [f.content(3 << 20) for _ in range(2)]
        for c in shared:                                  # Project: both items also on B
            f.file("A", f.project, 3 << 20, c)
            f.file("B", f.root["B"], 3 << 20, c)
        for i, d in enumerate(f.sub):                     # Misc: 2 MiB per subfolder
            f.file("A", d, 2 << 20)
        f.file("A", f.misc, 1 << 20)                      # loose in Misc
        f.coll = curation.create_collection(catalog, "Project", dir_ids=[f.project],
                                            ndsa_target=2)
        yield f


def by_label(found):
    return {u.label: u for u in found}


def test_every_live_file_is_in_exactly_one_unit(fleet) -> None:
    found = units.build(fleet.c, target_units=4)
    total = fleet.c.conn.execute("SELECT COUNT(*) FROM file").fetchone()[0]
    assert sum(u.files for u in found) == total
    keys = [u.key for u in found]
    assert len(keys) == len(set(keys))


def test_a_collection_folder_is_a_unit_with_its_target(fleet) -> None:
    project = next(u for u in units.build(fleet.c) if u.kind == "collection")
    assert project.label == "Project" and project.target == 3, "NDSA target 2 → 3 copies"
    assert project.holders == {fleet.a, fleet.b} and project.copies == 2
    curation.edit_collection(fleet.c, fleet.coll, {"min_copies": 4})
    assert next(u for u in units.build(fleet.c) if u.kind == "collection").target == 4


def test_unfiled_content_is_cut_under_the_threshold(fleet) -> None:
    whole = [u for u in units.build(fleet.c, target_units=1) if u.kind != "collection"]
    assert {u.kind for u in whole} == {"folder"}, "one threshold covers everything unfiled"
    cut = [u for u in units.build(fleet.c, target_units=50) if u.kind != "collection"]
    kinds = sorted(u.kind for u in cut)
    assert kinds.count("folder") >= 3 and "loose" in kinds, kinds
    assert all(u.target == units.DEFAULT_TARGET for u in cut)


def test_holders_are_the_drives_with_every_item(fleet) -> None:
    c = fleet.c
    extra = fleet.content(5 << 20)
    fleet.file("A", fleet.project, 5 << 20, extra)        # only on A
    project = next(u for u in units.build(c) if u.kind == "collection")
    assert project.holders == {fleet.a} and project.items == 3


def test_footprint_rounds_each_file_up_to_the_cluster(fleet) -> None:
    c = fleet.c
    d = fleet.mkdir("A", "Tiny")
    fleet.file("A", d, 1)
    fleet.file("A", d, 4097)
    found = units.build(c, clusters=(4096, 131072), target_units=50)
    tiny = next(u for u in found if u.dir_id == d)
    assert tiny.footprint == {4096: 4096 + 8192, 131072: 2 * 131072}
    assert capacity.footprint([1, 4097, 0], 4096) == 12288


def test_units_are_ranked_by_bytes_at_risk(fleet) -> None:
    found = units.build(fleet.c, target_units=50)
    risk = [u.bytes * u.p_loss for u in found]
    assert risk == sorted(risk, reverse=True)
    project = next(u for u in found if u.kind == "collection")
    single = max((u for u in found if u.kind == "folder"), key=lambda u: u.p_loss)
    assert single.p_loss > project.p_loss, "one copy is riskier than two"


def test_destinations_keep_a_reserve_and_say_why_they_are_excluded(fleet) -> None:
    c = fleet.c
    fleet.drive("R", retired_at="2025-01-01")
    fleet.drive("W", role="working")
    small = fleet.drive("S", capacity=4 * GiB, free=3 * GiB)
    c.conn.execute("INSERT INTO volume (volume_guid, drive_id) VALUES ('unknown', "
                   "(SELECT upsert FROM (SELECT ? AS upsert)))", (c.upsert_drive(serial="U"),))
    dests = {d.label: d for d in capacity.destinations(c)}
    a = next(d for d in dests.values() if d.drive_id == fleet.a)
    assert a.reserve == 10 * GiB and a.room == 40 * GiB and not a.excluded
    s = next(d for d in dests.values() if d.drive_id == small)
    assert s.reserve == GiB and s.room == 2 * GiB, "the 1 GiB floor"
    reasons = {d.excluded.split()[0] for d in dests.values() if d.excluded}
    assert {"retired", "its"} <= reasons
    assert any("capacity or free space is unknown" in d.excluded for d in dests.values())
    b = next(d for d in dests.values() if d.drive_id == fleet.b)
    assert b.cluster_bytes == 131072
