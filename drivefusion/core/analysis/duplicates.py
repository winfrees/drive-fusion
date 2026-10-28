"""Redundancy and duplication analysis.

The counting rule that matters, and the one that is easy to get wrong:
**redundancy is counted over distinct physical drives, not over paths.**

* Two paths on the same drive are one copy for durability purposes. If that
  drive dies, both are gone together.
* Two paths sharing an inode or file reference number are *the same file* seen
  twice — a hardlink is not a second copy of anything, and counting it as one
  would inflate the number that the whole tool exists to report.
* A volume whose physical drive is unknown (no serial, no partition-table
  identity, not confirmed by the user) gets two answers, each used where it
  errs safely (docs/REVIEW.md F-05). For **protection** it adds no drive that
  isn't proven: two partitions of one serial-less disk must not read as two
  copies, so copies are counted over identified drives only, at least one. For
  **reclamation** it counts as its own drive, so a copy that may well be on
  another disk is never offered as same-drive waste. Where the two differ the
  group says so (``identity_uncertain``).
* A file whose size no other catalogued file shares is one copy, known
  without reading a byte of it. Hashing never opens such a file, so it has no
  content identity, but leaving it out of the durability figures would hide the
  files most certainly at risk (docs/REVIEW.md F-04).

Everything here is a read-only query. Reclamation candidates are evidence for
a human, never an instruction (docs/PLAN.md §3.3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from drivefusion.core.store.catalog import Catalog

#: Drives that certainly hold a copy: identified drives only, and at least
#: one, since a live file is on *some* drive. Used for every protection
#: figure: copies, the under-protected list, repair sources.
DRIVES_PROVEN = "MAX(COUNT(DISTINCT v.drive_id), 1)"

#: Drives that might hold a copy: each unidentified volume counted as its own
#: device (negating the volume id keeps it clear of real drive ids). Used only
#: to keep same-drive reclamation from claiming a possibly separate copy.
DRIVES_POSSIBLE = "COUNT(DISTINCT COALESCE(v.drive_id, -v.id))"

_LIVE = "f.vanished_at IS NULL AND f.content_id IS NOT NULL"

#: Sizes held by exactly one live file. Counted over every read state: a size
#: shared with an unreadable file or a placeholder might be a second copy.
_UNIQUE_SIZES = (
    "SELECT size_bytes FROM file WHERE vanished_at IS NULL "
    "GROUP BY size_bytes HAVING COUNT(*) = 1"
)

#: Live files with no identity whose size is unique: single copies by
#: construction. Empty files are left out; there is nothing in them to lose.
_SINGLE_COPY = (
    "f.vanished_at IS NULL AND f.content_id IS NULL AND f.size_bytes > 0 "
    f"AND f.size_bytes IN ({_UNIQUE_SIZES})"
)


@dataclass(frozen=True)
class ContentGroup:
    """One content item and where it is held.

    ``content_id`` is ``None`` for a single-copy file of unique size, which
    has no identity because it never needed one; ``file_id`` names it instead.
    """

    content_id: int | None
    size_bytes: int
    paths: int
    drives: int
    physical_copies: int
    file_id: int | None = None
    #: Drives that *might* hold a copy; more than ``drives`` when a copy sits
    #: on a volume whose drive is unverified. ``None`` means equal.
    drives_possible: int | None = None

    @property
    def identity_uncertain(self) -> bool:
        """A copy is on a volume whose drive is unverified, so ``drives``
        (proven) may undercount. Confirming the drive settles it."""
        return (self.drives_possible or self.drives) > self.drives

    @property
    def reclaimable_bytes(self) -> int:
        """Bytes freed if every duplicate *within* a drive were removed.

        Cross-drive copies are the point of the exercise and are never counted
        as waste; only extra copies on the same device are. A copy on an
        unverified drive counts as possibly elsewhere.
        """
        drives = self.drives_possible or self.drives
        return max(0, self.physical_copies - drives) * self.size_bytes


def _group_rows(catalog: Catalog, having: str = "", params: tuple = ()):
    """One row per content item, with path, drive, and physical-copy counts."""
    return list(
        catalog.conn.execute(
            "SELECT c.id AS content_id, c.size_bytes, "
            "  COUNT(f.id) AS paths, "
            f"  {DRIVES_PROVEN} AS drives, "
            f"  {DRIVES_POSSIBLE} AS drives_possible, "
            # Hardlinks share a file reference number on a volume, so a
            # physical copy is a distinct (volume, frn) — falling back to the
            # file id where the filesystem has no frn to give.
            "  COUNT(DISTINCT f.volume_id || ':' || "
            "        COALESCE(f.frn, 'f' || f.id)) AS physical_copies "
            "FROM file f "
            "JOIN content c ON c.id = f.content_id "
            "JOIN volume v ON v.id = f.volume_id "
            f"WHERE {_LIVE} "
            "GROUP BY c.id "
            + (f"HAVING {having} " if having else "")
            + "ORDER BY c.size_bytes * COUNT(f.id) DESC",
            params,
        )
    )


def _as_groups(rows) -> list[ContentGroup]:
    return [
        ContentGroup(
            content_id=int(r["content_id"]),
            size_bytes=int(r["size_bytes"]),
            paths=int(r["paths"]),
            drives=int(r["drives"]),
            physical_copies=int(r["physical_copies"]),
            drives_possible=int(r["drives_possible"]),
        )
        for r in rows
    ]


def single_copy_files(catalog: Catalog) -> list[ContentGroup]:
    """Files whose size no other catalogued file shares: one copy each."""
    return [
        ContentGroup(
            content_id=None,
            size_bytes=int(r["size_bytes"]),
            paths=1,
            drives=1,
            physical_copies=1,
            file_id=int(r["id"]),
        )
        for r in catalog.conn.execute(
            f"SELECT f.id, f.size_bytes FROM file f WHERE {_SINGLE_COPY} "
            "ORDER BY f.size_bytes DESC"
        )
    ]


def duplicate_groups(catalog: Catalog, *, limit: int | None = None) -> list[ContentGroup]:
    """Content that exists at more than one path, anywhere."""
    groups = _as_groups(_group_rows(catalog, "COUNT(f.id) > 1"))
    return groups[:limit] if limit else groups


def under_protected(
    catalog: Catalog, *, min_copies: int = 2, limit: int | None = None
) -> list[ContentGroup]:
    """Content held on fewer distinct drives than the policy requires.

    Ranked by size, which is the best available proxy until M5 supplies each
    drive's failure probability and the ranking becomes size × P(loss).
    """
    groups = _as_groups(
        _group_rows(catalog, f"{DRIVES_PROVEN} < ?", (min_copies,))
    )
    if min_copies > 1:
        groups.extend(single_copy_files(catalog))
    groups.sort(key=lambda g: g.size_bytes * max(1, g.paths), reverse=True)
    return groups[:limit] if limit else groups


def reclamation_candidates(
    catalog: Catalog, *, limit: int | None = None
) -> list[ContentGroup]:
    """Content duplicated *within* a single drive — space with no durability.

    Evidence only. The tool does not delete, and does not generate a command
    that would (§3.2).
    """
    groups = [
        group
        for group in _as_groups(_group_rows(catalog, "COUNT(f.id) > 1"))
        if group.reclaimable_bytes > 0
    ]
    groups.sort(key=lambda g: g.reclaimable_bytes, reverse=True)
    return groups[:limit] if limit else groups


def redundancy_summary(catalog: Catalog, *, min_copies: int = 2) -> dict:
    """Headline numbers for the dashboard and the CLI."""
    row = catalog.conn.execute(
        "SELECT "
        "  COUNT(DISTINCT f.content_id) AS distinct_content, "
        "  COUNT(f.id) AS catalogued_paths, "
        "  COALESCE(SUM(f.size_bytes), 0) AS path_bytes "
        "FROM file f WHERE " + _LIVE
    ).fetchone()

    unique_bytes = catalog.conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS b FROM content "
        "WHERE id IN (SELECT DISTINCT content_id FROM file "
        "             WHERE vanished_at IS NULL AND content_id IS NOT NULL)"
    ).fetchone()["b"]

    # Genuinely unknown: no identity yet, and a size some other file shares.
    unhashed = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file "
        "WHERE vanished_at IS NULL AND content_id IS NULL "
        f"AND size_bytes NOT IN ({_UNIQUE_SIZES})"
    ).fetchone()["n"]

    single = single_copy_files(catalog)
    single_bytes = sum(g.size_bytes for g in single)
    exposed = under_protected(catalog, min_copies=min_copies)
    reclaim = reclamation_candidates(catalog)

    path_bytes = int(row["path_bytes"]) + single_bytes
    unique_total = int(unique_bytes) + single_bytes
    return {
        "distinct_content": int(row["distinct_content"]) + len(single),
        "catalogued_paths": int(row["catalogued_paths"]) + len(single),
        "path_bytes": path_bytes,
        "unique_bytes": unique_total,
        "duplicate_overhead_bytes": path_bytes - unique_total,
        "under_protected_items": len(exposed),
        # Of those, the ones that may be better protected than shown: a copy
        # is on a volume whose drive the user hasn't confirmed yet.
        "unverified_drive_items": sum(1 for g in exposed if g.identity_uncertain),
        "under_protected_bytes": sum(g.size_bytes for g in exposed),
        "reclaimable_bytes": sum(g.reclaimable_bytes for g in reclaim),
        # Files known to be single copies from their size alone; counted
        # above, never opened.
        "single_copy_files": len(single),
        # Coverage, stated rather than implied: files whose size is shared but
        # that have not been hashed could be copies of anything, and a summary
        # that left them out silently would read as a summary over all of it.
        "unhashed_files": int(unhashed),
    }


def integrity_incidents(catalog: Catalog, *, limit: int = 100) -> list[dict]:
    """Fixity failures: silent corruption and files that became unreadable."""
    rows = catalog.conn.execute(
        "SELECT fc.result, fc.checked_at, f.dir_id, f.name, f.size_bytes, "
        "       fc.content_id "
        "FROM fixity_check fc JOIN file f ON f.id = fc.file_id "
        # 'modified' is an edit since the last scan, not a fixity failure.
        "WHERE fc.result NOT IN ('ok', 'modified') "
        "ORDER BY fc.id DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in rows:
        out.append(
            {
                "result": row["result"],
                "checked_at": row["checked_at"],
                "path": catalog.dir_path(int(row["dir_id"])),
                "name": row["name"],
                "size_bytes": int(row["size_bytes"]),
                # Where else this content lives — the copies that could repair
                # it, which is the actionable half of a corruption report.
                "other_copies": _other_copies(catalog, int(row["content_id"])),
            }
        )
    return out


def content_paths(catalog: Catalog, content_id: int, *, limit: int = 10) -> list[str]:
    """Where a content item currently lives, for a report a human has to read.

    A group identified only by a row id is not evidence anyone can act on.
    """
    rows = catalog.files_for_content(content_id)
    return [
        os.path.join(catalog.dir_path(int(r["dir_id"]), os.sep), r["name"])
        for r in rows[:limit]
    ]


def group_paths(catalog: Catalog, group: ContentGroup, *, limit: int = 10) -> list[str]:
    """Where a group lives, whether it has a content identity or is a single file."""
    if group.content_id is not None:
        return content_paths(catalog, group.content_id, limit=limit)
    row = catalog.conn.execute(
        "SELECT dir_id, name FROM file WHERE id = ?", (group.file_id,)
    ).fetchone()
    if row is None:
        return []
    return [os.path.join(catalog.dir_path(int(row["dir_id"]), os.sep), row["name"])]


def _other_copies(catalog: Catalog, content_id: int) -> int:
    row = catalog.conn.execute(
        f"SELECT {DRIVES_PROVEN} AS n FROM file f "
        "JOIN volume v ON v.id = f.volume_id "
        "WHERE f.content_id = ? AND f.vanished_at IS NULL",
        (content_id,),
    ).fetchone()
    return int(row["n"])
