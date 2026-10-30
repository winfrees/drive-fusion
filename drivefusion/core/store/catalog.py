"""Catalog data access: drives, volumes, scope, scans, the merge, and search.

Hand-written SQL rather than an ORM, because the query shapes here are unusual
(group-by-content, capacity rollups, a set-based merge over a staging table)
and bulk-insert throughput matters more than convenience.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from drivefusion import __version__
from drivefusion.core.errors import CatalogLocationError
from drivefusion.core.store import audit, schema
from drivefusion.core.store.schema import utcnow

#: Rows per executemany batch. Large enough to amortise statement overhead,
#: small enough that a interrupted scan loses little work.
BATCH_ROWS = 10_000


def path_within(path: Path | str, root: Path | str) -> bool:
    """True when ``path`` is ``root`` or lies beneath it.

    Both sides are resolved first, so a symlink, junction or relative spelling
    cannot hide the catalog inside a root.
    """
    child = os.path.normcase(os.path.realpath(path))
    parent = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        # Different drives on Windows: never nested.
        return False


#: When a scan of volume ``v`` last finished, for "last scanned" displays.
_LAST_SCANNED_VOLUME = (
    "SELECT MAX(s.finished_at) FROM scan s "
    "WHERE s.volume_id = v.id AND s.status = 'done'"
)

#: Enabled roots and the volume each is on, for the catalog-location checks.
_ENABLED_ROOT_LOCATIONS = (
    "SELECT r.path, v.volume_guid FROM scope_root r "
    "LEFT JOIN volume v ON v.id = r.volume_id "
    "WHERE r.removed_at IS NULL AND r.enabled = 1"
)


def root_clause(root_ids, alias: str = "f") -> tuple[str, list]:
    """SQL restricting file rows to the given scope roots, or nothing for ``None``.

    Used by the passes that read file contents, so a root whose volume could not
    be confirmed is not read (docs/REVIEW.md F-01).
    """
    if root_ids is None:
        return "", []
    ids = sorted(int(r) for r in root_ids)
    placeholders = ", ".join("?" for _ in ids)
    return (
        f" AND {alias}.dir_id IN (SELECT id FROM dir WHERE root_id IN ({placeholders}))",
        ids,
    )


@dataclass(frozen=True)
class ScopeRoot:
    id: int
    volume_id: int
    path: str
    excludes: tuple[str, ...]
    enabled: bool
    added_at: str


@dataclass(frozen=True)
class ScanStats:
    scan_id: int
    dirs_seen: int = 0
    files_seen: int = 0
    bytes_seen: int = 0
    unreadable: int = 0
    dehydrated_skipped: int = 0
    excluded: int = 0


class Catalog:
    """A catalog database. Writes are confined to this file and its backups."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Checked before connecting as well as after: opening a WAL database
        # creates its -wal and -shm files, which is the very write to the
        # observed tree this refusal exists to prevent.
        self._refuse_location(self._roots_on_disk())
        self.conn = schema.connect(self.path)
        try:
            before = schema.current_version(self.conn)
            after = schema.migrate(self.conn, self.path)
            self._refuse_location(self._enabled_root_locations())
            if before and before != after:
                self.record("catalog.upgraded", {"from": before, "to": after})
        except BaseException:
            self.conn.close()
            raise

    # -- audit ---------------------------------------------------------------

    def record(self, action: str, detail: dict | None = None) -> int:
        """Append to the hash-chained audit log (``core/store/audit.py``)."""
        return audit.append(self.conn, action, detail)

    def _roots_on_disk(self) -> list[tuple[str, str | None]]:
        """Enabled roots of an existing catalog as ``(path, volume_guid)``,
        read without writing.

        ``immutable=1`` takes no locks and creates no side files. It does not
        see rows still only in the WAL; the check after connecting covers those.
        """
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return []
        uri = self.path.resolve().as_uri() + "?mode=ro&immutable=1"
        try:
            probe = sqlite3.connect(uri, uri=True)
            try:
                rows = probe.execute(_ENABLED_ROOT_LOCATIONS).fetchall()
            finally:
                probe.close()
        except sqlite3.Error:
            return []
        return [(row[0], row[1]) for row in rows]

    def _enabled_root_locations(self) -> list[tuple[str, str | None]]:
        return [(row[0], row[1]) for row in self.conn.execute(_ENABLED_ROOT_LOCATIONS)]

    def _refuse_location(self, roots: Iterable[tuple[str, str | None]]) -> None:
        """Refuse to use a catalog inside a root, or on a root's drive.

        The catalog may share a volume with scanned folders only when that
        volume is the system drive, which Windows writes to constantly anyway.
        Even there it may not sit inside a scanned folder, so scanning the
        whole system drive leaves nowhere on it for the catalog.
        """
        roots = list(roots)
        for root_path, _guid in roots:
            if path_within(self.path, root_path):
                raise CatalogLocationError(
                    f"refusing to use catalog {self.path}: it is inside scope "
                    f"root {root_path}, and Drive Fusion writes nothing inside "
                    "a scope root. Keep the catalog elsewhere (--catalog or "
                    "DRIVEFUSION_CATALOG)."
                )
        sharing = self._roots_sharing_catalog_volume(roots)
        if sharing:
            raise CatalogLocationError(
                f"refusing to use catalog {self.path}: it is on the same drive "
                f"as scope root {sharing[0]}, and Drive Fusion writes nothing to "
                "a drive it catalogues. Only the system drive may hold both the "
                "catalog and scanned folders. Keep the catalog on another drive "
                "(--catalog or DRIVEFUSION_CATALOG)."
            )

    def _roots_sharing_catalog_volume(
        self, roots: Iterable[tuple[str, str | None]]
    ) -> list[str]:
        """Roots on this catalog's volume, unless that volume is the system drive.

        A catalog whose volume can't be identified (a network share, say) is
        not refused: every scope root is on an identified local volume, so it
        can't be one of them.
        """
        by_guid: dict[str, list[str]] = {}
        for root_path, guid in roots:
            if guid:
                by_guid.setdefault(guid, []).append(root_path)
        if not by_guid:
            return []

        from drivefusion.core import discovery

        try:
            mine = discovery.volume_guid_of(str(self.path.resolve().parent))
        except (discovery.DiscoveryError, OSError):
            return []
        if mine not in by_guid:
            return []
        try:
            if mine == discovery.system_volume_guid():
                return []
        except (discovery.DiscoveryError, OSError):
            pass        # unknown system drive: no exception to the rule
        return by_guid[mine]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- drives and volumes ------------------------------------------------

    def upsert_drive(
        self, *, serial: str | None = None, layout_id: str | None = None, **fields
    ) -> int:
        """Record a physical drive, found by its serial or, failing that, its
        partition-table identity (docs/REVIEW.md F-05).

        A disk first seen without a serial (through a USB bridge that hides
        it) and later with one (in a dock that doesn't) is matched by layout
        and gains the serial, rather than becoming a second drive.
        """
        if not serial and not layout_id:
            raise ValueError("a drive needs a serial or a layout_id to be identified")
        row = None
        if serial:
            row = self.conn.execute(
                "SELECT id FROM drive WHERE serial = ?", (serial,)
            ).fetchone()
        if row is None and layout_id:
            row = self.conn.execute(
                "SELECT id FROM drive WHERE layout_id = ? "
                "AND (serial IS NULL OR serial = ?)",
                (layout_id, serial or ""),
            ).fetchone()
            if row is not None and serial:
                fields = {**fields, "serial": serial, "identity": "serial"}
        if layout_id and self._layout_free(layout_id, row["id"] if row else None):
            fields = {**fields, "layout_id": layout_id}
        now = utcnow()
        if row:
            if fields:
                assignments = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(
                    f"UPDATE drive SET {assignments}, last_seen_at = ? WHERE id = ?",
                    (*fields.values(), now, row["id"]),
                )
            else:
                self.conn.execute(
                    "UPDATE drive SET last_seen_at = ? WHERE id = ?", (now, row["id"])
                )
            return int(row["id"])

        fields = {"identity": "serial" if serial else "disk-layout", **fields}
        columns = ["serial", "first_seen_at", "last_seen_at", *fields]
        values = [serial, now, now, *fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.conn.execute(
            f"INSERT INTO drive ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return int(cursor.lastrowid)

    def _layout_free(self, layout_id: str, drive_id: int | None) -> bool:
        """Whether ``layout_id`` can be recorded on ``drive_id``.

        Two serial-bearing drives can share a layout (a disk cloned sector by
        sector), and the first keeps it; the serials still tell them apart.
        """
        holder = self.conn.execute(
            "SELECT id FROM drive WHERE layout_id = ?", (layout_id,)
        ).fetchone()
        return holder is None or holder["id"] == drive_id

    def drive(self, drive_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drive WHERE id = ?", (drive_id,)).fetchone()

    def update_drive(self, drive_id: int, changes: dict[str, str | None]) -> dict:
        """Edit what the user knows about a drive (ROADMAP M5.2).

        ``changes`` maps the user-facing names in ``core.health.metadata.FIELDS``
        to raw strings, or ``None`` to clear. Everything is validated before
        anything is written, and the edit is audited with each field's old and
        new value. Returns the changes that took effect.
        """
        from drivefusion.core.health import metadata

        row = self.drive(drive_id)
        if row is None:
            raise ValueError(f"no drive {drive_id} in the catalog")
        columns = metadata.parse_changes(changes)
        effective = {c: v for c, v in columns.items() if row[c] != v}
        if not effective:
            return {}
        assignments = ", ".join(f"{c} = ?" for c in effective)
        self.conn.execute(
            f"UPDATE drive SET {assignments} WHERE id = ?", (*effective.values(), drive_id)
        )
        self.record("drive.edit", {
            "drive_id": drive_id,
            "changes": {c: [row[c], v] for c, v in effective.items()},
        })
        return effective

    def import_afr_table(self, table: dict) -> str:
        """Keep an imported AFR table (``core.health.backblaze``), replacing one
        of the same source and version. Audited. Returns its name."""
        import json

        from drivefusion.core.store.schema import utcnow

        name = f"{table['source']}-{table['version']}"
        replaced = self.conn.execute(
            "SELECT 1 FROM afr_table WHERE name = ?", (name,)).fetchone() is not None
        self.conn.execute(
            "INSERT INTO afr_table (name, imported_at, table_json) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET imported_at = excluded.imported_at, "
            "table_json = excluded.table_json",
            (name, utcnow(), json.dumps(table, separators=(",", ":"))),
        )
        self.record("afr.import", {"name": name, "replaced": replaced,
                                   "models": len(table.get("models", [])),
                                   "inputs": table.get("inputs", {})})
        return name

    def afr_tables(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT name, imported_at, table_json FROM afr_table ORDER BY name").fetchall()

    def forget_afr_table(self, name: str) -> bool:
        forgotten = self.conn.execute("DELETE FROM afr_table WHERE name = ?", (name,)).rowcount
        if forgotten:
            self.record("afr.forget", {"name": name})
        return bool(forgotten)

    def record_discovered_volume(self, volume) -> int:
        """Record a volume found by discovery, and its drive if it has one.

        ``volume`` is a ``discovery.VolumeInfo``. A volume whose disk reports
        neither a serial nor a partition-table identity gets no drive: its
        drive is *unverified*, and copies on it don't count as proven until
        the user confirms which drive it is on (``confirm_volume_drive``,
        ``assign_volume_drive``). A drive already confirmed by the user is
        kept when discovery still can't identify it.
        """
        fields = volume.as_volume_fields()
        drive = volume.drive
        if drive is not None and (drive.serial or drive.layout_id):
            fields["drive_id"] = self.upsert_drive(
                serial=drive.serial,
                layout_id=drive.layout_id,
                model=drive.model,
                manufacturer=drive.manufacturer,
                bus=drive.bus,
                media=drive.media,
            )
        return self.upsert_volume(volume_guid=volume.volume_guid, **fields)

    def unverified_volumes(self) -> list[sqlite3.Row]:
        """Volumes whose physical drive is not known: no serial, no layout,
        no confirmation. Copies on them count as proven only once."""
        return list(
            self.conn.execute(
                "SELECT * FROM volume WHERE drive_id IS NULL ORDER BY id"
            )
        )

    def confirm_volume_drive(self, volume_id: int, *, nickname: str | None = None) -> int:
        """The user's word that this volume is on a drive of its own.

        Only for a volume discovery couldn't tie to a drive. A drive identified
        by its serial or its partition table is a fact the user can't
        overrule here.
        """
        volume = self._unverified_volume(volume_id)
        now = utcnow()
        cursor = self.conn.execute(
            "INSERT INTO drive (identity, nickname, first_seen_at, last_seen_at) "
            "VALUES ('user', ?, ?, ?)",
            (nickname or volume["label"], now, now),
        )
        drive_id = int(cursor.lastrowid)
        self.conn.execute(
            "UPDATE volume SET drive_id = ? WHERE id = ?", (drive_id, volume_id)
        )
        self.record("drive.confirm", {"volume_id": volume_id, "drive_id": drive_id,
                                      "volume_guid": volume["volume_guid"]})
        return drive_id

    def assign_volume_drive(self, volume_id: int, drive_id: int) -> None:
        """The user's word that this volume is on ``drive_id``, for instance a
        second partition of a disk that is already catalogued."""
        volume = self._unverified_volume(volume_id)
        if self.conn.execute("SELECT 1 FROM drive WHERE id = ?", (drive_id,)).fetchone() is None:
            raise ValueError(f"no drive {drive_id} in the catalog")
        self.conn.execute(
            "UPDATE volume SET drive_id = ? WHERE id = ?", (drive_id, volume_id)
        )
        self.record("drive.assign", {"volume_id": volume_id, "drive_id": drive_id,
                                     "volume_guid": volume["volume_guid"]})

    def _unverified_volume(self, volume_id: int) -> sqlite3.Row:
        volume = self.conn.execute(
            "SELECT v.*, d.identity AS drive_identity FROM volume v "
            "LEFT JOIN drive d ON d.id = v.drive_id WHERE v.id = ?",
            (volume_id,),
        ).fetchone()
        if volume is None:
            raise ValueError(f"no volume {volume_id} in the catalog")
        if volume["drive_id"] is not None and volume["drive_identity"] != "user":
            raise ValueError(
                f"volume {volume_id} is on drive {volume['drive_id']}, identified by "
                f"its {volume['drive_identity'].replace('-', ' ')}; that can't be "
                "overruled by hand"
            )
        return volume

    def upsert_volume(self, *, volume_guid: str, **fields) -> int:
        row = self.conn.execute(
            "SELECT id FROM volume WHERE volume_guid = ?", (volume_guid,)
        ).fetchone()
        now = utcnow()
        if row:
            if fields:
                assignments = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(
                    f"UPDATE volume SET {assignments}, last_seen_at = ? WHERE id = ?",
                    (*fields.values(), now, row["id"]),
                )
            return int(row["id"])

        columns = ["volume_guid", "last_seen_at", *fields]
        values = [volume_guid, now, *fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.conn.execute(
            f"INSERT INTO volume ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return int(cursor.lastrowid)

    def volumes(self) -> list[sqlite3.Row]:
        """Every volume, with ``last_scanned_at``: when a scan of it last
        finished. ``last_seen_at`` is when discovery last recorded it, which a
        scan doesn't change (docs/REVIEW.md F-12)."""
        return list(self.conn.execute(
            f"SELECT v.*, ({_LAST_SCANNED_VOLUME}) AS last_scanned_at "
            "FROM volume v ORDER BY v.id"
        ))

    def root_last_scanned(self) -> dict[int, str]:
        """When a scan of each scope root last finished, for roots ever scanned."""
        return {
            int(row["scope_root_id"]): row["finished_at"]
            for row in self.conn.execute(
                "SELECT scope_root_id, MAX(finished_at) AS finished_at FROM scan "
                "WHERE status = 'done' AND scope_root_id IS NOT NULL "
                "GROUP BY scope_root_id"
            )
        }

    # -- scope -------------------------------------------------------------

    def check_root_location(self, path: str, volume_guid: str | None = None) -> None:
        """Raise :class:`CatalogLocationError` if ``path`` may not become a root.

        Refused when it contains this catalog, or when ``volume_guid`` (the
        volume ``path`` is on) also holds the catalog and isn't the system
        drive. Callers that record anything before adding a root call this
        first, with the volume once they have identified it, so a refused root
        leaves nothing behind.
        """
        if path_within(self.path, path):
            raise CatalogLocationError(
                f"refusing to add {path}: it contains the catalog {self.path}, "
                "and Drive Fusion writes nothing inside a scope root. Move the "
                "catalog elsewhere (--catalog or DRIVEFUSION_CATALOG) first."
            )
        if volume_guid and self._roots_sharing_catalog_volume([(path, volume_guid)]):
            raise CatalogLocationError(
                f"refusing to add {path}: it is on the same drive as the catalog "
                f"{self.path}, and Drive Fusion writes nothing to a drive it "
                "catalogues. Only the system drive may hold both. Move the "
                "catalog to another drive (--catalog or DRIVEFUSION_CATALOG) first."
            )

    def add_scope_root(
        self, volume_id: int, path: str, excludes: Sequence[str] = ()
    ) -> int:
        """Add a root, or revive one that was removed earlier.

        Re-adding a previously removed path reactivates the original row rather
        than failing on the uniqueness constraint, so the catalogued history of
        that root is picked back up instead of being stranded.

        A root that contains this catalog is refused: every scan would then
        write into the tree it observes (docs/REVIEW.md F-03). So is a root on
        the catalog's own drive, unless that is the system drive.
        """
        recorded = self.conn.execute(
            "SELECT volume_guid FROM volume WHERE id = ?", (volume_id,)
        ).fetchone()
        self.check_root_location(path, recorded["volume_guid"] if recorded else None)
        row = self.conn.execute(
            "SELECT id FROM scope_root WHERE volume_id = ? AND path = ?",
            (volume_id, path),
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE scope_root SET removed_at = NULL, enabled = 1, "
                "excludes = ? WHERE id = ?",
                (json.dumps(list(excludes)), row["id"]),
            )
            root_id = int(row["id"])
        else:
            cursor = self.conn.execute(
                "INSERT INTO scope_root (volume_id, path, excludes, added_at) "
                "VALUES (?, ?, ?, ?)",
                (volume_id, path, json.dumps(list(excludes)), utcnow()),
            )
            root_id = int(cursor.lastrowid)
        self.record("scope.add", {
            "root_id": root_id, "path": path, "excludes": list(excludes),
            "volume_guid": recorded["volume_guid"] if recorded else None,
        })
        return root_id

    def remove_scope_root(self, root_id: int) -> bool:
        """Stop scanning a root without discarding what it already catalogued.

        The row is marked removed rather than deleted. Deleting it would either
        break the references from ``dir`` and ``scan``, or require cascading
        those away — and destroying catalogued history to express "stop looking
        here" is the opposite of what this tool is for.
        """
        cursor = self.conn.execute(
            "UPDATE scope_root SET removed_at = ?, enabled = 0 "
            "WHERE id = ? AND removed_at IS NULL",
            (utcnow(), root_id),
        )
        if cursor.rowcount > 0:
            self.record("scope.remove", {"root_id": root_id})
        return cursor.rowcount > 0

    def scope_roots(
        self, *, enabled_only: bool = False, include_removed: bool = False
    ) -> list[ScopeRoot]:
        clauses = []
        if not include_removed:
            clauses.append("removed_at IS NULL")
        if enabled_only:
            clauses.append("enabled = 1")
        sql = "SELECT * FROM scope_root"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [self._as_scope_root(row) for row in self.conn.execute(sql)]

    def scope_root(self, root_id: int) -> ScopeRoot | None:
        row = self.conn.execute(
            "SELECT * FROM scope_root WHERE id = ? AND removed_at IS NULL",
            (root_id,),
        ).fetchone()
        return self._as_scope_root(row) if row else None

    @staticmethod
    def _as_scope_root(row: sqlite3.Row) -> ScopeRoot:
        return ScopeRoot(
            id=int(row["id"]),
            volume_id=int(row["volume_id"]),
            path=row["path"],
            excludes=tuple(json.loads(row["excludes"])),
            enabled=bool(row["enabled"]),
            added_at=row["added_at"],
        )

    # -- scans -------------------------------------------------------------

    def begin_scan(
        self, volume_id: int, scope_root_id: int | None, method: str, elevated: bool
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO scan (volume_id, scope_root_id, started_at, status, "
            "method, elevated, tool_version) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (volume_id, scope_root_id, utcnow(), method, int(elevated), __version__),
        )
        return int(cursor.lastrowid)

    def finish_scan(
        self, stats: ScanStats, status: str = "done", error: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE scan SET finished_at = ?, status = ?, dirs_seen = ?, "
            "files_seen = ?, bytes_seen = ?, unreadable = ?, "
            "dehydrated_skipped = ?, excluded = ?, error = ? WHERE id = ?",
            (
                utcnow(), status, stats.dirs_seen, stats.files_seen,
                stats.bytes_seen, stats.unreadable, stats.dehydrated_skipped,
                stats.excluded, error, stats.scan_id,
            ),
        )
        row = self.conn.execute(
            "SELECT volume_id, scope_root_id, method FROM scan WHERE id = ?",
            (stats.scan_id,),
        ).fetchone()
        self.record("scan.finished", {
            "scan_id": stats.scan_id, "status": status,
            "root_id": row["scope_root_id"] if row else None,
            "volume_id": row["volume_id"] if row else None,
            "method": row["method"] if row else None,
            "files": stats.files_seen, "dirs": stats.dirs_seen,
            "unreadable": stats.unreadable, "error": error,
        })

    def scans(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM scan ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    # -- directory interning ----------------------------------------------

    def intern_dir(
        self,
        *,
        volume_id: int,
        root_id: int | None,
        parent_id: int | None,
        name: str,
        depth: int,
        scan_id: int,
        frn: int | None = None,
    ) -> int:
        """Return the id of a directory row, creating it if new.

        A single upsert with RETURNING, so no in-memory map of directory ids is
        needed and a rescan costs one statement per directory rather than a
        lookup plus a conditional insert.

        Root directories are the exception, and the reason is a genuine SQL
        trap: in a UNIQUE index, SQLite treats every NULL as distinct, so
        ``UNIQUE(volume_id, parent_id, name)`` does **not** constrain rows with
        ``parent_id IS NULL``. Left to the upsert, every rescan would insert a
        fresh root, and with it a fresh subtree and duplicate file rows for the
        entire volume. Roots are matched explicitly instead. There is one row
        per scope root, so the extra statement costs nothing.
        """
        if parent_id is None:
            existing = self.conn.execute(
                "SELECT id FROM dir WHERE volume_id = ? AND parent_id IS NULL "
                "AND name = ?",
                (volume_id, name),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE dir SET last_seen_scan = ?, root_id = ?, "
                    "vanished_at = NULL WHERE id = ?",
                    (scan_id, root_id, existing["id"]),
                )
                return int(existing["id"])
            cursor = self.conn.execute(
                "INSERT INTO dir (volume_id, root_id, parent_id, name, depth, "
                "frn, last_seen_scan) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (volume_id, root_id, name, depth, frn, scan_id),
            )
            return int(cursor.lastrowid)

        row = self.conn.execute(
            "INSERT INTO dir (volume_id, root_id, parent_id, name, depth, frn, "
            "last_seen_scan) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(volume_id, parent_id, name) DO UPDATE SET "
            "last_seen_scan = excluded.last_seen_scan, "
            "root_id = excluded.root_id, "
            "vanished_at = NULL "
            "RETURNING id",
            (volume_id, root_id, parent_id, name, depth, frn, scan_id),
        ).fetchone()
        return int(row["id"])

    def dir_path(self, dir_id: int, separator: str = "/") -> str:
        """Reconstruct a full path by walking the interned tree upward."""
        row = self.conn.execute(
            "WITH RECURSIVE up(id, parent_id, acc) AS ("
            "  SELECT id, parent_id, name FROM dir WHERE id = ?"
            "  UNION ALL"
            "  SELECT d.id, d.parent_id, d.name || ? || up.acc"
            "    FROM dir d JOIN up ON d.id = up.parent_id"
            ") SELECT acc FROM up WHERE parent_id IS NULL",
            (dir_id, separator),
        ).fetchone()
        return row["acc"] if row else ""

    # -- staging and merge -------------------------------------------------

    def stage_files(self, rows: Iterable[tuple]) -> int:
        """Bulk-insert into the unindexed landing table."""
        count = 0
        batch: list[tuple] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= BATCH_ROWS:
                count += self._flush_stage(batch)
                batch.clear()
        if batch:
            count += self._flush_stage(batch)
        return count

    def _flush_stage(self, batch: Sequence[tuple]) -> int:
        self.conn.executemany(
            "INSERT INTO stage_file (scan_id, volume_id, dir_id, name, ext, "
            "size_bytes, alloc_bytes, mtime_ns, ctime_ns, frn, nlink, attrs, "
            "read_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        return len(batch)

    def dir_by_frn(self, volume_id: int, frn: int) -> sqlite3.Row | None:
        """Find a catalogued directory by its file reference number.

        The change journal names parents by frn, so this is how a delta maps a
        journal record back to a row the catalog already knows.
        """
        return self.conn.execute(
            "SELECT * FROM dir WHERE volume_id = ? AND frn = ?", (volume_id, frn)
        ).fetchone()

    def tombstone_subtree(self, dir_id: int) -> int:
        """Mark a directory, its descendants, and all their files as gone.

        Used when a fresh listing shows a directory is no longer present. The
        rows are kept — a tombstone is how the catalog outlives the bits, and
        is what lets a report say what was lost when a drive fails.
        """
        now = utcnow()
        ids = [
            int(row["id"])
            for row in self.conn.execute(
                "WITH RECURSIVE tree(id) AS ("
                "  SELECT id FROM dir WHERE id = ?"
                "  UNION ALL"
                "  SELECT d.id FROM dir d JOIN tree ON d.parent_id = tree.id"
                ") SELECT id FROM tree",
                (dir_id,),
            )
        ]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE file SET vanished_at = ? WHERE vanished_at IS NULL "
            f"AND dir_id IN ({placeholders})",
            (now, *ids),
        )
        self.conn.execute(
            f"UPDATE dir SET vanished_at = ? WHERE vanished_at IS NULL "
            f"AND id IN ({placeholders})",
            (now, *ids),
        )
        return len(ids)

    def stage_dirs(self, scan_id: int, dir_ids: Iterable[int]) -> int:
        """Record which directories a scan actually examined."""
        rows = [(scan_id, dir_id) for dir_id in dir_ids]
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO stage_dir (scan_id, dir_id) VALUES (?, ?)",
                rows,
            )
        return len(rows)

    def journal_cursor(self, volume_id: int) -> tuple[int | None, int | None]:
        row = self.conn.execute(
            "SELECT usn_journal_id, usn_next FROM volume WHERE id = ?", (volume_id,)
        ).fetchone()
        if row is None:
            return (None, None)
        return (row["usn_journal_id"], row["usn_next"])

    def set_journal_cursor(
        self, volume_id: int, journal_id: int | None, next_usn: int | None
    ) -> None:
        """Store the point a delta rescan should resume from.

        Written only after a scan has been merged. Advancing it earlier would
        mean a crash between enumeration and merge silently skipped the changes
        that were read but never recorded.
        """
        self.conn.execute(
            "UPDATE volume SET usn_journal_id = ?, usn_next = ? WHERE id = ?",
            (journal_id, next_usn, volume_id),
        )

    def merge_scan(
        self,
        scan_id: int,
        volume_id: int,
        root_id: int | None,
        *,
        vanish_scope: str = "root",
    ) -> dict:
        """Fold a scan's staged rows into the catalog in one set-based pass.

        Returns counts for reporting. This is where the loading strategy from
        docs/PLAN.md §7.2 pays off: one upsert over the staging table instead
        of tens of millions of individual index-updating inserts.

        ``vanish_scope`` decides what "not seen" is allowed to mean:

        * ``"root"``: anything under the root that went unseen is gone. Only
          true when every directory under it was listed.
        * ``"listed-dirs"``: what a full scan uses. Files missing from a
          directory the scan listed are gone, as are child directories missing
          from such a listing, with their subtrees. Nothing beneath a directory
          whose listing failed is touched: it is unknown, not gone.
        * ``"staged-dirs"``: what a delta uses. Only files in the directories
          the journal named may be tombstoned; the delta handles removed
          directories itself. Applying the root rule to a delta would declare
          the entire volume missing.

        ``"listed-dirs"`` and ``"staged-dirs"`` both read the directories the
        scan examined from ``stage_dir``.
        """
        conn = self.conn
        conn.execute("BEGIN")
        try:
            staged = conn.execute(
                "SELECT COUNT(*) AS n FROM stage_file WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()["n"]
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM file WHERE volume_id = ?", (volume_id,)
            ).fetchone()["n"]

            conn.execute(
                "INSERT INTO file (volume_id, dir_id, name, ext, size_bytes, "
                "  alloc_bytes, mtime_ns, ctime_ns, frn, nlink, attrs, "
                "  read_state, first_seen_scan, last_seen_scan) "
                "SELECT s.volume_id, s.dir_id, s.name, s.ext, s.size_bytes, "
                "  s.alloc_bytes, s.mtime_ns, s.ctime_ns, s.frn, s.nlink, "
                "  s.attrs, s.read_state, ?, ? "
                "FROM stage_file s WHERE s.scan_id = ? "
                "ON CONFLICT(dir_id, name) DO UPDATE SET "
                "  size_bytes = excluded.size_bytes, "
                "  alloc_bytes = excluded.alloc_bytes, "
                "  mtime_ns = excluded.mtime_ns, "
                "  ctime_ns = excluded.ctime_ns, "
                "  frn = excluded.frn, "
                "  nlink = excluded.nlink, "
                "  attrs = excluded.attrs, "
                "  read_state = excluded.read_state, "
                "  last_seen_scan = excluded.last_seen_scan, "
                "  vanished_at = NULL, "
                # Content identity is invalidated when the bytes may have
                # changed; M3's hashing re-establishes it.
                "  content_id = CASE "
                "    WHEN file.size_bytes = excluded.size_bytes "
                "     AND file.mtime_ns = excluded.mtime_ns "
                "    THEN file.content_id ELSE NULL END",
                (scan_id, scan_id, scan_id),
            )

            after = conn.execute(
                "SELECT COUNT(*) AS n FROM file WHERE volume_id = ?", (volume_id,)
            ).fetchone()["n"]

            vanished = self._mark_vanished(
                scan_id, volume_id, root_id, vanish_scope
            )
            conn.execute("DELETE FROM stage_file WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM stage_dir WHERE scan_id = ?", (scan_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        inserted = after - before
        return {
            "staged": staged,
            "inserted": inserted,
            "updated": staged - inserted,
            "vanished": vanished,
        }

    def _mark_vanished(
        self,
        scan_id: int,
        volume_id: int,
        root_id: int | None,
        vanish_scope: str = "root",
    ) -> int:
        """Tombstone rows this scan did not see, scoped to what it looked at.

        Scoping matters: scanning ``D:\\Research`` must not mark everything
        under ``D:\\Games`` as gone. Because scope roots may not nest, a
        directory belongs to exactly one root, so ``dir.root_id`` is a
        sufficient and cheap filter.
        """
        now = utcnow()

        if vanish_scope == "listed-dirs":
            return self._mark_vanished_under_listed(scan_id, now)

        if vanish_scope == "staged-dirs":
            # Only directories this scan actually listed are eligible. A file
            # in an unexamined directory is unknown, not gone.
            cursor = self.conn.execute(
                "UPDATE file SET vanished_at = ? WHERE vanished_at IS NULL "
                "AND last_seen_scan < ? AND dir_id IN "
                "(SELECT dir_id FROM stage_dir WHERE scan_id = ?)",
                (now, scan_id, scan_id),
            )
            return cursor.rowcount

        if root_id is None:
            cursor = self.conn.execute(
                "UPDATE file SET vanished_at = ? WHERE volume_id = ? "
                "AND last_seen_scan < ? AND vanished_at IS NULL",
                (now, volume_id, scan_id),
            )
            vanished = cursor.rowcount
            self.conn.execute(
                "UPDATE dir SET vanished_at = ? WHERE volume_id = ? "
                "AND last_seen_scan < ? AND vanished_at IS NULL",
                (now, volume_id, scan_id),
            )
            return vanished

        cursor = self.conn.execute(
            "UPDATE file SET vanished_at = ? WHERE vanished_at IS NULL "
            "AND last_seen_scan < ? AND dir_id IN "
            "(SELECT id FROM dir WHERE root_id = ?)",
            (now, scan_id, root_id),
        )
        vanished = cursor.rowcount
        self.conn.execute(
            "UPDATE dir SET vanished_at = ? WHERE vanished_at IS NULL "
            "AND last_seen_scan < ? AND root_id = ?",
            (now, scan_id, root_id),
        )
        return vanished

    def _mark_vanished_under_listed(self, scan_id: int, now: str) -> int:
        """Tombstone what listed directories no longer contain (docs/REVIEW.md F-02)."""
        listed = "(SELECT dir_id FROM stage_dir WHERE scan_id = ?)"
        cursor = self.conn.execute(
            "UPDATE file SET vanished_at = ? WHERE vanished_at IS NULL "
            f"AND last_seen_scan < ? AND dir_id IN {listed}",
            (now, scan_id, scan_id),
        )
        vanished = cursor.rowcount

        # A directory absent from its parent's listing is gone, and so is
        # everything catalogued beneath it, however deep.
        gone = (
            "WITH RECURSIVE gone(id) AS ("
            "  SELECT id FROM dir WHERE vanished_at IS NULL "
            f"   AND last_seen_scan < ? AND parent_id IN {listed}"
            "  UNION"
            "  SELECT d.id FROM dir d JOIN gone ON d.parent_id = gone.id"
            ") SELECT id FROM gone"
        )
        cursor = self.conn.execute(
            f"UPDATE file SET vanished_at = ? WHERE vanished_at IS NULL "
            f"AND dir_id IN ({gone})",
            (now, scan_id, scan_id),
        )
        vanished += cursor.rowcount
        self.conn.execute(
            f"UPDATE dir SET vanished_at = ? WHERE vanished_at IS NULL "
            f"AND id IN ({gone})",
            (now, scan_id, scan_id),
        )
        return vanished

    def rebuild_rollups(self, volume_id: int) -> int:
        """Recompute directory rollups bottom-up.

        Done in one pass per depth level rather than per directory, so the cost
        is proportional to tree depth (tens of statements) rather than to the
        number of directories.
        """
        conn = self.conn
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM dir_rollup WHERE dir_id IN "
                "(SELECT id FROM dir WHERE volume_id = ?)",
                (volume_id,),
            )
            conn.execute(
                "INSERT INTO dir_rollup (dir_id, files, bytes, subtree_files, "
                "  subtree_bytes) "
                "SELECT d.id, "
                "  COALESCE(f.n, 0), COALESCE(f.b, 0), "
                "  COALESCE(f.n, 0), COALESCE(f.b, 0) "
                "FROM dir d LEFT JOIN ("
                "  SELECT dir_id, COUNT(*) AS n, SUM(size_bytes) AS b "
                "  FROM file WHERE vanished_at IS NULL GROUP BY dir_id"
                ") f ON f.dir_id = d.id "
                "WHERE d.volume_id = ? AND d.vanished_at IS NULL",
                (volume_id,),
            )

            max_depth_row = conn.execute(
                "SELECT MAX(depth) AS d FROM dir WHERE volume_id = ? "
                "AND vanished_at IS NULL",
                (volume_id,),
            ).fetchone()
            max_depth = int(max_depth_row["d"] or 0)

            for depth in range(max_depth - 1, -1, -1):
                conn.execute(
                    "UPDATE dir_rollup SET "
                    "  subtree_files = files + COALESCE(("
                    "    SELECT SUM(c.subtree_files) FROM dir k "
                    "    JOIN dir_rollup c ON c.dir_id = k.id "
                    "    WHERE k.parent_id = dir_rollup.dir_id), 0), "
                    "  subtree_bytes = bytes + COALESCE(("
                    "    SELECT SUM(c.subtree_bytes) FROM dir k "
                    "    JOIN dir_rollup c ON c.dir_id = k.id "
                    "    WHERE k.parent_id = dir_rollup.dir_id), 0) "
                    "WHERE dir_id IN (SELECT id FROM dir WHERE volume_id = ? "
                    "  AND depth = ? AND vanished_at IS NULL)",
                    (volume_id, depth),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return max_depth

    def analyze(self) -> None:
        """Refresh planner statistics after a large change."""
        self.conn.execute("ANALYZE")
        self.conn.execute("PRAGMA optimize")

    # -- content identity --------------------------------------------------

    def hash_candidates(
        self,
        volume_id: int | None = None,
        limit: int | None = None,
        root_ids=None,
    ):
        """Files worth opening: those whose size occurs more than once.

        A size that appears exactly once anywhere in the catalog cannot be a
        duplicate of anything, so the file is never opened — that is what keeps
        a multi-terabyte pass tractable (docs/PLAN.md §6.4).

        Ordered by volume then file reference number, which approximates
        physical layout on NTFS and turns a seek-bound workload into a largely
        sequential one. exFAT has no file ids, so those rows fall back to
        directory order, which approximates allocation order on the
        mostly-append-only drives this tool targets.

        ``root_ids``, when given, limits candidates to files under those roots.
        The size test still runs over the whole catalog: a file whose twin is on
        a drive that is unplugged today still needs an identity.
        """
        sql = (
            "SELECT f.id, f.volume_id, f.dir_id, f.name, f.size_bytes, f.frn "
            "FROM file f "
            "WHERE f.content_id IS NULL "
            "  AND f.vanished_at IS NULL "
            "  AND f.read_state = 'ok' "
            "  AND f.size_bytes > 0 "
            "  AND f.size_bytes IN ("
            "      SELECT size_bytes FROM file "
            "      WHERE vanished_at IS NULL AND read_state = 'ok' "
            "      GROUP BY size_bytes HAVING COUNT(*) > 1"
            "  )"
        )
        params: list = []
        if volume_id is not None:
            sql += " AND f.volume_id = ?"
            params.append(volume_id)
        clause, clause_params = root_clause(root_ids)
        sql += clause
        params.extend(clause_params)
        sql += " ORDER BY f.volume_id, f.frn IS NULL, f.frn, f.dir_id, f.name"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params))

    def upsert_content(
        self,
        *,
        size_bytes: int,
        quick_hash: bytes,
        full_hash: bytes | None,
        hash_algo: str,
    ) -> int:
        """Return the content row for a hash, creating it if new.

        The ``full_hash IS NULL`` case is handled separately, and for the same
        reason the directory root is (see ``intern_dir``): **SQLite treats
        every NULL as distinct in a UNIQUE index**, so
        ``UNIQUE(size_bytes, quick_hash, full_hash)`` does not constrain rows
        that have no full hash yet. Left to the upsert, two byte-identical
        files would each get their own content row, and the tool would report
        one copy of each where there are two of one — the precise opposite of
        what it exists to say.
        """
        if full_hash is None:
            existing = self.conn.execute(
                "SELECT id FROM content WHERE size_bytes = ? AND quick_hash = ? "
                "AND full_hash IS NULL",
                (size_bytes, quick_hash),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = self.conn.execute(
                "INSERT INTO content (size_bytes, quick_hash, full_hash, hash_algo) "
                "VALUES (?, ?, NULL, ?)",
                (size_bytes, quick_hash, hash_algo),
            )
            return int(cursor.lastrowid)

        row = self.conn.execute(
            "INSERT INTO content (size_bytes, quick_hash, full_hash, hash_algo) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(size_bytes, quick_hash, full_hash) DO UPDATE SET "
            "  hash_algo = excluded.hash_algo "
            "RETURNING id",
            (size_bytes, quick_hash, full_hash, hash_algo),
        ).fetchone()
        return int(row["id"])

    def assign_content(self, pairs: Iterable[tuple[int, int]]) -> int:
        """Attach files to content rows: an iterable of (content_id, file_id)."""
        rows = list(pairs)
        if rows:
            self.conn.executemany(
                "UPDATE file SET content_id = ? WHERE id = ?", rows
            )
        return len(rows)

    def quick_hash_collisions(self) -> list[sqlite3.Row]:
        """Content rows whose quick hash may be hiding more than one file.

        Only these need a full read. A group is ambiguous when several *files*
        with different bytes could share the sampled blocks; a group whose
        quick hash covered the whole file is already exact.

        A group of one is ambiguous too when another content row with live
        files shares its size and quick hash. That other row is always one the
        full pass already resolved: once a group is fully hashed its files move
        to full-hash rows and the quick-only row is left empty, and the next
        file with those sampled blocks lands on it alone. Unless it is fully
        read as well, it never meets the content it may be identical to, and a
        third copy of something reads as a first copy of something else
        (docs/VV.md §7).
        """
        return list(
            self.conn.execute(
                "SELECT c.id, c.size_bytes, COUNT(f.id) AS files "
                "FROM content c JOIN file f ON f.content_id = c.id "
                "WHERE c.full_hash IS NULL AND f.vanished_at IS NULL "
                "GROUP BY c.id HAVING COUNT(f.id) > 1 OR EXISTS ("
                "  SELECT 1 FROM content o JOIN file g ON g.content_id = o.id "
                "  WHERE o.size_bytes = c.size_bytes "
                "    AND o.quick_hash = c.quick_hash "
                "    AND o.id != c.id AND g.vanished_at IS NULL)"
            )
        )

    def files_for_content(self, content_id: int, root_ids=None) -> list[sqlite3.Row]:
        clause, params = root_clause(root_ids, alias="file")
        return list(
            self.conn.execute(
                "SELECT id, volume_id, dir_id, name, size_bytes FROM file "
                "WHERE content_id = ? AND vanished_at IS NULL" + clause,
                (content_id, *params),
            )
        )

    def record_fixity(
        self,
        content_id: int,
        file_id: int,
        expected: bytes | None,
        observed: bytes | None,
        result: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO fixity_check (content_id, file_id, checked_at, "
            "expected_hash, observed_hash, result) VALUES (?, ?, ?, ?, ?, ?)",
            (content_id, file_id, utcnow(), expected, observed, result),
        )

    # -- queries -----------------------------------------------------------

    def find(
        self,
        pattern: str,
        *,
        limit: int = 100,
        include_vanished: bool = False,
        after_id: int = 0,
    ) -> Iterator[dict]:
        """Search filenames with SQL LIKE semantics, paged by keyset.

        Keyset paging rather than OFFSET: OFFSET degrades linearly and the
        catalog is expected to reach tens of millions of rows (§11).
        """
        sql = (
            "SELECT f.id, f.name, f.size_bytes, f.dir_id, f.vanished_at, "
            "       v.volume_guid, v.label "
            "FROM file f JOIN volume v ON v.id = f.volume_id "
            "WHERE f.name LIKE ? AND f.id > ?"
        )
        params: list = [pattern, after_id]
        if not include_vanished:
            sql += " AND f.vanished_at IS NULL"
        sql += " ORDER BY f.id LIMIT ?"
        params.append(limit)

        for row in self.conn.execute(sql, params):
            yield {
                "id": int(row["id"]),
                "name": row["name"],
                "size_bytes": int(row["size_bytes"]),
                "path": self.dir_path(int(row["dir_id"])),
                "volume": row["label"] or row["volume_guid"],
                "vanished_at": row["vanished_at"],
            }

    def counts(self) -> dict:
        row = self.conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM volume) AS volumes, "
            "  (SELECT COUNT(*) FROM scope_root) AS roots, "
            "  (SELECT COUNT(*) FROM dir WHERE vanished_at IS NULL) AS dirs, "
            "  (SELECT COUNT(*) FROM file WHERE vanished_at IS NULL) AS files, "
            "  (SELECT COALESCE(SUM(size_bytes), 0) FROM file "
            "     WHERE vanished_at IS NULL) AS bytes, "
            "  (SELECT COUNT(*) FROM file WHERE vanished_at IS NOT NULL) AS vanished, "
            "  (SELECT COUNT(*) FROM file WHERE vanished_at IS NULL "
            "     AND content_id IS NOT NULL) AS identified, "
            "  (SELECT COUNT(*) FROM content) AS content"
        ).fetchone()
        return dict(row)
