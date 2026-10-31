"""Placement units: what the planner places (PLAN §9, ROADMAP M7.2, D16).

Planning per file over 50M files is intractable and unreviewable, so the
planner places **units**, each one folder's worth of content on one volume:

* each folder of a confirmed collection (a collection merged from several
  folders gives one unit per folder, so every unit has one place it lives);
* unfiled content, cut from the folder tree top down: a subtree becomes one
  unit once its unfiled bytes fall under a threshold sized to give about
  ``TARGET_UNITS`` units; a folder too big for that becomes a "loose files"
  unit for the files directly in it, and its subfolders are cut in turn.

Each unit carries its files and bytes, its **footprint** on each cluster size
the destinations use, its **holders** (the proven drives holding a copy of
every item in it), its P(loss) over those holders (M5.5) and its copy
**target**: a collection's ``min_copies``; else 3 when its NDSA target is 2
or more; else 2 (NDSA level 1: two complete copies in separate locations).

One pass over the live files builds all of it; nothing per file is kept.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from drivefusion.core.health import afr, durability, smart
from drivefusion.core.store import curation

TARGET_UNITS = 2000
#: A unit of unfiled content is never cut finer than this.
MIN_UNIT_BYTES = 1 << 20
DEFAULT_TARGET = 2


@dataclass
class Unit:
    key: str
    kind: str                       # collection | folder | loose
    label: str
    dir_id: int                     # the folder it is
    volume_id: int
    collection_id: int | None = None
    target: int = DEFAULT_TARGET
    files: int = 0
    bytes: int = 0
    items: int = 0
    holders: frozenset = frozenset()   # proven drives holding every item
    footprint: dict[int, int] = field(default_factory=dict)   # cluster -> bytes
    p_loss: float = 0.0

    @property
    def copies(self) -> int:
        """Proven complete copies; a unit on an unverified drive is still one."""
        return max(1, len(self.holders))


def target_for(row) -> int:
    if row is None:
        return DEFAULT_TARGET
    if row["min_copies"]:
        return int(row["min_copies"])
    if row["ndsa_target"] and row["ndsa_target"] >= 2:
        return 3
    return DEFAULT_TARGET


def build(catalog, *, clusters=(4096,), target_units: int = TARGET_UNITS,
          today=None) -> list[Unit]:
    conn = catalog.conn
    dirs = {r["id"]: (r["parent_id"], r["volume_id"], r["name"]) for r in conn.execute(
        "SELECT id, parent_id, volume_id, name FROM dir WHERE vanished_at IS NULL")}
    children: dict[int, list[int]] = defaultdict(list)
    for d, (parent, _v, _n) in dirs.items():
        if parent in dirs:
            children[parent].append(d)
    direct_bytes: dict[int, int] = defaultdict(int)
    for r in conn.execute("SELECT dir_id, SUM(size_bytes) AS b FROM file "
                          "WHERE vanished_at IS NULL GROUP BY dir_id"):
        direct_bytes[r["dir_id"]] = r["b"] or 0

    # Folders of confirmed collections, and everything under them.
    unit_of: dict[int, str] = {}
    units: dict[str, Unit] = {}
    for coll in curation.collections(catalog):
        for rule in curation.rules(catalog, coll["id"]):
            if rule["dir_id"] not in dirs:
                continue
            key = f"c{coll['id']}:{rule['dir_id']}"
            units[key] = Unit(key, "collection", coll["title"], rule["dir_id"],
                              dirs[rule["dir_id"]][1], coll["id"], target_for(coll))
            stack = [rule["dir_id"]]
            while stack:
                d = stack.pop()
                unit_of[d] = key
                stack.extend(children[d])

    # Unfiled subtree sizes, then cut top down.
    unfiled: dict[int, int] = {}
    for d in sorted(dirs, key=lambda d: -_depth(d, dirs)):
        if d in unit_of:
            unfiled[d] = 0
            continue
        unfiled[d] = direct_bytes[d] + sum(unfiled.get(c, 0) for c in children[d])
    total = sum(unfiled[d] for d, (parent, _v, _n) in dirs.items() if parent not in dirs)
    threshold = max(total // max(1, target_units), MIN_UNIT_BYTES)

    def path(d: int) -> str:
        return catalog.dir_path(d)

    def claim(d: int, key: str) -> None:
        stack = [d]
        while stack:
            current = stack.pop()
            if current in unit_of:
                continue
            unit_of[current] = key
            stack.extend(children[current])

    stack = [d for d, (parent, _v, _n) in dirs.items() if parent not in dirs]
    while stack:
        d = stack.pop()
        if unfiled[d] == 0 or d in unit_of:
            continue
        if unfiled[d] <= threshold or not children[d]:
            key = f"f{d}"
            units[key] = Unit(key, "folder", path(d), d, dirs[d][1])
            claim(d, key)
            continue
        if direct_bytes[d]:
            key = f"l{d}"
            units[key] = Unit(key, "loose", f"{path(d)} (files directly in it)", d, dirs[d][1])
            unit_of[d] = key
        stack.extend(children[d])

    # One pass over the live files: counts, footprints, and the drives that
    # hold a copy of every item.
    content_sets, _single = durability.drive_sets(catalog)
    holders: dict[str, set | None] = {}
    seen_items: dict[str, set] = defaultdict(set)
    for row in conn.execute(
        "SELECT f.id, f.dir_id, f.content_id, f.size_bytes, v.drive_id FROM file f "
        "JOIN volume v ON v.id = f.volume_id WHERE f.vanished_at IS NULL"
    ):
        key = unit_of.get(row["dir_id"])
        if key is None:
            continue
        unit = units[key]
        size = row["size_bytes"] or 0
        unit.files += 1
        unit.bytes += size
        for cluster in clusters:
            unit.footprint[cluster] = unit.footprint.get(cluster, 0) + (
                -(-size // cluster) * cluster if size else 0)
        item = row["content_id"] if row["content_id"] is not None else ("f", row["id"])
        if item in seen_items[key]:
            continue
        seen_items[key].add(item)
        held = (set(content_sets.get(row["content_id"], ())) if row["content_id"] is not None
                else ({row["drive_id"]} if row["drive_id"] is not None else set()))
        holders[key] = held if holders.get(key) is None else holders[key] & held

    params = afr.model()
    tables = afr.tables(catalog)
    drive_rows = {r["id"]: r for r in conn.execute("SELECT * FROM drive")}
    risks = {d: afr.drive_risk(row, today=today, health=smart.latest(catalog, d),
                               rate_tables=tables, params=params)
             for d, row in drive_rows.items()}
    unverified = afr.drive_risk(durability._UNVERIFIED_DRIVE, today=today, rate_tables=tables,
                                params=params)
    out = []
    for key, unit in units.items():
        if not unit.bytes:
            continue
        unit.items = len(seen_items[key])
        unit.holders = frozenset(holders.get(key) or ())
        found = durability.set_risk(tuple(sorted(unit.holders)), risks, drive_rows, params,
                                    unverified)
        unit.p_loss = found.p_loss
        out.append(unit)
    out.sort(key=lambda u: -u.bytes * u.p_loss)
    return out


def _depth(d: int, dirs: dict) -> int:
    depth = 0
    while dirs[d][0] in dirs:
        d = dirs[d][0]
        depth += 1
    return depth
