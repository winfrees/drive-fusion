"""Model behaviour at scale: paging, laziness, and counts that are honest.

These are the assertions behind §11's "non-negotiable at 50M rows". A paging bug
does not raise — it shows a directory with files missing from it, which to the
user is indistinguishable from the files not existing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from drivefusion.core.analysis.browse import PAGE_ROWS, files_in_dir
from drivefusion.core.identity import hash_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

pytestmark = pytest.mark.usefixtures("qt_app")


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def build(catalog: Catalog, root: Path, *, volume_id: int | None = None) -> int:
    if volume_id is None:
        volume_id = catalog.upsert_volume(volume_guid=f"gui:{root.name}", fs_type="ntfs")
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)
    return volume_id


def many_files(root: Path, count: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        # Zero-padded so lexical order is stable and predictable.
        (root / f"file-{index:05d}.bin").write_bytes(b"x" * (index % 7 + 1))


def only_dir(catalog: Catalog) -> int:
    return int(catalog.conn.execute("SELECT dir_id FROM file LIMIT 1").fetchone()["dir_id"])


# -- keyset paging ------------------------------------------------------------

def test_paging_returns_every_file_exactly_once(catalog: Catalog, tmp_path: Path) -> None:
    """The assertion that matters: no row skipped, no row repeated.

    A cursor over a non-unique ordering fails exactly this way, and only once the
    data is big enough to page — which is to say, never in a small test and
    always in production.
    """
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    total = PAGE_ROWS * 2 + 37
    many_files(root, total)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))
    model.load_all()

    names = [model.row_at(i).name for i in range(model.rowCount())]
    assert len(names) == total, "paging lost or invented rows"
    assert len(set(names)) == total, "a row was returned on two pages"
    assert names == sorted(names), "pages arrived out of order"


def test_first_page_does_not_load_the_whole_directory(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Opening a folder must cost one page, whatever is in it."""
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    many_files(root, PAGE_ROWS * 3)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))

    assert model.rowCount() == PAGE_ROWS
    assert model.canFetchMore()


def test_row_count_never_triggers_a_full_table_count(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Qt calls rowCount on every repaint; it must not touch the database.

    Traced at the connection, so this states a fact about the SQL issued rather
    than a reading of the code.
    """
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    many_files(root, PAGE_ROWS + 10)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))

    statements: list[str] = []
    catalog.conn.set_trace_callback(statements.append)
    try:
        for _ in range(50):
            model.rowCount()
            model.columnCount()
    finally:
        catalog.conn.set_trace_callback(None)

    assert statements == [], f"rowCount hit the database: {statements[:3]}"


def test_paging_uses_a_seek_not_an_offset(catalog: Catalog, tmp_path: Path) -> None:
    """OFFSET degrades linearly, so its absence is part of the contract."""
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    many_files(root, PAGE_ROWS * 2)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))

    statements: list[str] = []
    catalog.conn.set_trace_callback(statements.append)
    try:
        model.fetchMore()
    finally:
        catalog.conn.set_trace_callback(None)

    issued = " ".join(statements)
    assert "OFFSET" not in issued.upper()

    # The cursor column and the sort column must be the same one. Seeking on
    # `name` while ordering by something else still looks like keyset paging and
    # still silently drops rows — this assertion exists because an earlier
    # version of this test passed with exactly that mismatch injected. The trace
    # callback reports statements with parameters already bound, so the cursor
    # value appears as a literal rather than a placeholder.
    cursor_column = re.search(r"AND f\.(\w+) > ", issued)
    order_column = re.search(r"ORDER BY f\.(\w+)", issued)
    assert cursor_column and order_column, issued
    assert cursor_column.group(1) == order_column.group(1), (
        f"paging seeks on {cursor_column.group(1)} but orders by "
        f"{order_column.group(1)}"
    )


def test_the_directory_listing_query_is_index_driven(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A plan that scans the file table would be fine now and fatal at 50M."""
    root = tmp_path / "vol"
    many_files(root, 20)
    build(catalog, root)

    plan = " ".join(
        str(row[3])
        for row in catalog.conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT f.id, f.name FROM file f "
            "WHERE f.dir_id = ? AND f.vanished_at IS NULL AND f.name > ? "
            "ORDER BY f.name LIMIT 200",
            (only_dir(catalog), ""),
        )
    )
    assert "ux_file_dir_name" in plan, plan
    assert "SCAN file" not in plan, plan


def test_exhausted_only_after_a_short_or_empty_page(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A directory holding exactly one page must still confirm the end."""
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    many_files(root, PAGE_ROWS)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))

    assert model.rowCount() == PAGE_ROWS
    assert model.canFetchMore(), "a full page is not proof of the end"

    model.fetchMore()
    assert model.rowCount() == PAGE_ROWS
    assert not model.canFetchMore()


def test_empty_directory_is_not_an_error(catalog: Catalog, tmp_path: Path) -> None:
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    (root / "empty").mkdir(parents=True)
    (root / "keep.bin").write_bytes(b"x")
    build(catalog, root)

    empty_id = catalog.conn.execute(
        "SELECT id FROM dir WHERE name = 'empty'"
    ).fetchone()["id"]

    model = FileTableModel.over(catalog)
    model.set_directory(int(empty_id))
    assert model.rowCount() == 0
    assert not model.canFetchMore()


# -- what the cells say -------------------------------------------------------

def test_unhashed_files_show_copies_as_unknown(catalog: Catalog, tmp_path: Path) -> None:
    """"Unknown" and "1" are different claims, and only one of them is true."""
    from PySide6.QtCore import Qt

    from drivefusion.gui.models import FileTableModel
    from drivefusion.gui.models.filetable import COPIES, UNKNOWN

    root = tmp_path / "vol"
    root.mkdir()
    (root / "solo.bin").write_bytes(b"unique bytes here")
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))
    assert model.data(model.index(0, COPIES), Qt.DisplayRole) == UNKNOWN

    tooltip = model.data(model.index(0, COPIES), Qt.ToolTipRole)
    assert "not zero" in tooltip, "the tooltip must not let 'unknown' read as 'none'"


def test_copies_column_counts_drives_after_hashing(
    catalog: Catalog, tmp_path: Path
) -> None:
    from PySide6.QtCore import Qt

    from drivefusion.gui.models import FileTableModel
    from drivefusion.gui.models.filetable import COPIES

    payload = b"shared across two drives" * 4
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "x.bin").write_bytes(payload)
    (second / "x.bin").write_bytes(payload)

    for name, path in (("A", first), ("B", second)):
        drive_id = catalog.upsert_drive(serial=f"SER-{name}")
        volume_id = catalog.upsert_volume(volume_guid=f"vol:{name}", drive_id=drive_id)
        build(catalog, path, volume_id=volume_id)
    hash_pass(catalog)

    dir_id = int(
        catalog.conn.execute(
            "SELECT dir_id FROM file WHERE name = 'x.bin' LIMIT 1"
        ).fetchone()["dir_id"]
    )
    model = FileTableModel.over(catalog)
    model.set_directory(dir_id)
    assert model.data(model.index(0, COPIES), Qt.DisplayRole) == "2"


def test_two_paths_on_one_drive_count_as_one_copy_in_the_browser(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The browser must use the same counting rule as the reports.

    If it counted paths, a file duplicated twice on a single drive would show
    "2 copies" and imply a durability the user does not have.
    """
    from PySide6.QtCore import Qt

    from drivefusion.gui.models import FileTableModel
    from drivefusion.gui.models.filetable import COPIES

    root = tmp_path / "one"
    (root / "sub").mkdir(parents=True)
    payload = b"same content twice on one drive" * 3
    (root / "a.bin").write_bytes(payload)
    (root / "sub" / "b.bin").write_bytes(payload)

    drive_id = catalog.upsert_drive(serial="SER-ONE")
    volume_id = catalog.upsert_volume(volume_guid="vol:one", drive_id=drive_id)
    build(catalog, root, volume_id=volume_id)
    hash_pass(catalog)

    dir_id = int(
        catalog.conn.execute(
            "SELECT dir_id FROM file WHERE name = 'a.bin'"
        ).fetchone()["dir_id"]
    )
    model = FileTableModel.over(catalog)
    model.set_directory(dir_id)
    assert model.data(model.index(0, COPIES), Qt.DisplayRole) == "1"


def test_status_text_admits_it_is_still_counting(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A partial table must never print a total as though it were the whole."""
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    many_files(root, PAGE_ROWS * 2)
    build(catalog, root)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))
    assert "counting" in model.status_text()

    model.set_total(PAGE_ROWS * 2)
    assert model.status_text() == f"{PAGE_ROWS:,} of {PAGE_ROWS * 2:,} files"

    model.load_all()
    assert model.status_text() == f"{PAGE_ROWS * 2:,} files"


# -- the tree -----------------------------------------------------------------

def test_tree_does_not_read_children_until_expanded(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The arrow comes from EXISTS; loading a subtree to draw one is the bug."""
    from drivefusion.core.analysis.browse import child_dirs
    from drivefusion.gui.models import DirTreeModel

    root = tmp_path / "vol"
    (root / "deep" / "deeper").mkdir(parents=True)
    (root / "deep" / "deeper" / "f.bin").write_bytes(b"x")
    build(catalog, root)

    fetched: list[int | None] = []

    def counting_fetch(parent_id):
        fetched.append(parent_id)
        return child_dirs(catalog, parent_id)

    model = DirTreeModel(counting_fetch)
    assert model.rowCount() == 1                 # the scope root
    top = model.index(0, 0)
    assert model.hasChildren(top)
    assert fetched == [None], "hasChildren loaded the children"

    model.rowCount(top)
    assert fetched == [None, model.dir_id_at(top)]


def test_tree_reports_subtree_sizes_from_the_rollup(
    catalog: Catalog, tmp_path: Path
) -> None:
    from PySide6.QtCore import Qt

    from drivefusion.gui.models import DirTreeModel
    from drivefusion.gui.models.dirtree import SUBTREE_FILES

    root = tmp_path / "vol"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.bin").write_bytes(b"x" * 10)
    (root / "sub" / "b.bin").write_bytes(b"x" * 20)
    (root / "top.bin").write_bytes(b"x" * 5)
    build(catalog, root)

    model = DirTreeModel.over(catalog)
    assert model.data(model.index(0, SUBTREE_FILES), Qt.DisplayRole) == "3"


def test_tree_root_query_handles_null_parents(catalog: Catalog, tmp_path: Path) -> None:
    """`parent_id = NULL` matches nothing in SQLite; `IS NULL` is required.

    The same trap has bitten this schema three times, so the browser's root query
    gets its own assertion rather than trusting a reviewer to notice.
    """
    from drivefusion.core.analysis.browse import child_dirs

    root = tmp_path / "vol"
    (root / "child").mkdir(parents=True)
    (root / "child" / "f.bin").write_bytes(b"x")
    build(catalog, root)

    roots = child_dirs(catalog, None)
    assert len(roots) == 1, "root directories were not found"
    assert roots[0].has_children


def test_vanished_entries_are_not_browsable(catalog: Catalog, tmp_path: Path) -> None:
    """Tombstoned rows are history, not contents."""
    from drivefusion.gui.models import FileTableModel

    root = tmp_path / "vol"
    root.mkdir()
    (root / "stays.bin").write_bytes(b"x")
    (root / "goes.bin").write_bytes(b"y")
    volume_id = build(catalog, root)

    (root / "goes.bin").unlink()
    root_id = catalog.scope_roots()[0].id
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)

    model = FileTableModel.over(catalog)
    model.set_directory(only_dir(catalog))
    names = [model.row_at(i).name for i in range(model.rowCount())]
    assert names == ["stays.bin"]


def test_browse_page_boundaries_line_up(catalog: Catalog, tmp_path: Path) -> None:
    root = tmp_path / "vol"
    many_files(root, 30)
    build(catalog, root)

    page = files_in_dir(catalog, only_dir(catalog), limit=10)
    assert len(page) == 10
    following = files_in_dir(
        catalog, only_dir(catalog), after_name=page[-1].name, limit=10
    )
    assert following[0].name > page[-1].name
