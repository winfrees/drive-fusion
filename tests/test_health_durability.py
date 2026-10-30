"""P(loss) over drive sets and expected bytes lost a year (ROADMAP M5.5)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from drivefusion.core.analysis import under_protected
from drivefusion.core.health import afr, durability
from drivefusion.core.store.catalog import Catalog

TODAY = dt.date(2026, 10, 30)
PARAMS = afr.model()
HDD = afr.base_afr(None, "hdd", afr.tables())[0]        # whichever table is in force


class Fleet:
    """A catalog built row by row: drives, one volume each, and content on them."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.volumes: dict[int | str, int] = {}
        self.next_file = 0

    def drive(self, name: str, **columns) -> int:
        drive_id = self.catalog.upsert_drive(serial=name, media="hdd")
        if columns:
            sets = ", ".join(f"{k} = ?" for k in columns)
            self.catalog.conn.execute(f"UPDATE drive SET {sets} WHERE id = ?",
                                      (*columns.values(), drive_id))
        self.volumes[drive_id] = self._volume(name, drive_id)
        return drive_id

    def unverified_volume(self, name: str) -> str:
        self.volumes[name] = self._volume(name, None)
        return name

    def _volume(self, name: str, drive_id: int | None) -> int:
        conn = self.catalog.conn
        volume = conn.execute("INSERT INTO volume (volume_guid, drive_id) VALUES (?, ?)",
                              (f"\\\\?\\Volume{{{name}}}\\", drive_id)).lastrowid
        conn.execute("INSERT INTO dir (volume_id, name, depth) VALUES (?, '', 0)", (volume,))
        return volume

    def content(self, size: int, *where) -> int:
        conn = self.catalog.conn
        content = conn.execute("INSERT INTO content (size_bytes) VALUES (?)", (size,)).lastrowid
        for place in where:
            self._file(place, size, content)
        return content

    def single(self, size: int, place) -> None:
        self._file(place, size, None)

    def _file(self, place, size: int, content: int | None) -> None:
        volume = self.volumes[place]
        dir_id = self.catalog.conn.execute(
            "SELECT id FROM dir WHERE volume_id = ?", (volume,)).fetchone()["id"]
        self.next_file += 1
        self.catalog.conn.execute(
            "INSERT INTO file (volume_id, dir_id, name, size_bytes, content_id) "
            "VALUES (?, ?, ?, ?, ?)", (volume, dir_id, f"f{self.next_file}", size, content))


@pytest.fixture
def fleet(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        yield Fleet(catalog)


def by_drives(found: durability.Exposure) -> dict:
    return {s.drive_ids: s for s in found.sets}


# -- the formula ----------------------------------------------------------------------

def test_one_drive_loses_at_its_own_rate(fleet) -> None:
    a = fleet.drive("A")
    fleet.content(1000, a)
    [only] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert only.drive_ids == (a,) and only.p_loss == pytest.approx(HDD)
    assert only.expected_bytes_per_year == pytest.approx(1000 * HDD)


def test_two_drives_in_different_zones_multiply(fleet) -> None:
    a, b = fleet.drive("A", disaster_zone="home"), fleet.drive("B", disaster_zone="office")
    fleet.content(1000, a, b)
    [both] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert both.p_loss == pytest.approx(HDD * HDD) and both.correlation == 1.0


def test_copies_in_one_zone_or_batch_are_worth_less(fleet) -> None:
    """R-HL-02: two copies on one batch score worse than two across batches."""
    batch = {"manufacturer": "Seagate", "model": "ST4000DM004", "purchase_date": "2021-03"}
    a = fleet.drive("A", disaster_zone="home", **batch)
    b = fleet.drive("B", disaster_zone="office", **batch)
    c = fleet.drive("C", disaster_zone="office", manufacturer="WDC", model="WD40EFRX",
                    purchase_date="2022-06")
    fleet.content(1000, a, b)                     # one batch, two zones
    fleet.content(2000, a, c)                     # two batches, two zones
    fleet.content(3000, b, c)                     # two batches, one zone
    sets = by_drives(durability.exposure(fleet.catalog, today=TODAY))
    same_batch, apart, same_zone = sets[(a, b)], sets[(a, c)], sets[(b, c)]
    assert same_batch.p_loss > apart.p_loss
    assert same_batch.correlation == PARAMS.correlation["same_batch"]
    assert same_zone.correlation == PARAMS.correlation["same_zone"]
    assert any("one purchase batch" in n for n in same_batch.assumptions)


def test_an_unknown_zone_is_assumed_shared(fleet) -> None:
    a, b = fleet.drive("A"), fleet.drive("B")
    fleet.content(1000, a, b)
    [both] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert both.correlation == PARAMS.correlation["same_zone"]
    assert "no disaster zone recorded, assumed together" in both.assumptions[0]


def test_correlation_never_makes_all_copies_likelier_to_go_than_one(fleet) -> None:
    drives = [fleet.drive(name) for name in "ABCD"]       # one unknown zone, four drives
    fleet.content(1000, *drives)
    [all_four] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert all_four.p_loss <= HDD


# -- what is counted ------------------------------------------------------------------

def test_expected_loss_matches_independent_arithmetic(fleet) -> None:
    """R-HL-03: the total equals Σ size × P(loss) worked out by hand."""
    a = fleet.drive("A", disaster_zone="home", purchase_date="2019-01", bus="usb")
    b = fleet.drive("B", disaster_zone="office", purchase_date="2024-01")
    fleet.content(10_000, a)
    fleet.content(20_000, a, b)
    fleet.single(5_000, b)
    found = durability.exposure(fleet.catalog, today=TODAY)
    rate_a = HDD * 2.2 * 1.2                      # 7 years old, behind USB
    rate_b = HDD * 0.9                            # 2 years old
    expected = 10_000 * rate_a + 20_000 * rate_a * rate_b + 5_000 * rate_b
    assert found.expected_bytes_per_year == pytest.approx(expected)
    assert found.confidence == "estimated"


def test_every_copy_on_unverified_drives_counts_as_one_unknown_drive(fleet) -> None:
    fleet.unverified_volume("X")
    fleet.unverified_volume("Y")
    fleet.content(1000, "X", "Y")
    [only] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert only.drive_ids == durability.UNVERIFIED and only.confidence == "unknown"
    assert only.p_loss == pytest.approx(afr.base_afr(None, None, afr.tables())[0])


def test_an_unverified_copy_adds_no_protection(fleet) -> None:
    a = fleet.drive("A")
    fleet.unverified_volume("X")
    fleet.content(1000, a, "X")
    [only] = durability.exposure(fleet.catalog, today=TODAY).sets
    assert only.drive_ids == (a,), "the unverified volume may be a partition of A"


def test_unhashed_files_are_left_out_and_counted(fleet) -> None:
    a = fleet.drive("A")
    fleet.single(700, a)
    fleet.single(700, a)                          # a shared size: not known to be one copy
    found = durability.exposure(fleet.catalog, today=TODAY)
    assert found.unhashed_files == 2 and found.expected_bytes_per_year == 0


def test_under_protected_is_ranked_by_bytes_at_risk_not_size(fleet) -> None:
    old = fleet.drive("OLD", purchase_date="2016-01", bus="usb")
    new = fleet.drive("NEW", purchase_date="2024-01")
    small = fleet.content(1000, old)
    large = fleet.content(1500, new)
    groups = under_protected(fleet.catalog)
    assert [g.content_id for g in groups] == [large, small], "by size, before M5.5"
    ranked = durability.ranked_under_protected(fleet.catalog, groups)
    assert [g.content_id for g, _ in ranked] == [small, large]
    assert ranked[0][1].p_loss > ranked[1][1].p_loss
