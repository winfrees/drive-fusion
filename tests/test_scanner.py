"""The scan pipeline, end to end against a real fixture tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.scan import preview_root, scan_root
from drivefusion.core.scope.registry import ExclusionSet
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    # Deliberately outside the scanned tree: a catalog written inside the tree
    # would be a change to the thing under observation.
    with Catalog(tmp_path / "catalog" / "catalog.db") as cat:
        yield cat


def scan(catalog: Catalog, root: Path, excludes=None, lister=None) -> dict:
    volume_id = catalog.upsert_volume(volume_guid="test:1", fs_type="exfat")
    root_id = catalog.add_scope_root(volume_id, str(root))
    return scan_root(
        catalog,
        root_path=str(root),
        root_id=root_id,
        volume_id=volume_id,
        excludes=excludes,
        lister=lister,
    )


def rescan(catalog: Catalog, root: Path, excludes=None) -> dict:
    stored = catalog.scope_roots()[0]
    return scan_root(
        catalog,
        root_path=stored.path,
        root_id=stored.id,
        volume_id=stored.volume_id,
        excludes=excludes,
    )


def test_scan_catalogs_every_file(catalog: Catalog, sample_tree: Path) -> None:
    result = scan(catalog, sample_tree)

    expected = {
        p for p in sample_tree.rglob("*") if p.is_file() and not p.is_symlink()
    }
    assert result["counters"].files == len(expected)
    assert catalog.counts()["files"] == len(expected)
    assert result["counters"].bytes == sum(p.stat().st_size for p in expected)


def test_scan_records_paths_that_reconstruct(
    catalog: Catalog, sample_tree: Path
) -> None:
    scan(catalog, sample_tree)
    matches = list(catalog.find("%report.txt%"))
    paths = {f"{m['path']}/{m['name']}" for m in matches}
    assert f"{sample_tree}/docs/report.txt" in paths


def test_rescan_is_idempotent(catalog: Catalog, sample_tree: Path) -> None:
    """Regression: duplicate roots produced a duplicate catalog every scan."""
    first = scan(catalog, sample_tree)
    before = catalog.counts()

    second = rescan(catalog, sample_tree)
    after = catalog.counts()

    assert before == after
    assert second["merged"]["inserted"] == 0
    assert second["merged"]["updated"] == first["counters"].files
    assert second["merged"]["vanished"] == 0

    roots = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM dir WHERE parent_id IS NULL"
    ).fetchone()["n"]
    assert roots == 1


def test_deleted_files_are_tombstoned_not_removed(
    catalog: Catalog, sample_tree: Path
) -> None:
    scan(catalog, sample_tree)
    (sample_tree / "docs" / "report.txt").unlink()

    result = rescan(catalog, sample_tree)
    assert result["merged"]["vanished"] == 1

    live = list(catalog.find("%report.txt%"))
    assert not any(m["name"] == "report.txt" for m in live)

    remembered = list(catalog.find("%report.txt%", include_vanished=True))
    assert any(m["name"] == "report.txt" for m in remembered)


def test_modified_files_update_in_place(catalog: Catalog, sample_tree: Path) -> None:
    scan(catalog, sample_tree)
    target = sample_tree / "docs" / "report.txt"
    target.write_bytes(b"different content entirely")

    rescan(catalog, sample_tree)
    row = catalog.conn.execute(
        "SELECT size_bytes FROM file WHERE name = 'report.txt'"
    ).fetchone()
    assert row["size_bytes"] == target.stat().st_size


def test_exclusions_are_counted_and_skipped(
    catalog: Catalog, sample_tree: Path
) -> None:
    junk = sample_tree / "$RECYCLE.BIN"
    junk.mkdir()
    (junk / "deleted.bin").write_bytes(b"x" * 100)

    result = scan(catalog, sample_tree)
    assert result["counters"].excluded >= 1
    assert not list(catalog.find("%deleted.bin%"))


def test_links_are_not_traversed(catalog: Catalog, sample_tree: Path) -> None:
    if not (sample_tree / "docs-link").is_symlink():
        pytest.skip("symlink creation not permitted here")

    result = scan(catalog, sample_tree)
    assert result["counters"].reparse_skipped >= 1

    # report.txt exists once under docs, not a second time under the link.
    matches = list(catalog.find("%report.txt%"))
    assert len(matches) == 1


def test_rollups_match_the_tree(catalog: Catalog, sample_tree: Path) -> None:
    result = scan(catalog, sample_tree)
    root_dir = catalog.conn.execute(
        "SELECT id FROM dir WHERE parent_id IS NULL"
    ).fetchone()["id"]
    rollup = catalog.conn.execute(
        "SELECT * FROM dir_rollup WHERE dir_id = ?", (root_dir,)
    ).fetchone()

    assert rollup["subtree_files"] == result["counters"].files
    assert rollup["subtree_bytes"] == result["counters"].bytes


def test_preview_agrees_with_the_scan(catalog: Catalog, sample_tree: Path) -> None:
    """A dry run that disagrees with the real scan is worse than none."""
    excludes = ExclusionSet.for_root(())
    preview = preview_root(str(sample_tree), excludes)
    result = scan(catalog, sample_tree, excludes)

    assert preview.files == result["counters"].files
    assert preview.bytes == result["counters"].bytes
    assert preview.dirs == result["counters"].dirs


def test_preview_writes_nothing(tmp_path: Path, sample_tree: Path) -> None:
    catalog_path = tmp_path / "catalog" / "catalog.db"
    preview_root(str(sample_tree))
    assert not catalog_path.exists()


def test_scan_records_coverage(catalog: Catalog, sample_tree: Path) -> None:
    result = scan(catalog, sample_tree)
    assert "files read" in result["coverage"]

    row = catalog.conn.execute("SELECT * FROM scan ORDER BY id DESC").fetchone()
    assert row["status"] == "done"
    assert row["files_seen"] == result["counters"].files
    assert row["method"] == "walk"


def test_dehydrated_files_are_catalogued_but_not_opened(
    catalog: Catalog, sample_tree: Path, monkeypatch
) -> None:
    """Cloud placeholders belong in the catalog; opening them does not.

    The marker is injected through the lister rather than by patching the
    gateway, because which lister the scan uses is platform-dependent: Windows
    reads directories through the Win32 batch call, so patching
    ``fsio.scandir`` would have quietly tested nothing there.
    """
    from drivefusion.core import fsio
    from drivefusion.core.enum import batchwalk
    from drivefusion.core.enum.records import DirEntry

    def marked_lister(path: str) -> list[DirEntry]:
        out = []
        for entry in batchwalk.fsio_lister(path):
            if entry.name == "report.txt":
                entry = DirEntry(
                    name=entry.name,
                    attributes=fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                    size=entry.size, alloc_size=entry.alloc_size,
                    mtime_ns=entry.mtime_ns, ctime_ns=entry.ctime_ns,
                    file_id=entry.file_id,
                )
            out.append(entry)
        return out

    monkeypatch.setattr(
        fsio, "open_read", lambda *a, **k: pytest.fail("scan opened a file")
    )

    result = scan(catalog, sample_tree, lister=marked_lister)
    assert result["counters"].dehydrated == 1

    row = catalog.conn.execute(
        "SELECT read_state FROM file WHERE name = 'report.txt'"
    ).fetchone()
    assert row["read_state"] == "skipped-dehydrated"
