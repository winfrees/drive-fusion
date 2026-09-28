"""Incremental rescan, asserted against the full scan it must agree with.

The central property: **a delta must leave the catalog in the same state a
full rescan would.** If that ever stops holding, the fast path is silently
producing a different picture of the user's drives than the slow one, and
every copy count downstream inherits the difference.

These run on Linux because the orchestration takes its records and its lister
as arguments. Directory frns are real inodes here, which is what lets a
synthetic journal reference the same directories the scanner catalogued.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from drivefusion.core.enum.batchwalk import fsio_lister
from drivefusion.core.enum.delta import (
    DeltaNotApplicable,
    apply_delta,
    touched_parent_frns,
)
from drivefusion.core.enum.records import UsnRecord
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

DIRECTORY = 0x10


def usn(frn: int, parent_frn: int, name: str, *, is_dir: bool = False) -> UsnRecord:
    return UsnRecord(
        frn=frn,
        parent_frn=parent_frn,
        name=name,
        attributes=DIRECTORY if is_dir else 0,
        usn=1,
    )


def inode(path: Path) -> int:
    return os.stat(path).st_ino


def full_scan(catalog: Catalog, root: Path) -> dict:
    volume_id = catalog.upsert_volume(volume_guid="delta:1", fs_type="ntfs")
    root_id = catalog.add_scope_root(volume_id, str(root))
    return scan_root(
        catalog, root_path=str(root), root_id=root_id, volume_id=volume_id
    )


def rescan(catalog: Catalog, root: Path) -> dict:
    stored = catalog.scope_roots()[0]
    return scan_root(
        catalog,
        root_path=stored.path,
        root_id=stored.id,
        volume_id=stored.volume_id,
    )


def delta(catalog: Catalog, root: Path, records, **kwargs) -> dict:
    stored = catalog.scope_roots()[0]
    kwargs.setdefault("lister", fsio_lister)
    return apply_delta(
        catalog,
        root_path=stored.path,
        root_id=stored.id,
        volume_id=stored.volume_id,
        records=records,
        **kwargs,
    )


def catalog_state(catalog: Catalog) -> set[tuple]:
    """Every live file as (path, name, size) — the picture a user would see."""
    return {
        (row["path"], row["name"], row["size_bytes"])
        for row in catalog.find("%", limit=10_000)
    }


def state_after_full_rescan(tmp_path: Path, tree: Path) -> set[tuple]:
    """What a full scan of the current tree produces, in a separate catalog."""
    with Catalog(tmp_path / "reference" / "catalog.db") as reference:
        full_scan(reference, tree)
        return catalog_state(reference)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "vol"
    (root / "docs" / "2024").mkdir(parents=True)
    (root / "media").mkdir()
    (root / "docs" / "a.txt").write_bytes(b"alpha")
    (root / "docs" / "2024" / "b.txt").write_bytes(b"beta")
    (root / "media" / "c.bin").write_bytes(b"c" * 500)
    return root


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


# -- record collection --------------------------------------------------------

def test_collects_parents_and_directory_frns() -> None:
    records = [
        usn(10, 5, "file.txt"),
        usn(11, 5, "other.txt"),
        usn(20, 5, "subdir", is_dir=True),
    ]
    parents, count, dir_records = touched_parent_frns(records)

    assert count == 3
    assert dir_records == 1
    # The directory contributes its own frn as well: a rename changes the paths
    # of everything beneath it, and re-reading it is how that is picked up.
    assert parents == {5, 20}


# -- equivalence with a full rescan -------------------------------------------

def test_no_changes_leaves_the_catalog_identical(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    before = catalog_state(catalog)

    result = delta(catalog, tree, [])

    assert catalog_state(catalog) == before
    assert result["counters"].records == 0
    assert result["merged"]["vanished"] == 0


def test_added_file_matches_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    (tree / "docs" / "new.txt").write_bytes(b"new content")

    delta(catalog, tree, [usn(999, inode(tree / "docs"), "new.txt")])

    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


def test_modified_file_matches_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    target = tree / "docs" / "a.txt"
    target.write_bytes(b"substantially longer content than before")

    delta(catalog, tree, [usn(inode(target), inode(tree / "docs"), "a.txt")])

    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


def test_deleted_file_matches_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    docs_inode = inode(tree / "docs")
    (tree / "docs" / "a.txt").unlink()

    result = delta(catalog, tree, [usn(12345, docs_inode, "a.txt")])

    assert result["merged"]["vanished"] == 1
    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


def test_renamed_file_matches_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    docs_inode = inode(tree / "docs")
    os.rename(tree / "docs" / "a.txt", tree / "docs" / "renamed.txt")

    delta(
        catalog,
        tree,
        [usn(1, docs_inode, "a.txt"), usn(1, docs_inode, "renamed.txt")],
    )

    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


def test_new_subtree_is_walked_and_matches_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    """A created directory brings unseen children the journal never named."""
    full_scan(catalog, tree)
    media_inode = inode(tree / "media")

    deep = tree / "media" / "trip" / "raw"
    deep.mkdir(parents=True)
    (tree / "media" / "trip" / "index.txt").write_bytes(b"index")
    (deep / "img1.raw").write_bytes(b"1" * 100)
    (deep / "img2.raw").write_bytes(b"2" * 200)

    result = delta(catalog, tree, [usn(777, media_inode, "trip", is_dir=True)])

    assert result["counters"].new_subtrees == 1
    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


def test_deleted_directory_tombstones_its_whole_subtree(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    docs_inode = inode(tree / "docs")
    shutil.rmtree(tree / "docs" / "2024")

    result = delta(catalog, tree, [usn(888, docs_inode, "2024", is_dir=True)])

    assert result["counters"].vanished_dirs == 1
    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)
    # The metadata survives the bytes.
    remembered = list(catalog.find("%b.txt%", include_vanished=True))
    assert len(remembered) == 1


def test_several_changes_at_once_match_a_full_rescan(
    catalog: Catalog, tree: Path, tmp_path: Path
) -> None:
    full_scan(catalog, tree)
    docs_inode = inode(tree / "docs")
    media_inode = inode(tree / "media")

    (tree / "docs" / "a.txt").unlink()
    (tree / "docs" / "added.txt").write_bytes(b"added")
    (tree / "media" / "c.bin").write_bytes(b"resized")

    delta(
        catalog,
        tree,
        [
            usn(1, docs_inode, "a.txt"),
            usn(2, docs_inode, "added.txt"),
            usn(3, media_inode, "c.bin"),
        ],
    )

    assert catalog_state(catalog) == state_after_full_rescan(tmp_path, tree)


# -- safety -------------------------------------------------------------------

def test_untouched_directories_are_never_tombstoned(
    catalog: Catalog, tree: Path
) -> None:
    """The scoping rule: a file in an unexamined directory is unknown, not gone.

    Applying the full-scan tombstone rule to a delta would declare the entire
    volume missing on the first incremental rescan.
    """
    full_scan(catalog, tree)
    before = catalog_state(catalog)

    # Name only one directory, and change nothing anywhere.
    result = delta(catalog, tree, [usn(1, inode(tree / "docs"), "a.txt")])

    assert result["merged"]["vanished"] == 0
    assert catalog_state(catalog) == before


def test_changes_outside_scope_are_counted_not_guessed(
    catalog: Catalog, tree: Path
) -> None:
    """An unresolvable parent has no correct location; inventing one is worse."""
    full_scan(catalog, tree)
    before = catalog_state(catalog)

    result = delta(catalog, tree, [usn(1, 99_999_999, "elsewhere.txt")])

    assert result["counters"].unknown_parents == 1
    assert catalog_state(catalog) == before


def test_very_large_change_sets_fall_back_to_a_full_pass(
    catalog: Catalog, tree: Path
) -> None:
    """Past a point a delta is more work than the walk it was avoiding."""
    full_scan(catalog, tree)
    every_dir = [
        int(row["frn"])
        for row in catalog.conn.execute(
            "SELECT frn FROM dir WHERE frn IS NOT NULL"
        )
    ]
    records = [usn(i, frn, f"f{i}.txt") for i, frn in enumerate(every_dir)]
    records += [usn(500 + i, 10_000 + i, f"g{i}.txt") for i in range(20)]

    with pytest.raises(DeltaNotApplicable, match="cheaper than a delta"):
        delta(catalog, tree, records)


def test_cursor_advances_only_after_the_merge(
    catalog: Catalog, tree: Path
) -> None:
    full_scan(catalog, tree)
    volume_id = catalog.scope_roots()[0].volume_id
    assert catalog.journal_cursor(volume_id) == (None, None)

    delta(catalog, tree, [], next_usn=4242, journal_id=77)

    assert catalog.journal_cursor(volume_id) == (77, 4242)


def test_unreadable_directory_does_not_tombstone_its_contents(
    catalog: Catalog, tree: Path
) -> None:
    """A locked directory is unknown, not empty."""
    full_scan(catalog, tree)
    before = catalog_state(catalog)

    def failing_lister(path: str):
        if path.endswith("docs"):
            raise OSError("access denied (simulated)")
        return fsio_lister(path)

    result = delta(
        catalog, tree, [usn(1, inode(tree / "docs"), "a.txt")],
        lister=failing_lister,
    )

    assert result["counters"].unreadable == 1
    assert result["merged"]["vanished"] == 0
    assert catalog_state(catalog) == before


def test_delta_is_recorded_as_its_own_scan(catalog: Catalog, tree: Path) -> None:
    full_scan(catalog, tree)
    delta(catalog, tree, [usn(1, inode(tree / "docs"), "a.txt")])

    row = catalog.conn.execute("SELECT * FROM scan ORDER BY id DESC").fetchone()
    assert row["method"] == "usn-delta"
    assert row["status"] == "done"
    assert row["elevated"] == 1
