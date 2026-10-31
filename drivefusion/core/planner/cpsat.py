"""CP-SAT over the greedy plan, when ``ortools`` is installed (PLAN §9, ROADMAP M7.5, D15).

The same choice as ``greedy.py`` (which unit gets a copy where) as an integer
program: a boolean per unit and admissible destination, room by footprint per
destination, and no more copies than a unit's target needs. P(loss) is not
linear, so the model maximises a surrogate: each copy placed counts first,
then the unit's bytes × the fall in P(loss) that copy alone would bring (a
drive sharing a batch or zone with a copy brings less: that is how D18
makes them costlier rather than forbidden), then one drive fewer to connect.

The greedy plan is the hint, and the answer is judged on the real numbers:
CP-SAT's plan is taken only when it leaves fewer copies short, or as few
and less expected loss. So it can only improve on the explainable baseline,
and ``solver`` says which plan this is.
"""

from __future__ import annotations

from typing import Callable

from drivefusion.core.planner import greedy
from drivefusion.core.planner.capacity import Destination
from drivefusion.core.planner.greedy import Added, Solution
from drivefusion.core.planner.units import Unit

#: Seconds CP-SAT may search; the greedy plan stands if it finds nothing better.
TIME_LIMIT = 10.0
#: The weight of one copy placed; the risk term is scaled below it.
COPY_WEIGHT = 1 << 20


def available() -> bool:
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except ImportError:
        return False
    return True


def expected_loss(units: list[Unit], solution: Solution) -> float:
    """Bytes expected lost per year after the plan."""
    return sum(u.bytes * solution.p_after[u.key] for u in units)


def short_copies(solution: Solution) -> int:
    return sum(n for n, _ in solution.short.values())


def _rank(units, solution) -> tuple:
    return (short_copies(solution), expected_loss(units, solution), len(solution.drives_touched))


def solve(units: list[Unit], destinations: list[Destination],
          p_of: Callable[[frozenset], float], *, time_limit: float = TIME_LIMIT,
          seed: int = 0) -> tuple[Solution, str]:
    """The better of the greedy plan and CP-SAT's, and which it is."""
    baseline = greedy.solve(units, destinations, p_of)
    if not available() or not units:
        return baseline, "greedy"
    found = _cp(units, destinations, p_of, baseline, time_limit, seed)
    if found is not None and _rank(units, found) < _rank(units, baseline):
        return found, "cp-sat"
    return baseline, "greedy"


def _cp(units, destinations, p_of, baseline, time_limit, seed) -> Solution | None:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    by_id = {d.drive_id: d for d in destinations}
    open_dests = [d for d in destinations if not d.excluded and d.room > 0]
    x: dict[tuple[str, int], object] = {}
    value: dict[tuple[str, int], float] = {}
    for u in units:
        need = u.target - u.copies
        if need <= 0:
            continue
        p_now = p_of(frozenset(u.holders))
        # Only drives holding none of the unit's items, so that every copy
        # placed gives every item one more (greedy also weighs the others).
        anywhere = set().union(*u.holder_sets) if u.holder_sets else set(u.holders)
        for d in open_dests:
            fp = u.footprint.get(d.cluster_bytes, u.bytes)
            if d.drive_id in anywhere or fp > d.room:
                continue
            x[u.key, d.drive_id] = model.NewBoolVar(f"x[{u.key},{d.drive_id}]")
            value[u.key, d.drive_id] = u.bytes * max(0.0, p_now - p_of(
                frozenset(u.holders | {d.drive_id})))
        mine = [(d, x[u.key, d.drive_id]) for d in open_dests if (u.key, d.drive_id) in x]
        if not mine:
            continue
        model.Add(sum(v for _, v in mine) <= need)
    if not x:
        return None

    units_by_key = {u.key: u for u in units}
    used = {d.drive_id: model.NewBoolVar(f"y[{d.drive_id}]") for d in open_dests}
    for d in open_dests:
        terms = [(units_by_key[k].footprint.get(d.cluster_bytes, units_by_key[k].bytes), v)
                 for (k, drive), v in x.items() if drive == d.drive_id]
        if not terms:
            model.Add(used[d.drive_id] == 0)
            continue
        model.Add(sum(fp * v for fp, v in terms) <= d.room)
        for _, v in terms:
            model.AddImplication(v, used[d.drive_id])

    top = max(value.values()) or 1.0
    scale = (COPY_WEIGHT - 2) / top
    model.Maximize(sum((COPY_WEIGHT + int(value[k] * scale)) * v for k, v in x.items())
                   - sum(used.values()))

    chosen = {(a.unit, a.drive_id) for a in baseline.added}
    for k, v in x.items():
        model.AddHint(v, k in chosen)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.random_seed = seed
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    out = Solution()
    room = {d.drive_id: d.room for d in destinations}
    holders = {u.key: set(u.holders) for u in units}
    for (key, drive), v in x.items():
        if solver.BooleanValue(v):
            u = units_by_key[key]
            fp = u.footprint.get(by_id[drive].cluster_bytes, u.bytes)
            room[drive] -= fp
            holders[key].add(drive)
            out.added.append(Added(key, drive, fp, u.bytes))
    for u in units:
        out.p_before[u.key] = u.p_loss
        out.holders[u.key] = frozenset(holders[u.key])
        out.p_after[u.key] = (p_of(out.holders[u.key]) if holders[u.key] != u.holders
                              else u.p_loss)
        need = u.target - u.copies_with(holders[u.key] - u.holders)
        if need > 0:
            out.short[u.key] = (need, need * u.bytes)
    out.room_left = room
    greedy.mark_crowding(units, destinations, out)
    return out
