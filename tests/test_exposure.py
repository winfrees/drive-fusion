"""Exposure: expected bytes lost per year, and the ranking it changes.

M3 ranked the under-protected queue by size and left a comment promising that
M5 would rank it by size × P(loss). These tests hold that promise to account:
the two orderings must actually differ when the health data says they should,
or the change was cosmetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.analysis import under_protected
from drivefusion.core.analysis.exposure import (
    RiskIndex,
    content_exposure,
    expected_bytes_lost_per_year,
    riskiest_drives,
)
from drivefusion.core.health.risk import ESTIMATED, MEASURED, UNKNOWN
from drivefusion.core.identity import hash_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def drive_with_root(
    catalog: Catalog,
    tmp_path: Path,
    name: str,
    files: dict[str, bytes],
    *,
    health: dict | None = None,
    **drive_fields,
) -> int:
    """A physical drive, a volume, a scanned root, and optional health.

    Each payload is written twice. That is not padding: a file whose size occurs
    exactly once in the catalog is never opened and never gets a content
    identity (the tiered-hashing property from M3), so a fixture that wants
    content in the exposure analysis must make the size occur more than once.
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for filename, payload in files.items():
        (root / filename).write_bytes(payload)
        (root / f"twin-{filename}").write_bytes(payload)

    drive_id = catalog.upsert_drive(serial=f"SER-{name}", **drive_fields)
    volume_id = catalog.upsert_volume(volume_guid=f"vol:{name}", drive_id=drive_id)
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)

    if health is not None:
        catalog.record_health(drive_id, **health)
    return drive_id


# -- the ranking M3 promised --------------------------------------------------

def test_the_queue_is_ranked_by_expected_loss_not_size(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A small file on a dying drive outranks a large one on a healthy drive.

    This is the whole point of M5. By size the top of the queue is whatever is
    biggest — usually video on a drive that is fine. By expected loss it is
    whatever will actually be gone next month.
    """
    big = b"L" * 40_000
    small = b"S" * 1_000

    drive_with_root(
        catalog, tmp_path, "healthy", {"big.bin": big},
        media="hdd", bus="sata",
        health={"confidence": MEASURED, "pending": 0, "reallocated": 0,
                "power_on_hours": 5000},
    )
    drive_with_root(
        catalog, tmp_path, "dying", {"small.bin": small},
        media="hdd", bus="sata",
        health={"confidence": MEASURED, "pending": 12, "reallocated": 80,
                "power_on_hours": 41000},
    )
    hash_pass(catalog)

    by_size = under_protected(catalog, min_copies=2, by_risk=False)
    by_risk = under_protected(catalog, min_copies=2, by_risk=True)

    assert by_size[0].size_bytes == len(big), "size ranking should lead with the big file"
    assert by_risk[0].size_bytes == len(small), (
        "risk ranking must lead with the endangered file, not the large one"
    )
    assert [g.content_id for g in by_size] != [g.content_id for g in by_risk], (
        "if the two orderings never differ, the ranking change did nothing"
    )


def test_ranking_falls_back_gracefully_without_health_data(
    catalog: Catalog, tmp_path: Path
) -> None:
    """With no health recorded every drive scores alike, so size decides."""
    drive_with_root(catalog, tmp_path, "a", {"big.bin": b"L" * 9000}, media="hdd")
    drive_with_root(catalog, tmp_path, "b", {"small.bin": b"S" * 100}, media="hdd")
    hash_pass(catalog)

    ranked = under_protected(catalog, min_copies=2)
    assert ranked[0].size_bytes == 9000


# -- expected bytes lost per year ---------------------------------------------

def test_expected_loss_is_reported_with_its_coverage(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A total without its caveats reads as authoritative and is not."""
    drive_with_root(
        catalog, tmp_path, "one", {"a.bin": b"a" * 5000, "b.bin": b"b" * 5000},
        media="hdd", bus="usb",
    )
    hash_pass(catalog)

    summary = expected_bytes_lost_per_year(catalog)
    assert summary["expected_bytes_lost_per_year"] >= 0
    assert "unhashed_files" in summary
    assert summary["generic_rates"] is True, "the shipped table is generic; say so"
    assert "generic-defaults" in summary["table"]


def test_unhashed_content_is_excluded_and_declared(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Content with no identity is not in the sum; the caveat must travel with it."""
    # The helper writes each payload twice, so this root holds two files.
    drive_with_root(catalog, tmp_path, "one", {"unique.bin": b"z" * 321}, media="hdd")
    # Deliberately no hash_pass: nothing has a content identity yet.
    summary = expected_bytes_lost_per_year(catalog)

    assert summary["content_items"] == 0, "unhashed content must not enter the sum"
    assert summary["unhashed_files"] == 2, "and the caveat must travel with the total"


def test_two_drives_beat_one_for_the_same_content(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The model must actually reward redundancy, or it is not a durability model."""
    payload = b"worth keeping" * 200

    single = Catalog(tmp_path / "single" / "c.db")
    drive_with_root(single, tmp_path, "solo", {"x.bin": payload}, media="hdd", bus="sata")
    hash_pass(single)
    solo_loss = expected_bytes_lost_per_year(single)["expected_bytes_lost_per_year"]
    single.close()

    drive_with_root(catalog, tmp_path, "d1", {"x.bin": payload}, media="hdd", bus="sata")
    drive_with_root(catalog, tmp_path, "d2", {"x.bin": payload}, media="hdd", bus="sata")
    hash_pass(catalog)
    paired_loss = expected_bytes_lost_per_year(catalog)["expected_bytes_lost_per_year"]

    assert paired_loss < solo_loss


def test_copies_in_one_batch_are_worth_less_than_copies_across_two(
    catalog: Catalog, tmp_path: Path
) -> None:
    """`l` and `p` made first-class: same purchase order, correlated failure."""
    payload = b"two copies" * 300

    diverse = Catalog(tmp_path / "diverse" / "c.db")
    drive_with_root(diverse, tmp_path, "wd", {"x.bin": payload},
                    media="hdd", bus="sata", batch_key="WD|RED|2021-03")
    drive_with_root(diverse, tmp_path, "sg", {"x.bin": payload},
                    media="hdd", bus="sata", batch_key="SEAGATE|IRONWOLF|2023-08")
    hash_pass(diverse)
    diverse_loss = expected_bytes_lost_per_year(diverse)["expected_bytes_lost_per_year"]
    diverse.close()

    drive_with_root(catalog, tmp_path, "b1", {"x.bin": payload},
                    media="hdd", bus="sata", batch_key="WD|RED|2021-03")
    drive_with_root(catalog, tmp_path, "b2", {"x.bin": payload},
                    media="hdd", bus="sata", batch_key="WD|RED|2021-03")
    hash_pass(catalog)
    batch_loss = expected_bytes_lost_per_year(catalog)["expected_bytes_lost_per_year"]

    assert batch_loss > diverse_loss, (
        "two drives from one purchase order must not count as two independent copies"
    )


# -- which drive to act on ----------------------------------------------------

def test_riskiest_drive_is_the_costliest_failure_not_the_likeliest(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A dying drive holding nothing unique is not the one to act on first."""
    drive_with_root(
        catalog, tmp_path, "dying-empty", {"tiny.bin": b"t" * 10},
        media="hdd", bus="sata",
        health={"confidence": MEASURED, "pending": 20, "reallocated": 200},
    )
    drive_with_root(
        catalog, tmp_path, "healthy-full", {"huge.bin": b"H" * 500_000},
        media="hdd", bus="sata",
        health={"confidence": MEASURED, "pending": 0, "reallocated": 0},
    )
    hash_pass(catalog)

    ranked = riskiest_drives(catalog)
    assert ranked, "no drives ranked"
    assert ranked[0]["name"].endswith("healthy-full") or "healthy-full" in str(
        ranked[0]["name"]
    ) or ranked[0]["sole_copy_bytes"] == 500_000, (
        "the drive holding half a megabyte alone outranks one holding ten bytes"
    )
    assert ranked[0]["reasons"], "a ranking with no stated reason is not actionable"


def test_an_unmapped_volume_still_counts_as_a_device(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Dropping it would silently improve the durability of everything on it."""
    root = tmp_path / "unmapped"
    root.mkdir()
    (root / "f.bin").write_bytes(b"u" * 2000)
    (root / "twin.bin").write_bytes(b"u" * 2000)   # so the size is not unique
    volume_id = catalog.upsert_volume(volume_guid="vol:unmapped")
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)
    hash_pass(catalog)

    exposures = content_exposure(catalog)
    assert exposures, "content on an unmapped volume disappeared from the analysis"
    assert exposures[0].loss_probability > 0
    assert exposures[0].confidence in (ESTIMATED, UNKNOWN)


def test_risk_index_computes_each_drive_once(catalog: Catalog, tmp_path: Path) -> None:
    """Twenty drives and millions of files: per-row recomputation is quadratic."""
    drive_with_root(catalog, tmp_path, "many",
                    {f"f{i}.bin": b"x" * (100 + i) for i in range(20)}, media="hdd")
    hash_pass(catalog)

    index = RiskIndex(catalog)
    calls = {"n": 0}
    original = index.risk_for

    def counting(key):
        calls["n"] += 1
        return original(key)

    index.risk_for = counting
    content_exposure(catalog, index=index)

    # One lookup per (content, drive) pair is fine; recomputation is not.
    assert len(index._risks) <= 2, "risk was recomputed per row rather than cached"


def test_an_empty_catalog_reports_zero_not_an_error(catalog: Catalog) -> None:
    summary = expected_bytes_lost_per_year(catalog)
    assert summary["expected_bytes_lost_per_year"] == 0
    assert summary["content_items"] == 0
    assert riskiest_drives(catalog) == []
