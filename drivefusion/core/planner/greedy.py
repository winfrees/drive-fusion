"""The greedy planner: an explainable baseline (PLAN §9, ROADMAP M7.4).

Units are taken riskiest first (bytes × P(loss)). Each gets copies added
until it meets its target: every time, the admissible destination that
leaves the unit with the lowest P(loss), ties going to a drive already being
connected for this plan, then to the one with the most room left. A
destination is admissible when

* it doesn't already hold the unit and isn't excluded (``capacity.py``);
* the unit's **footprint** at that destination's cluster size fits its room.

Batch diversity and zone separation are costs, not rules (D18): P(loss)
already counts drives of one purchase batch, and drives in one recorded zone,
as failing together, so a diverse drive wins whenever it is the safer place.
But a second copy on a drive from the same batch beats no second copy, so
where nothing diverse has room the copy is still planned, and marked: an
added copy that leaves more than ``target - 1`` of the unit's copies in one
batch or one recorded zone carries what it shares in ``Added.crowds``.

A unit's copies are counted per item, the fewest any item has, so a copy to
a drive that already holds some of the unit's items is only counted when it
still gives every least-copied item another drive. When nothing admissible is
left the unit is **short**, by so many copies and bytes: the plan's
``acquire`` step. Then a swap pass moves *added* copies to a
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
    #: What this copy shares with too many of the unit's other copies, e.g.
    #: ("batch", "maker|model|2021-03"); empty when it keeps them apart.
    crowds: tuple = ()


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
    return unit.footprint.get(dest.cluster_bytes, unit.bytes) <= room[dest.drive_id]


def crowding(unit: Unit, holders, dest: Destination, by_id: dict[int, Destination]) -> tuple:
    """What a copy on ``dest`` shares with more than ``target - 1`` of the
    unit's copies (``holders`` includes ``dest``): a purchase batch, a zone."""
    limit = max(1, unit.target - 1)
    known = [by_id[h] for h in holders if h in by_id]
    out = []
    for attr, label in (("batch_key", "batch"), ("zone", "zone")):
        value = getattr(dest, attr)
        if value and Counter(getattr(d, attr) for d in known)[value] > limit:
            out += [label, value]
    return tuple(out)


def mark_crowding(units: list[Unit], destinations: list[Destination],
                  solution: "Solution") -> None:
    by_id = {d.drive_id: d for d in destinations}
    units_by_key = {u.key: u for u in units}
    for a in solution.added:
        a.crowds = crowding(units_by_key[a.unit], solution.holders[a.unit], by_id[a.drive_id],
                            by_id)


def solve(units: list[Unit], destinations: list[Destination],
          p_of: Callable[[frozenset], float]) -> Solution:
    by_id = {d.drive_id: d for d in destinations}
    room = {d.drive_id: d.room for d in destinations}
    out = Solution()
    touched: set[int] = set()
    added: dict[str, set] = {u.key: set() for u in units}

    def holders(u: Unit) -> set:
        return set(u.holders) | added[u.key]

    for unit in units:
        out.p_before[unit.key] = unit.p_loss
        while unit.copies_with(added[unit.key]) < unit.target:
            now = unit.copies_with(added[unit.key])
            # Only a drive that gives every least-copied item another copy counts.
            options = [d for d in destinations
                       if _admissible(unit, holders(unit), d, room, by_id)
                       and unit.copies_with(added[unit.key] | {d.drive_id}) > now]
            if not options:
                break
            best = min(options, key=lambda d: (
                p_of(frozenset(holders(unit) | {d.drive_id})),
                d.drive_id not in touched, -room[d.drive_id]))
            cost = unit.footprint.get(best.cluster_bytes, unit.bytes)
            room[best.drive_id] -= cost
            added[unit.key].add(best.drive_id)
            touched.add(best.drive_id)
            out.added.append(Added(unit.key, best.drive_id, cost, unit.bytes))
        need = unit.target - unit.copies_with(added[unit.key])
        if need > 0:
            out.short[unit.key] = (need, need * unit.bytes)

    # Swap pass: an added copy may move where its unit is safer, if it fits
    # and the unit keeps as many copies.
    units_by_key = {u.key: u for u in units}
    for a in out.added:
        unit = units_by_key[a.unit]
        kept = unit.copies_with(added[unit.key])
        rest = added[unit.key] - {a.drive_id}
        others = set(unit.holders) | rest
        current = p_of(frozenset(holders(unit)))
        room[a.drive_id] += a.footprint
        candidates = [d for d in destinations if d.drive_id != a.drive_id
                      and _admissible(unit, others, d, room, by_id)
                      and unit.copies_with(rest | {d.drive_id}) >= kept]
        better = min(candidates, key=lambda d: p_of(frozenset(others | {d.drive_id})),
                     default=None)
        if better is not None and p_of(frozenset(others | {better.drive_id})) < current:
            added[unit.key] = rest | {better.drive_id}
            a.drive_id = better.drive_id
            a.footprint = unit.footprint.get(better.cluster_bytes, unit.bytes)
        room[a.drive_id] -= a.footprint

    for unit in units:
        out.holders[unit.key] = frozenset(holders(unit))
        out.p_after[unit.key] = (p_of(out.holders[unit.key]) if added[unit.key]
                                 else unit.p_loss)
    out.room_left = room
    mark_crowding(units, destinations, out)
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
