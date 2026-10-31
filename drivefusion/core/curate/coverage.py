"""How much of the archive is curated, and what the rest risks (PLAN §8.4, ROADMAP M6.4).

Unfiled content is a first-class, reported state: at 50M files most bytes
start unfiled, and a scorecard over 12 % of the archive must not read as a
scorecard over all of it. So every collection figure comes with coverage.

The items and bytes are exactly those of the redundancy and durability
figures (``core/analysis/duplicates.py``): each distinct content item once,
plus every file whose size no other file shares, at its own size. An item is
**filed** when any live copy of it lies in a confirmed collection's folders,
and is then counted in each collection holding a copy. Coverage is filed
bytes over all distinct bytes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from drivefusion.core.analysis.duplicates import _LIVE, _SINGLE_COPY
from drivefusion.core.health import durability

#: Every live folder under a confirmed collection's subtree rules, with the
#: collection it belongs to.
_FILED_DIRS = (
    "WITH RECURSIVE tree(id, coll) AS ("
    "  SELECT dir_id, collection_id FROM collection_rule WHERE kind = 'subtree' "
    "  UNION SELECT d.id, t.coll FROM dir d JOIN tree t ON d.parent_id = t.id "
    "  WHERE d.vanished_at IS NULL) "
)


@dataclass
class Bucket:
    items: int = 0
    bytes: int = 0
    expected_loss: float = 0.0           # bytes a year (M5.5)
    under_protected_items: int = 0
    under_protected_bytes: int = 0

    def add(self, size: int, p_loss: float, under: bool) -> None:
        self.items += 1
        self.bytes += size
        self.expected_loss += size * p_loss
        if under:
            self.under_protected_items += 1
            self.under_protected_bytes += size


@dataclass
class Coverage:
    total: Bucket
    filed: Bucket
    unfiled: Bucket
    collections: dict[int, Bucket] = field(default_factory=dict)
    min_copies: int = 2

    @property
    def share(self) -> float:
        """Filed bytes over all distinct bytes; 0 for an empty catalog."""
        return self.filed.bytes / self.total.bytes if self.total.bytes else 0.0


def coverage(catalog, *, min_copies: int = 2, found=None) -> Coverage:
    found = found or durability.exposure(catalog)
    p_loss = {s.drive_ids: s.p_loss for s in found.sets}
    content_sets, file_sets = durability.drive_sets(catalog)

    in_content: dict[int, set[int]] = defaultdict(set)
    for row in catalog.conn.execute(
        _FILED_DIRS + "SELECT DISTINCT f.content_id AS item, t.coll FROM file f "
        f"JOIN tree t ON t.id = f.dir_id WHERE {_LIVE}"
    ):
        in_content[row["item"]].add(row["coll"])
    in_file: dict[int, set[int]] = defaultdict(set)
    for row in catalog.conn.execute(
        _FILED_DIRS + "SELECT f.id AS item, t.coll FROM file f "
        f"JOIN tree t ON t.id = f.dir_id WHERE {_SINGLE_COPY}"
    ):
        in_file[row["item"]].add(row["coll"])

    sizes = {r["id"]: r["size_bytes"] for r in catalog.conn.execute(
        "SELECT id, size_bytes FROM content")}
    single_sizes = {r["id"]: r["size_bytes"] for r in catalog.conn.execute(
        f"SELECT f.id, f.size_bytes FROM file f WHERE {_SINGLE_COPY}")}

    result = Coverage(Bucket(), Bucket(), Bucket(), min_copies=min_copies)
    for keys, item_sizes, holders in ((content_sets, sizes, in_content),
                                      (file_sets, single_sizes, in_file)):
        for item, key in keys.items():
            size = item_sizes[item]
            loss = p_loss.get(key, 0.0)
            # Proven copies, as the redundancy figures count them: at least one.
            under = max(1, len(key)) < min_copies
            result.total.add(size, loss, under)
            colls = holders.get(item)
            (result.filed if colls else result.unfiled).add(size, loss, under)
            for coll in colls or ():
                result.collections.setdefault(coll, Bucket()).add(size, loss, under)
    return result
