"""Catalog schema, interning, merge, and rollup behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.store import schema
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "catalog.db") as cat:
        yield cat


@pytest.fixture
def volume_id(catalog: Catalog) -> int:
    return catalog.upsert_volume(volume_guid="test:1", label="TEST", fs_type="exfat")


def stage_row(scan_id, volume_id, dir_id, name, size=10, mtime=1):
    return (
        scan_id, volume_id, dir_id, name, "txt", size, None, mtime, mtime,
        None, 1, 0, "ok",
    )


# -- schema -------------------------------------------------------------------

def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "catalog.db"
    with Catalog(path):
        pass
    with Catalog(path) as second:
        assert schema.current_version(second.conn) == schema.SCHEMA_VERSION
        rows = second.conn.execute("SELECT COUNT(*) AS n FROM schema_version")
        assert rows.fetchone()["n"] == 1


def test_wal_and_page_size_applied(catalog: Catalog) -> None:
    mode = catalog.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    assert catalog.conn.execute("PRAGMA page_size").fetchone()[0] == (
        schema.INITIAL_PAGE_SIZE
    )


def test_future_schema_is_refused(tmp_path: Path) -> None:
    """A newer catalog must not be silently downgraded."""
    path = tmp_path / "catalog.db"
    with Catalog(path) as cat:
        cat.conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (schema.SCHEMA_VERSION + 5, schema.utcnow()),
        )
    with pytest.raises(schema.SchemaError):
        Catalog(path)


# -- directory interning ------------------------------------------------------

def test_root_dirs_are_not_duplicated_across_scans(
    catalog: Catalog, volume_id: int
) -> None:
    """Regression: NULL parent_id does not conflict in a SQLite UNIQUE index.

    Every NULL is distinct there, so an upsert keyed on
    (volume_id, parent_id, name) silently inserts a new root on every scan —
    and with it a whole duplicate subtree and duplicate file rows.
    """
    first = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=None,
        name="/data", depth=0, scan_id=1,
    )
    second = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=None,
        name="/data", depth=0, scan_id=2,
    )
    assert first == second

    count = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM dir WHERE parent_id IS NULL"
    ).fetchone()["n"]
    assert count == 1


def test_child_dirs_are_stable(catalog: Catalog, volume_id: int) -> None:
    root = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=None,
        name="/data", depth=0, scan_id=1,
    )
    first = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=root,
        name="docs", depth=1, scan_id=1,
    )
    second = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=root,
        name="docs", depth=1, scan_id=2,
    )
    assert first == second


def test_dir_path_reconstruction(catalog: Catalog, volume_id: int) -> None:
    root = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=None,
        name="/data", depth=0, scan_id=1,
    )
    mid = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=root,
        name="docs", depth=1, scan_id=1,
    )
    leaf = catalog.intern_dir(
        volume_id=volume_id, root_id=None, parent_id=mid,
        name="2024", depth=2, scan_id=1,
    )
    assert catalog.dir_path(leaf) == "/data/docs/2024"


# -- scope roots --------------------------------------------------------------

def test_removing_a_root_keeps_its_catalogued_history(
    catalog: Catalog, volume_id: int
) -> None:
    """Removal means "stop looking here", not "forget what you saw"."""
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_id = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_id,
    )
    catalog.stage_files([stage_row(scan_id, volume_id, dir_id, "a.txt")])
    catalog.merge_scan(scan_id, volume_id, root_id)

    assert catalog.remove_scope_root(root_id) is True
    assert catalog.scope_roots() == []
    assert catalog.counts()["files"] == 1
    assert len(list(catalog.find("%a.txt%"))) == 1


def test_re_adding_a_removed_root_revives_it(
    catalog: Catalog, volume_id: int
) -> None:
    original = catalog.add_scope_root(volume_id, "/data")
    catalog.remove_scope_root(original)
    revived = catalog.add_scope_root(volume_id, "/data", ["*.iso"])

    assert revived == original
    roots = catalog.scope_roots()
    assert len(roots) == 1 and roots[0].excludes == ("*.iso",)


def test_removing_an_unknown_root_reports_failure(catalog: Catalog) -> None:
    assert catalog.remove_scope_root(999) is False


# -- merge --------------------------------------------------------------------

def test_merge_inserts_then_updates(catalog: Catalog, volume_id: int) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_one = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_one,
    )
    catalog.stage_files([stage_row(scan_one, volume_id, dir_id, "a.txt", size=10)])
    first = catalog.merge_scan(scan_one, volume_id, root_id)
    assert first == {"staged": 1, "inserted": 1, "updated": 0, "vanished": 0}

    scan_two = catalog.begin_scan(volume_id, root_id, "walk", False)
    catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_two,
    )
    catalog.stage_files(
        [stage_row(scan_two, volume_id, dir_id, "a.txt", size=99, mtime=2)]
    )
    second = catalog.merge_scan(scan_two, volume_id, root_id)
    assert second == {"staged": 1, "inserted": 0, "updated": 1, "vanished": 0}

    row = catalog.conn.execute("SELECT size_bytes FROM file").fetchone()
    assert row["size_bytes"] == 99
    assert catalog.counts()["files"] == 1


def test_merge_tombstones_missing_files(catalog: Catalog, volume_id: int) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_one = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_one,
    )
    catalog.stage_files(
        [
            stage_row(scan_one, volume_id, dir_id, "keep.txt"),
            stage_row(scan_one, volume_id, dir_id, "gone.txt"),
        ]
    )
    catalog.merge_scan(scan_one, volume_id, root_id)

    scan_two = catalog.begin_scan(volume_id, root_id, "walk", False)
    catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_two,
    )
    catalog.stage_files([stage_row(scan_two, volume_id, dir_id, "keep.txt")])
    result = catalog.merge_scan(scan_two, volume_id, root_id)

    assert result["vanished"] == 1
    counts = catalog.counts()
    assert counts["files"] == 1 and counts["vanished"] == 1

    # The metadata survives the bytes: FAIR A2, and the basis for reporting
    # what was lost when a drive fails.
    row = catalog.conn.execute(
        "SELECT name, vanished_at FROM file WHERE vanished_at IS NOT NULL"
    ).fetchone()
    assert row["name"] == "gone.txt" and row["vanished_at"]


def test_tombstoning_is_scoped_to_the_root_that_was_scanned(
    catalog: Catalog, volume_id: int
) -> None:
    """Scanning D:\\Research must not declare everything in D:\\Games gone."""
    research = catalog.add_scope_root(volume_id, "/research")
    games = catalog.add_scope_root(volume_id, "/games")

    for root_id, name in ((research, "/research"), (games, "/games")):
        scan_id = catalog.begin_scan(volume_id, root_id, "walk", False)
        dir_id = catalog.intern_dir(
            volume_id=volume_id, root_id=root_id, parent_id=None,
            name=name, depth=0, scan_id=scan_id,
        )
        catalog.stage_files([stage_row(scan_id, volume_id, dir_id, "f.txt")])
        catalog.merge_scan(scan_id, volume_id, root_id)

    # Rescan only /research, finding nothing.
    scan_id = catalog.begin_scan(volume_id, research, "walk", False)
    catalog.intern_dir(
        volume_id=volume_id, root_id=research, parent_id=None,
        name="/research", depth=0, scan_id=scan_id,
    )
    result = catalog.merge_scan(scan_id, volume_id, research)

    assert result["vanished"] == 1
    assert catalog.counts()["files"] == 1  # the /games file is untouched


def test_content_identity_is_invalidated_when_bytes_may_have_changed(
    catalog: Catalog, volume_id: int
) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_one = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_one,
    )
    catalog.stage_files([stage_row(scan_one, volume_id, dir_id, "a.txt", 10, 1)])
    catalog.merge_scan(scan_one, volume_id, root_id)

    content_id = catalog.conn.execute(
        "INSERT INTO content (size_bytes) VALUES (10) RETURNING id"
    ).fetchone()["id"]
    catalog.conn.execute("UPDATE file SET content_id = ?", (content_id,))

    # Unchanged size and mtime: identity is kept.
    scan_two = catalog.begin_scan(volume_id, root_id, "walk", False)
    catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_two,
    )
    catalog.stage_files([stage_row(scan_two, volume_id, dir_id, "a.txt", 10, 1)])
    catalog.merge_scan(scan_two, volume_id, root_id)
    assert catalog.conn.execute("SELECT content_id FROM file").fetchone()[0] == (
        content_id
    )

    # Changed mtime: identity is dropped so M3 re-hashes rather than trusting
    # a hash for bytes that may no longer be those bytes.
    scan_three = catalog.begin_scan(volume_id, root_id, "walk", False)
    catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_three,
    )
    catalog.stage_files([stage_row(scan_three, volume_id, dir_id, "a.txt", 10, 2)])
    catalog.merge_scan(scan_three, volume_id, root_id)
    assert catalog.conn.execute("SELECT content_id FROM file").fetchone()[0] is None


# -- rollups ------------------------------------------------------------------

def test_rollups_aggregate_up_the_tree(catalog: Catalog, volume_id: int) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_id = catalog.begin_scan(volume_id, root_id, "walk", False)
    root = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_id,
    )
    child = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=root,
        name="docs", depth=1, scan_id=scan_id,
    )
    grandchild = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=child,
        name="2024", depth=2, scan_id=scan_id,
    )
    catalog.stage_files(
        [
            stage_row(scan_id, volume_id, root, "top.txt", size=100),
            stage_row(scan_id, volume_id, child, "mid.txt", size=200),
            stage_row(scan_id, volume_id, grandchild, "deep.txt", size=300),
        ]
    )
    catalog.merge_scan(scan_id, volume_id, root_id)
    catalog.rebuild_rollups(volume_id)

    rows = {
        r["dir_id"]: r
        for r in catalog.conn.execute("SELECT * FROM dir_rollup")
    }
    assert rows[grandchild]["subtree_bytes"] == 300
    assert rows[child]["subtree_bytes"] == 500
    assert rows[root]["subtree_bytes"] == 600
    assert rows[root]["subtree_files"] == 3
    assert rows[root]["files"] == 1  # direct children only


# -- search -------------------------------------------------------------------

def test_find_pages_by_keyset(catalog: Catalog, volume_id: int) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_id = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_id,
    )
    catalog.stage_files(
        stage_row(scan_id, volume_id, dir_id, f"file-{i:03d}.txt")
        for i in range(25)
    )
    catalog.merge_scan(scan_id, volume_id, root_id)

    first = list(catalog.find("%", limit=10))
    assert len(first) == 10
    second = list(catalog.find("%", limit=10, after_id=first[-1]["id"]))
    assert len(second) == 10
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert first[0]["path"] == "/data"


def test_find_excludes_vanished_by_default(catalog: Catalog, volume_id: int) -> None:
    root_id = catalog.add_scope_root(volume_id, "/data")
    scan_id = catalog.begin_scan(volume_id, root_id, "walk", False)
    dir_id = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/data", depth=0, scan_id=scan_id,
    )
    catalog.stage_files([stage_row(scan_id, volume_id, dir_id, "a.txt")])
    catalog.merge_scan(scan_id, volume_id, root_id)
    catalog.conn.execute("UPDATE file SET vanished_at = '2026-01-01'")

    assert list(catalog.find("%a.txt%")) == []
    assert len(list(catalog.find("%a.txt%", include_vanished=True))) == 1
