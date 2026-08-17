"""Redundancy analysis: what counts as a copy, and what counts as waste.

These assertions are the ones a user would act on. Getting them wrong does not
produce an error — it produces a confident number that is false, which is the
worst failure this tool can have.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drivefusion.core.analysis import (
    duplicate_groups,
    integrity_incidents,
    reclamation_candidates,
    redundancy_summary,
    under_protected,
)
from drivefusion.core.identity import hash_pass, verify_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

PAYLOAD = b"shared content bytes" * 50


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def add_drive_volume(catalog: Catalog, serial: str, guid: str) -> int:
    """A volume backed by its own physical drive."""
    drive_id = catalog.upsert_drive(serial=serial)
    return catalog.upsert_volume(
        volume_guid=guid, fs_type="ntfs", drive_id=drive_id
    )


def scan_into(catalog: Catalog, volume_id: int, root: Path) -> None:
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)


def two_drive_setup(catalog: Catalog, tmp_path: Path) -> tuple[Path, Path]:
    """The same content on two genuinely separate drives."""
    first = tmp_path / "driveA"
    second = tmp_path / "driveB"
    first.mkdir()
    second.mkdir()
    (first / "shared.bin").write_bytes(PAYLOAD)
    (second / "shared.bin").write_bytes(PAYLOAD)

    scan_into(catalog, add_drive_volume(catalog, "SERIAL-A", "vol:a"), first)
    scan_into(catalog, add_drive_volume(catalog, "SERIAL-B", "vol:b"), second)
    hash_pass(catalog)
    return first, second


# -- what counts as a copy ----------------------------------------------------

def test_copies_are_counted_across_distinct_drives(
    catalog: Catalog, tmp_path: Path
) -> None:
    two_drive_setup(catalog, tmp_path)

    groups = duplicate_groups(catalog)
    assert len(groups) == 1
    assert groups[0].paths == 2
    assert groups[0].drives == 2


def test_two_paths_on_one_drive_are_one_copy(
    catalog: Catalog, tmp_path: Path
) -> None:
    """If that drive dies, both go together — this is the central rule."""
    root = tmp_path / "one"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "x.bin").write_bytes(PAYLOAD)
    (root / "b" / "x.bin").write_bytes(PAYLOAD)

    scan_into(catalog, add_drive_volume(catalog, "SERIAL-ONLY", "vol:one"), root)
    hash_pass(catalog)

    group = duplicate_groups(catalog)[0]
    assert group.paths == 2
    assert group.drives == 1, "same drive is one copy, however many paths"

    # And it is therefore under-protected despite existing twice.
    assert len(under_protected(catalog, min_copies=2)) == 1


def test_hardlinks_are_not_a_second_copy(catalog: Catalog, tmp_path: Path) -> None:
    """A hardlink is the same file seen twice, not a duplicate of it.

    Counting it as one would inflate both the copy count and the reclaimable
    bytes — the two numbers a user would act on.
    """
    root = tmp_path / "vol"
    root.mkdir()
    original = root / "original.bin"
    original.write_bytes(PAYLOAD)
    link = root / "hardlink.bin"
    try:
        os.link(original, link)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks not supported here")

    scan_into(catalog, add_drive_volume(catalog, "SERIAL-H", "vol:h"), root)
    hash_pass(catalog)

    group = duplicate_groups(catalog)[0]
    assert group.paths == 2
    assert group.physical_copies == 1, "one inode is one physical copy"
    assert group.reclaimable_bytes == 0, "removing a hardlink frees nothing"


def test_volume_without_a_known_drive_counts_as_its_own(
    catalog: Catalog, tmp_path: Path
) -> None:
    """An unmapped disk must never be silently merged with another."""
    first = tmp_path / "u1"
    second = tmp_path / "u2"
    first.mkdir()
    second.mkdir()
    (first / "s.bin").write_bytes(PAYLOAD)
    (second / "s.bin").write_bytes(PAYLOAD)

    # Neither volume has a drive_id.
    scan_into(catalog, catalog.upsert_volume(volume_guid="vol:x"), first)
    scan_into(catalog, catalog.upsert_volume(volume_guid="vol:y"), second)
    hash_pass(catalog)

    assert duplicate_groups(catalog)[0].drives == 2


# -- the reports --------------------------------------------------------------

def test_under_protected_lists_single_drive_content(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "solo"
    root.mkdir()
    (root / "only.bin").write_bytes(b"q" * 4096)
    (root / "also.bin").write_bytes(b"q" * 4096)
    scan_into(catalog, add_drive_volume(catalog, "SERIAL-S", "vol:s"), root)
    hash_pass(catalog)

    exposed = under_protected(catalog, min_copies=2)
    assert len(exposed) == 1
    assert exposed[0].drives == 1


def test_content_on_two_drives_is_not_under_protected(
    catalog: Catalog, tmp_path: Path
) -> None:
    two_drive_setup(catalog, tmp_path)
    assert under_protected(catalog, min_copies=2) == []
    assert len(under_protected(catalog, min_copies=3)) == 1


def test_reclamation_only_counts_same_drive_duplicates(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Cross-drive copies are the goal, never waste."""
    two_drive_setup(catalog, tmp_path)
    assert reclamation_candidates(catalog) == []


def test_reclaimable_bytes_are_the_extra_copies_on_one_drive(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "dupes"
    root.mkdir()
    for name in ("a.bin", "b.bin", "c.bin"):
        (root / name).write_bytes(PAYLOAD)
    scan_into(catalog, add_drive_volume(catalog, "SERIAL-D", "vol:d"), root)
    hash_pass(catalog)

    candidate = reclamation_candidates(catalog)[0]
    assert candidate.physical_copies == 3
    assert candidate.drives == 1
    # Keeping one on this drive; the other two are space with no durability.
    assert candidate.reclaimable_bytes == 2 * len(PAYLOAD)


def test_summary_reports_unhashed_coverage(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A summary over a partly-hashed catalog must say so."""
    root = tmp_path / "vol"
    root.mkdir()
    (root / "unique.bin").write_bytes(b"z" * 321)   # unique size: never hashed
    (root / "a.bin").write_bytes(PAYLOAD)
    (root / "b.bin").write_bytes(PAYLOAD)
    scan_into(catalog, add_drive_volume(catalog, "SERIAL-C", "vol:c"), root)

    before = redundancy_summary(catalog)
    assert before["unhashed_files"] == 3

    hash_pass(catalog)
    after = redundancy_summary(catalog)

    assert after["unhashed_files"] == 1, "the unique-size file is never opened"
    assert after["distinct_content"] == 1
    assert after["duplicate_overhead_bytes"] == len(PAYLOAD)


def test_summary_on_an_empty_catalog_is_zeroed_not_broken(
    catalog: Catalog,
) -> None:
    summary = redundancy_summary(catalog)
    assert summary["distinct_content"] == 0
    assert summary["duplicate_overhead_bytes"] == 0
    assert summary["reclaimable_bytes"] == 0


# -- integrity ----------------------------------------------------------------

def test_integrity_report_names_the_copies_that_could_repair(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A corruption report without "where a good copy lives" is only half."""
    from drivefusion.core.identity import hasher

    size = hasher.SMALL_FILE_BYTES * 4
    first = tmp_path / "driveA"
    second = tmp_path / "driveB"
    first.mkdir()
    second.mkdir()
    payload = bytes(b"m" * size)
    (first / "big.bin").write_bytes(payload)
    (second / "big.bin").write_bytes(payload)

    scan_into(catalog, add_drive_volume(catalog, "SERIAL-A", "vol:a"), first)
    scan_into(catalog, add_drive_volume(catalog, "SERIAL-B", "vol:b"), second)
    hash_pass(catalog)

    target = first / "big.bin"
    stat = os.stat(target)
    damaged = bytearray(payload)
    damaged[size // 3] = ord("!")
    target.write_bytes(bytes(damaged))
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    verify_pass(catalog)
    incidents = integrity_incidents(catalog)

    assert len(incidents) == 1
    assert incidents[0]["result"] == "mismatch"
    assert incidents[0]["name"] == "big.bin"
    assert incidents[0]["other_copies"] == 2


def test_no_incidents_when_everything_verifies(
    catalog: Catalog, tmp_path: Path
) -> None:
    two_drive_setup(catalog, tmp_path)
    result = verify_pass(catalog)
    # "No incidents" only means something if something was actually checked.
    assert result["checked"] == 2
    assert integrity_incidents(catalog) == []
