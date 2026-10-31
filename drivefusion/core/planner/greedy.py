"""The greedy planner: an explainable baseline (PLAN §9, ROADMAP M7.4).

Units are taken riskiest first (bytes × P(loss)). Each gets copies added
until it meets its target: every time, the admissible destination that
leaves the unit with the lowest P(loss), ties going to a drive already being
connected for this plan, then to the one with the most room left. A
destination is admissible when

* it doesn't already hold the unit and isn't excluded (``capacity.py``);
* the unit's **footprint** at that destination's cluster size fits its room;
* it keeps **batch diversity** and **zone separation**: no more than
  ``target - 1`` of the unit's copies in one purchase batch or one recorded
  disaster zone. Drives with no zone recorded aren't grouped by this rule
  (they may well be apart); P(loss) already counts them as together.

When nothing admissible is left the unit is **short**, by so many copies and
bytes: the plan's ``acquire`` step. Then a swap pass moves *added* copies to a
destination that lowers their unit's P(loss) if one has room. Existing copies
are pinned throughout: holders only grow, and the planner cannot express a
state where a copy is gone.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from drivefusion.core.planner.capacity import Destination
from drivefusion.core.planner.units import Unit


@dataclass
class Added:
    unit: str
    drive_id: int
    footprint: int
    bytes: int


@dataclass
class Solution:
    added: list[Added] = field(default_factory=list)
    short: dict[str, tuple[int, int]] = field(default_factory=dict)  # key -> (copies, bytes)
    holders: dict[str, frozenset] = field(default_factory=dict)       # after the plan
    room_left: dict[int, int] = field(default_factory=dict)
    p_before: dict[str, float] = field(default_factory=dict)
    p_after: dict[str, float] = field(default_factory=dict)

    @property
    def drives_touched(self) -> set[int]:
        return {a.drive_id for a in self.added}


def _admissible(unit: Unit, holders: set, dest: Destination, room: dict[int, int],
                by_id: dict[int, Destination]) -> bool:
    if dest.excluded or dest.drive_id in holders:
        return False
    if unit.footprint.get(dest.cluster_bytes, unit.bytes) > room[dest.drive_id]:
        return False
    limit = max(1, unit.target - 1)
    known = [by_id[h] for h in holders if h in by_id]
    if dest.batch_key and Counter(d.batch_key for d in known)[dest.batch_key] >= limit:
        return False
    if dest.zone and Counter(d.zone for d in known)[dest.zone] >= limit:
        return False
    return True


def solve(units: list[Unit], destinations: list[Destination],
          p_of: Callable[[frozenset], float]) -> Solution:
    by_id = {d.drive_id: d for d in destinations}
    room = {d.drive_id: d.room for d in destinations}
    out = Solution()
    touched: set[int] = set()
    holders = {u.key: set(u.holders) for u in units}

    for unit in units:
        out.p_before[unit.key] = unit.p_loss
        need = unit.target - unit.copies
        while need > 0:
            options = [d for d in destinations
                       if _admissible(unit, holders[unit.key], d, room, by_id)]
            if not options:
                break
            best = min(options, key=lambda d: (
                p_of(frozenset(holders[unit.key] | {d.drive_id})),
                d.drive_id not in touched, -room[d.drive_id]))
            cost = unit.footprint.get(best.cluster_bytes, unit.bytes)
            room[best.drive_id] -= cost
            holders[unit.key].add(best.drive_id)
            touched.add(best.drive_id)
            out.added.append(Added(unit.key, best.drive_id, cost, unit.bytes))
            need -= 1
        if need > 0:
            out.short[unit.key] = (need, need * unit.bytes)

    # Swap pass: an added copy may move where its unit is safer, if it fits.
    units_by_key = {u.key: u for u in units}
    for added in out.added:
        unit = units_by_key[added.unit]
        others = holders[unit.key] - {added.drive_id}
        current = p_of(frozenset(holders[unit.key]))
        room[added.drive_id] += added.footprint
        candidates = [d for d in destinations if d.drive_id != added.drive_id
                      and _admissible(unit, others, d, room, by_id)]
        better = min(candidates, key=lambda d: p_of(frozenset(others | {d.drive_id})),
                     default=None)
        if better is not None and p_of(frozenset(others | {better.drive_id})) < current:
            holders[unit.key] = others | {better.drive_id}
            added.drive_id = better.drive_id
            added.footprint = unit.footprint.get(better.cluster_bytes, unit.bytes)
        room[added.drive_id] -= added.footprint

    for unit in units:
        out.holders[unit.key] = frozenset(holders[unit.key])
        out.p_after[unit.key] = (p_of(out.holders[unit.key]) if holders[unit.key] != unit.holders
                                 else unit.p_loss)
    out.room_left = room
    return out


def risk_function(catalog, *, today=None) -> Callable[[frozenset], float]:
    """P(loss) of a set of drives, as M5.5 computes it, for the solver."""
    from drivefusion.core.health import afr, durability, smart

    params = afr.model()
    tables = afr.tables(catalog)
    drives = {r["id"]: r for r in catalog.conn.execute("SELECT * FROM drive")}
    risks = {d: afr.drive_risk(row, today=today, health=smart.latest(catalog, d),
                               rate_tables=tables, params=params) for d, row in drives.items()}
    unverified = afr.drive_risk(durability._UNVERIFIED_DRIVE, today=today, rate_tables=tables,
                                params=params)
    cache: dict[frozenset, float] = {}

    def p_of(holders: frozenset) -> float:
        if holders not in cache:
            cache[holders] = durability.set_risk(tuple(sorted(holders)), risks, drives, params,
                                                 unverified).p_loss
        return cache[holders]
    return p_of
