"""Placement units, and what a unit actually costs on a destination.

**Units, not files.** Solving per-file over 50M files is intractable and, worse,
unreviewable — nobody approves fifty million decisions. The planner works over a
few thousand units: a confirmed collection, or a directory subtree taken at the
depth where its size falls under a threshold. Units are what a human can
approve, and they keep related files together.

**`footprint(unit, destination)` is deliberately not `size`.** This is the part
of the planner most able to produce a document that lies. A unit's cost on a
destination depends on that volume's cluster size: a million 4 KB files on an
exFAT volume with 128 KiB clusters occupy 128 GiB, not 4 GiB — thirty-two times
the logical total. Estimating from logical bytes produces plans that overflow
the destination partway through a copy, which is the failure most likely to make
someone stop trusting the tool, and it only appears on the exFAT half of the
fleet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from drivefusion.core.analysis.duplicates import DRIVE_KEY
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.store.catalog import Catalog

#: Subtree size at or below which a directory becomes one unit rather than
#: being split further. Chosen so a 20-drive, 50M-file archive yields a few
#: thousand units: enough to place well, few enough to read.
UNIT_TARGET_BYTES = 20 * 1024 * 1024 * 1024

#: Depth past which subtrees are taken whole regardless of size, so a pathological
#: tree cannot produce a unit per directory.
MAX_UNIT_DEPTH = 8

#: Assumed when a volume does not report its cluster size. 4 KiB is the NTFS
#: default; using 1 would silently turn the footprint calculation back into a
#: logical-byte estimate, which is the bug this module exists to prevent.
DEFAULT_CLUSTER_BYTES = 4096


@dataclass(frozen=True)
class Unit:
    """Something the planner places as a whole."""

    key: str
    label: str
    files: int
    bytes: int
    #: Drive keys already holding it, as the copy-counting rule produces them
    #: (negative for a volume with no identified physical drive).
    drive_keys: tuple[int, ...]
    collection_id: int | None = None
    dir_id: int | None = None
    #: Required copies and zones, from the collection's NDSA target.
    ndsa_target: int | None = None
    #: File sizes, kept only as a histogram: the footprint calculation needs the
    #: distribution, and holding 50M individual sizes would defeat the point of
    #: working in units.
    size_histogram: tuple[tuple[int, int], ...] = field(default_factory=tuple)

    @property
    def copies(self) -> int:
        return len(self.drive_keys)


def _histogram(catalog: Catalog, where: str, params: list) -> tuple[tuple[int, int], ...]:
    """File sizes bucketed by power of two, with counts.

    A histogram rather than a list: the footprint of a unit on a destination is
    a sum over files, but it only needs the distribution of sizes, and buckets
    give the exact answer when every file in a bucket rounds to the same number
    of clusters. Where a bucket straddles a cluster boundary the midpoint is
    used, which is why :func:`footprint` is documented as an estimate for mixed
    buckets and exact for the common case.
    """
    rows = catalog.conn.execute(
        "SELECT CAST(size_bytes AS INTEGER) AS size, COUNT(*) AS n FROM file f "
        f"WHERE f.vanished_at IS NULL AND {where} "
        "GROUP BY size_bytes ORDER BY size_bytes",
        params,
    )
    return tuple((int(row["size"]), int(row["n"])) for row in rows)


def footprint(unit: Unit, cluster_bytes: int | None) -> int:
    """Bytes a unit occupies on a volume with this cluster size.

    Exact: every file is rounded up to a whole number of clusters, because that
    is what a filesystem does. A zero-length file still occupies a directory
    entry but no cluster, so it contributes nothing here.
    """
    cluster = int(cluster_bytes or DEFAULT_CLUSTER_BYTES)
    if cluster <= 1:
        cluster = DEFAULT_CLUSTER_BYTES

    total = 0
    for size, count in unit.size_histogram:
        if size <= 0:
            continue
        clusters = -(-size // cluster)      # ceiling division
        total += clusters * cluster * count
    return total


def slack(unit: Unit, cluster_bytes: int | None) -> int:
    """How much of the footprint is cluster slack rather than data.

    Reported so "this unit needs 128 GB on that drive but is 4 GB of data" is
    an explanation rather than a surprise.
    """
    return max(0, footprint(unit, cluster_bytes) - unit.bytes)


def _drive_keys(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    return tuple(sorted(int(part) for part in text.split(",") if part.strip()))


def collection_units(catalog: Catalog) -> list[Unit]:
    """One unit per confirmed collection."""
    units: list[Unit] = []
    for collection in collections_module.all_collections(catalog):
        where, params = collections_module.member_file_filter(catalog, collection.id)
        row = catalog.conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
            f"  GROUP_CONCAT(DISTINCT {DRIVE_KEY}) AS drive_keys "
            "FROM file f JOIN volume v ON v.id = f.volume_id "
            f"WHERE f.vanished_at IS NULL AND {where}",
            params,
        ).fetchone()

        if not row or not int(row["files"]):
            continue
        units.append(
            Unit(
                key=f"collection:{collection.id}",
                label=collection.title,
                files=int(row["files"]),
                bytes=int(row["bytes"]),
                drive_keys=_drive_keys(row["drive_keys"]),
                collection_id=collection.id,
                ndsa_target=collection.ndsa_target,
                size_histogram=_histogram(catalog, where, params),
            )
        )
    return units


def subtree_units(
    catalog: Catalog,
    *,
    target_bytes: int = UNIT_TARGET_BYTES,
    max_depth: int = MAX_UNIT_DEPTH,
    exclude_dirs: set[int] | None = None,
) -> list[Unit]:
    """Units from directory subtrees, for content in no collection.

    §8.4: the planner does not wait for curation. Walking ``dir_rollup`` down to
    the depth where a subtree fits the target gives usable units on day one, and
    confirmed collections simply make them better.
    """
    exclude_dirs = exclude_dirs or set()
    units: list[Unit] = []
    visited: set[int] = set()

    def walk(dir_id: int | None, depth: int) -> None:
        rows = catalog.conn.execute(
            "SELECT d.id, d.name, COALESCE(r.subtree_bytes, 0) AS bytes, "
            "       COALESCE(r.subtree_files, 0) AS files "
            "FROM dir d LEFT JOIN dir_rollup r ON r.dir_id = d.id "
            "WHERE d.parent_id IS ? AND d.vanished_at IS NULL",
            (dir_id,),
        ).fetchall()

        for row in rows:
            child = int(row["id"])
            if child in exclude_dirs or child in visited:
                continue
            if not int(row["files"]):
                continue

            small_enough = int(row["bytes"]) <= target_bytes
            if small_enough or depth >= max_depth:
                visited.add(child)
                unit = _unit_for_dir(catalog, child, row["name"], exclude_dirs)
                if unit is not None:
                    units.append(unit)
            else:
                walk(child, depth + 1)
                # Files directly in this directory are not covered by any child
                # unit, so they need one of their own or they vanish from the
                # plan — silently dropping content is worse than a small unit.
                leftover = _direct_files_unit(catalog, child, row["name"])
                if leftover is not None:
                    units.append(leftover)

    walk(None, 0)
    return units


def _unit_for_dir(
    catalog: Catalog, dir_id: int, name: str, exclude_dirs: set[int]
) -> Unit | None:
    dir_ids = [
        d
        for d in collections_module.subtree_dir_ids(catalog, dir_id)
        if d not in exclude_dirs
    ]
    if not dir_ids:
        return None

    placeholders = ",".join("?" * len(dir_ids))
    where = f"f.dir_id IN ({placeholders})"
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
        f"  GROUP_CONCAT(DISTINCT {DRIVE_KEY}) AS drive_keys "
        "FROM file f JOIN volume v ON v.id = f.volume_id "
        f"WHERE f.vanished_at IS NULL AND {where}",
        dir_ids,
    ).fetchone()

    if not row or not int(row["files"]):
        return None
    return Unit(
        key=f"dir:{dir_id}",
        label=catalog.dir_path(dir_id) or name,
        files=int(row["files"]),
        bytes=int(row["bytes"]),
        drive_keys=_drive_keys(row["drive_keys"]),
        dir_id=dir_id,
        size_histogram=_histogram(catalog, where, list(dir_ids)),
    )


def _direct_files_unit(catalog: Catalog, dir_id: int, name: str) -> Unit | None:
    """Files sitting directly in a split directory, which no child unit covers."""
    where = "f.dir_id = ?"
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
        f"  GROUP_CONCAT(DISTINCT {DRIVE_KEY}) AS drive_keys "
        "FROM file f JOIN volume v ON v.id = f.volume_id "
        f"WHERE f.vanished_at IS NULL AND {where}",
        (dir_id,),
    ).fetchone()

    if not row or not int(row["files"]):
        return None
    return Unit(
        key=f"loose:{dir_id}",
        label=f"{catalog.dir_path(dir_id) or name} (files directly here)",
        files=int(row["files"]),
        bytes=int(row["bytes"]),
        drive_keys=_drive_keys(row["drive_keys"]),
        dir_id=dir_id,
        size_histogram=_histogram(catalog, where, [dir_id]),
    )


def placement_units(
    catalog: Catalog, *, target_bytes: int = UNIT_TARGET_BYTES
) -> list[Unit]:
    """Every unit the planner will place: collections first, then the rest.

    Collections take precedence, and their directories are excluded from the
    subtree pass so the same bytes are never placed twice under two names.
    """
    units = collection_units(catalog)

    claimed: set[int] = set()
    for collection in collections_module.all_collections(catalog):
        claimed |= set(collections_module.member_dir_ids(catalog, collection.id))

    units.extend(
        subtree_units(catalog, target_bytes=target_bytes, exclude_dirs=claimed)
    )
    units.sort(key=lambda u: u.bytes, reverse=True)
    return units


def volume_capacity(catalog: Catalog) -> list[dict]:
    """Destinations the planner can place onto, with their cluster sizes."""
    rows = catalog.conn.execute(
        "SELECT v.id AS volume_id, v.volume_guid, v.label, v.fs_type, "
        "       v.capacity_bytes, v.free_bytes, v.cluster_bytes, "
        "       v.drive_id, d.nickname, d.model, d.serial, "
        "       d.batch_key, d.disaster_zone "
        "FROM volume v LEFT JOIN drive d ON d.id = v.drive_id"
    )
    out = []
    for row in rows:
        entry = dict(row)
        entry["drive_key"] = (
            entry["drive_id"]
            if entry["drive_id"] is not None
            else -int(entry["volume_id"])
        )
        entry["name"] = (
            entry["nickname"]
            or entry["model"]
            or entry["label"]
            or entry["volume_guid"]
        )
        entry["cluster_bytes"] = entry["cluster_bytes"] or DEFAULT_CLUSTER_BYTES
        out.append(entry)
    return out
