"""Catalog data access: drives, volumes, scope, scans, the merge, and search.

Hand-written SQL rather than an ORM, because the query shapes here are unusual
(group-by-content, capacity rollups, a set-based merge over a staging table)
and bulk-insert throughput matters more than convenience.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from drivefusion import __version__
from drivefusion.core.store import schema
from drivefusion.core.store.schema import utcnow

#: Rows per executemany batch. Large enough to amortise statement overhead,
#: small enough that a interrupted scan loses little work.
BATCH_ROWS = 10_000


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
        self.conn = schema.connect(self.path)
        schema.migrate(self.conn, self.path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- drives and volumes ------------------------------------------------

    def upsert_drive(self, *, serial: str, **fields) -> int:
        row = self.conn.execute(
            "SELECT id FROM drive WHERE serial = ?", (serial,)
        ).fetchone()
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

        columns = ["serial", "first_seen_at", "last_seen_at", *fields]
        values = [serial, now, now, *fields.values()]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.conn.execute(
            f"INSERT INTO drive ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return int(cursor.lastrowid)

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
        return list(self.conn.execute("SELECT * FROM volume ORDER BY id"))

    # -- scope -------------------------------------------------------------

    def add_scope_root(
        self, volume_id: int, path: str, excludes: Sequence[str] = ()
    ) -> int:
        """Add a root, or revive one that was removed earlier.

        Re-adding a previously removed path reactivates the original row rather
        than failing on the uniqueness constraint, so the catalogued history of
        that root is picked back up instead of being stranded.
        """
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
            return int(row["id"])

        cursor = self.conn.execute(
            "INSERT INTO scope_root (volume_id, path, excludes, added_at) "
            "VALUES (?, ?, ?, ?)",
            (volume_id, path, json.dumps(list(excludes)), utcnow()),
        )
        return int(cursor.lastrowid)

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

    def merge_scan(self, scan_id: int, volume_id: int, root_id: int | None) -> dict:
        """Fold a scan's staged rows into the catalog in one set-based pass.

        Returns counts for reporting. This is where the loading strategy from
        docs/PLAN.md §7.2 pays off: one upsert over the staging table instead
        of tens of millions of individual index-updating inserts.
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

            vanished = self._mark_vanished(scan_id, volume_id, root_id)
            conn.execute("DELETE FROM stage_file WHERE scan_id = ?", (scan_id,))
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
        self, scan_id: int, volume_id: int, root_id: int | None
    ) -> int:
        """Tombstone rows this scan did not see, scoped to what it looked at.

        Scoping matters: scanning ``D:\\Research`` must not mark everything
        under ``D:\\Games`` as gone. Because scope roots may not nest, a
        directory belongs to exactly one root, so ``dir.root_id`` is a
        sufficient and cheap filter.
        """
        now = utcnow()
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
            "  (SELECT COUNT(*) FROM file WHERE vanished_at IS NOT NULL) AS vanished"
        ).fetchone()
        return dict(row)
