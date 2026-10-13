"""Redundancy and duplication analysis.

The counting rule that matters, and the one that is easy to get wrong:
**redundancy is counted over distinct physical drives, not over paths.**

* Two paths on the same drive are one copy for durability purposes. If that
  drive dies, both are gone together.
* Two paths sharing an inode or file reference number are *the same file* seen
  twice — a hardlink is not a second copy of anything, and counting it as one
  would inflate the number that the whole tool exists to report.
* A volume with no identified physical drive counts as its own drive, so an
  unmapped disk is never silently merged with another.

Everything here is a read-only query. Reclamation candidates are evidence for
a human, never an instruction (docs/PLAN.md §3.3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from drivefusion.core.store.catalog import Catalog

#: A volume whose physical drive is unknown is treated as its own device.
#: Negating the volume id keeps it from colliding with a real drive id.
#:
#: Written against the alias ``v``. Use :func:`drive_key` for any query that
#: aliases the volume table differently — notably a subquery. Dropping this
#: constant into a subquery that calls its volume ``v2`` leaves the expression
#: pointing at the *outer* ``v``, and an aggregate whose arguments all come
#: from the outer query is evaluated as an aggregate *of* the outer query:
#: SQLite silently collapses the whole result to one row. That cost an
#: afternoon, and the inventory CSV it broke had one line where it should have
#: had thirty-six.
DRIVE_KEY = "COALESCE(v.drive_id, -v.id)"


def drive_key(alias: str = "v") -> str:
    """The copy-counting drive key, against a given volume alias."""
    return f"COALESCE({alias}.drive_id, -{alias}.id)"

_LIVE = "f.vanished_at IS NULL AND f.content_id IS NOT NULL"


@dataclass(frozen=True)
class ContentGroup:
    content_id: int
    size_bytes: int
    paths: int
    drives: int
    physical_copies: int

    @property
    def reclaimable_bytes(self) -> int:
        """Bytes freed if every duplicate *within* a drive were removed.

        Cross-drive copies are the point of the exercise and are never counted
        as waste; only extra copies on the same device are.
        """
        return max(0, self.physical_copies - self.drives) * self.size_bytes


def _group_rows(catalog: Catalog, having: str = "", params: tuple = ()):
    """One row per content item, with path, drive, and physical-copy counts."""
    return list(
        catalog.conn.execute(
            "SELECT c.id AS content_id, c.size_bytes, "
            "  COUNT(f.id) AS paths, "
            f"  COUNT(DISTINCT {DRIVE_KEY}) AS drives, "
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
        )
        for r in rows
    ]


def duplicate_groups(catalog: Catalog, *, limit: int | None = None) -> list[ContentGroup]:
    """Content that exists at more than one path, anywhere."""
    groups = _as_groups(_group_rows(catalog, "COUNT(f.id) > 1"))
    return groups[:limit] if limit else groups


def under_protected(
    catalog: Catalog,
    *,
    min_copies: int = 2,
    limit: int | None = None,
    by_risk: bool = True,
) -> list[ContentGroup]:
    """Content held on fewer distinct drives than the policy requires.

    Ranked by **size × P(loss)** since M5, which is a different list from the
    one size alone produces: by size the top is whatever is biggest, usually
    video on a healthy drive; by expected loss the top is whatever sits on the
    drive with pending sectors, which may be far smaller and is the thing that
    will actually be gone next month. Only one of those is a work queue.

    ``by_risk=False`` falls back to the size ranking, and so does a catalog with
    no health data — where every drive's probability is the same estimate, the
    two orderings coincide anyway.
    """
    groups = _as_groups(
        _group_rows(catalog, f"COUNT(DISTINCT {DRIVE_KEY}) < ?", (min_copies,))
    )

    if by_risk and groups:
        # Imported here: analysis.exposure imports the health package, which
        # imports this module for DRIVE_KEY. Deferring the import keeps the
        # cycle from forming at module load.
        from drivefusion.core.analysis.exposure import RiskIndex

        index = RiskIndex(catalog)
        weights = _risk_weights(catalog, index, [g.content_id for g in groups])
        groups.sort(
            key=lambda g: g.size_bytes * weights.get(g.content_id, 1.0), reverse=True
        )
    else:
        groups.sort(key=lambda g: g.size_bytes * max(1, g.paths), reverse=True)

    return groups[:limit] if limit else groups


def _risk_weights(catalog: Catalog, index, content_ids: list[int]) -> dict[int, float]:
    """P(loss) per content id, for ranking. One query, not one per row."""
    if not content_ids:
        return {}

    placeholders = ",".join("?" * len(content_ids))
    rows = catalog.conn.execute(
        "SELECT f.content_id, "
        f"  GROUP_CONCAT(DISTINCT {DRIVE_KEY}) AS drive_keys "
        "FROM file f JOIN volume v ON v.id = f.volume_id "
        f"WHERE f.vanished_at IS NULL AND f.content_id IN ({placeholders}) "
        "GROUP BY f.content_id",
        content_ids,
    )

    weights: dict[int, float] = {}
    for row in rows:
        keys = tuple(
            int(part) for part in (row["drive_keys"] or "").split(",") if part.strip()
        )
        probability, _confidence = index.exposure_for(keys)
        weights[int(row["content_id"])] = probability
    return weights


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
        if group.physical_copies > group.drives
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

    unhashed = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file "
        "WHERE vanished_at IS NULL AND content_id IS NULL"
    ).fetchone()["n"]

    exposed = under_protected(catalog, min_copies=min_copies)
    reclaim = reclamation_candidates(catalog)

    return {
        "distinct_content": int(row["distinct_content"]),
        "catalogued_paths": int(row["catalogued_paths"]),
        "path_bytes": int(row["path_bytes"]),
        "unique_bytes": int(unique_bytes),
        "duplicate_overhead_bytes": int(row["path_bytes"]) - int(unique_bytes),
        "under_protected_items": len(exposed),
        "under_protected_bytes": sum(g.size_bytes for g in exposed),
        "reclaimable_bytes": sum(g.reclaimable_bytes for g in reclaim),
        # Coverage, stated rather than implied: analysis only describes what
        # has been hashed, and a summary over a partly-hashed catalog would
        # otherwise read as a summary over all of it.
        "unhashed_files": int(unhashed),
    }


def integrity_incidents(catalog: Catalog, *, limit: int = 100) -> list[dict]:
    """Fixity failures: silent corruption and files that became unreadable."""
    rows = catalog.conn.execute(
        "SELECT fc.result, fc.checked_at, f.dir_id, f.name, f.size_bytes, "
        "       fc.content_id "
        "FROM fixity_check fc JOIN file f ON f.id = fc.file_id "
        "WHERE fc.result != 'ok' "
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


def _other_copies(catalog: Catalog, content_id: int) -> int:
    row = catalog.conn.execute(
        f"SELECT COUNT(DISTINCT {DRIVE_KEY}) AS n FROM file f "
        "JOIN volume v ON v.id = f.volume_id "
        "WHERE f.content_id = ? AND f.vanished_at IS NULL",
        (content_id,),
    ).fetchone()
    return int(row["n"])
