"""Queries that back the catalog browser.

Kept out of the GUI package on purpose. A browser row shows how many copies of
a file exist, and that number must be computed by *the same rule* the reports
use — a GUI that counted copies its own way would eventually disagree with
``drivefusion report``, and one of the two would be lying to the user. So the
drive-counting expression is imported from :mod:`duplicates` rather than
rewritten here.

Every query is shaped for keyset pagination over an index (§11): a directory
listing is served by ``ux_file_dir_name(dir_id, name)``, so paging is a seek
rather than a scan, and stays that way at 50M rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from drivefusion.core.analysis.duplicates import DRIVE_KEY
from drivefusion.core.store.catalog import Catalog

#: Rows per page. Enough to fill any realistic viewport in one fetch, small
#: enough that a fetch is imperceptible.
PAGE_ROWS = 200


@dataclass(frozen=True)
class DirRow:
    id: int
    name: str
    volume_id: int
    subtree_files: int
    subtree_bytes: int
    has_children: bool


@dataclass(frozen=True)
class FileRow:
    id: int
    name: str
    size_bytes: int
    content_id: int | None
    #: Distinct physical drives holding this content, or ``None`` when the file
    #: has no content identity yet. ``None`` means *unknown*, and the view must
    #: render it as unknown: showing "1" for an unhashed file would be a
    #: confident claim that no copy exists elsewhere.
    drives: int | None

    @property
    def copies_known(self) -> bool:
        return self.drives is not None


def child_dirs(
    catalog: Catalog, parent_id: int | None, *, volume_id: int | None = None
) -> list[DirRow]:
    """Directories directly beneath ``parent_id`` (``None`` for scope roots).

    ``parent_id IS ?`` rather than ``= ?``: SQLite's ``=`` never matches NULL,
    so the root query would silently return nothing — the same trap that
    duplicated whole volumes in M1 and split content rows in M3.
    """
    sql = (
        "SELECT d.id, d.name, d.volume_id, "
        "  COALESCE(r.subtree_files, 0) AS subtree_files, "
        "  COALESCE(r.subtree_bytes, 0) AS subtree_bytes, "
        "  EXISTS(SELECT 1 FROM dir c "
        "         WHERE c.parent_id = d.id AND c.vanished_at IS NULL) AS has_children "
        "FROM dir d LEFT JOIN dir_rollup r ON r.dir_id = d.id "
        "WHERE d.parent_id IS ? AND d.vanished_at IS NULL"
    )
    params: list = [parent_id]
    if volume_id is not None:
        sql += " AND d.volume_id = ?"
        params.append(volume_id)
    sql += " ORDER BY d.name"

    return [
        DirRow(
            id=int(row["id"]),
            name=row["name"],
            volume_id=int(row["volume_id"]),
            subtree_files=int(row["subtree_files"]),
            subtree_bytes=int(row["subtree_bytes"]),
            has_children=bool(row["has_children"]),
        )
        for row in catalog.conn.execute(sql, params)
    ]


def files_in_dir(
    catalog: Catalog,
    dir_id: int,
    *,
    after_name: str | None = None,
    limit: int = PAGE_ROWS,
) -> list[FileRow]:
    """One page of a directory's files, ordered by name.

    ``(dir_id, name)`` is unique, so the last name on a page is an unambiguous
    cursor: the next page can neither repeat a row nor skip one. An ordering
    that were not unique would do both, intermittently, and a browser that
    quietly drops files is worse than one that fails.
    """
    sql = (
        "SELECT f.id, f.name, f.size_bytes, f.content_id, "
        "  CASE WHEN f.content_id IS NULL THEN NULL ELSE ("
        f"    SELECT COUNT(DISTINCT {DRIVE_KEY}) FROM file f2 "
        "     JOIN volume v ON v.id = f2.volume_id "
        "     WHERE f2.content_id = f.content_id AND f2.vanished_at IS NULL"
        "  ) END AS drives "
        "FROM file f "
        "WHERE f.dir_id = ? AND f.vanished_at IS NULL AND f.name > ? "
        "ORDER BY f.name LIMIT ?"
    )
    rows = catalog.conn.execute(sql, (dir_id, after_name or "", limit))
    return [
        FileRow(
            id=int(row["id"]),
            name=row["name"],
            size_bytes=int(row["size_bytes"]),
            content_id=None if row["content_id"] is None else int(row["content_id"]),
            drives=None if row["drives"] is None else int(row["drives"]),
        )
        for row in rows
    ]


def count_files_in_dir(catalog: Catalog, dir_id: int) -> int:
    """Exact file count for one directory.

    Safe to call because it is bounded by an index range, unlike a ``COUNT(*)``
    over the whole table — which is why the views ask for this and never for
    that (§11).
    """
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file WHERE dir_id = ? AND vanished_at IS NULL",
        (dir_id,),
    ).fetchone()
    return int(row["n"])


def dir_summary(catalog: Catalog, dir_id: int) -> dict:
    """Subtree totals, served from ``dir_rollup`` rather than recomputed."""
    row = catalog.conn.execute(
        "SELECT d.name, d.volume_id, d.depth, "
        "  COALESCE(r.files, 0) AS files, COALESCE(r.bytes, 0) AS bytes, "
        "  COALESCE(r.subtree_files, 0) AS subtree_files, "
        "  COALESCE(r.subtree_bytes, 0) AS subtree_bytes "
        "FROM dir d LEFT JOIN dir_rollup r ON r.dir_id = d.id WHERE d.id = ?",
        (dir_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "name": row["name"],
        "volume_id": int(row["volume_id"]),
        "depth": int(row["depth"]),
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
        "subtree_files": int(row["subtree_files"]),
        "subtree_bytes": int(row["subtree_bytes"]),
    }


def drive_inventory(catalog: Catalog) -> list[dict]:
    """Physical drives with their volumes and what has been catalogued on them.

    Volumes with no identified drive are reported under their own entry rather
    than being dropped or merged, so the inventory adds up to what was scanned.
    """
    rows = catalog.conn.execute(
        "SELECT v.id AS volume_id, v.volume_guid, v.label, v.fs_type, "
        "       v.capacity_bytes, v.free_bytes, v.cluster_bytes, v.rescan_cost, "
        "       v.last_seen_at, v.supports_usn, "
        "       d.id AS drive_id, d.serial, d.model, d.manufacturer, d.bus, "
        "       d.media, d.purchase_date, d.nickname, d.location, d.role, "
        "       d.capacity_bytes AS drive_capacity_bytes, "
        "       (SELECT COUNT(*) FROM file f "
        "         WHERE f.volume_id = v.id AND f.vanished_at IS NULL) AS files, "
        "       (SELECT COALESCE(SUM(f.size_bytes), 0) FROM file f "
        "         WHERE f.volume_id = v.id AND f.vanished_at IS NULL) AS bytes "
        "FROM volume v LEFT JOIN drive d ON d.id = v.drive_id "
        "ORDER BY COALESCE(d.nickname, d.model, v.label, v.volume_guid)"
    )
    return [dict(row) for row in rows]
