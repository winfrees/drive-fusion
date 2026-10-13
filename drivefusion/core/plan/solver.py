"""The solver: greedy seed, hill climb, CP-SAT when available.

**The solver can only add placements.** Every existing copy is pinned, and the
representation has no way to express a unit *not* being on a drive it is already
on: `assignments[unit]` starts as the set of drives holding it and is only ever
unioned with. That is the formal expression of read-only operation — no plan can
imply a deletion, because no reachable state in this module represents one.

A capacity shortfall therefore surfaces as **"you need another N TB"** — an
``acquire`` finding — rather than being resolved by quietly dropping a copy.
That is the honest answer and it is also the only one available.

Greedy first, always: riskiest and largest units placed first, then a hill climb
over add/swap neighbourhoods. CP-SAT runs only as an improvement on that
baseline, warm-started from it, so the optimiser can never do worse than a
result a person can follow, and an archive without ``ortools`` installed loses
nothing but a few percent of objective quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from drivefusion.core.analysis.exposure import RiskIndex
from drivefusion.core.health import risk as risk_module
from drivefusion.core.plan.units import Unit, footprint
from drivefusion.core.policy.rules import Policy


@dataclass
class Destination:
    """A volume the planner may place onto."""

    volume_id: int
    drive_key: int
    name: str
    cluster_bytes: int
    capacity_bytes: int
    free_bytes: int
    batch_key: str | None = None
    disaster_zone: str | None = None
    fs_type: str | None = None
    #: Bytes this plan has already committed here.
    committed: int = 0

    def remaining(self, policy: Policy) -> int:
        """Space a plan may still use, after the reserve.

        Based on free space where it is known, and on capacity-minus-reserve
        otherwise: a drive whose free space was never recorded is not assumed
        empty.
        """
        if self.free_bytes:
            headroom = int(self.free_bytes) - int(
                self.capacity_bytes * policy.reserve_fraction
            )
        else:
            headroom = policy.usable_bytes(self.capacity_bytes)
        return max(0, headroom - self.committed)


@dataclass
class Shortfall:
    """Why a unit could not reach its copy target. An acquire finding."""

    unit_key: str
    label: str
    needed_copies: int
    achieved_copies: int
    bytes_needed: int
    reason: str


@dataclass
class Solution:
    """What the solver decided. Additions only."""

    #: unit key -> drive keys holding it after the plan (a superset of before).
    assignments: dict[str, set[int]] = field(default_factory=dict)
    #: unit key -> list of (volume_id, footprint_bytes) this plan adds.
    additions: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    shortfalls: list[Shortfall] = field(default_factory=list)
    #: (unit key, volume id) -> why this destination was second best. A copy
    #: placed beside its siblings in one purchase batch still reduces risk, so
    #: it is planned rather than refused, but the plan has to say so: the
    #: correlation model (§8.2) prices co-batch copies well above independent
    #: ones, and a user comparing two plans needs to see which one bought
    #: genuine separation.
    concessions: dict[tuple[str, int], str] = field(default_factory=dict)
    #: (unit key, volume id) pairs that go beyond the policy's copy target.
    #: Worth having — durability dominates the objective — but a plan proposing
    #: three copies under a two-copy policy has to say which copy is the extra
    #: one, or it reads as the policy being ignored.
    beyond_target: set[tuple[str, int]] = field(default_factory=set)
    solver: str = "greedy"
    objective: dict = field(default_factory=dict)

    def bytes_copied(self) -> int:
        return sum(
            size for places in self.additions.values() for _volume, size in places
        )

    def drives_touched(self) -> set[int]:
        return {
            volume for places in self.additions.values() for volume, _size in places
        }


def _unit_risk(unit: Unit, index: RiskIndex, drive_keys: set[int]) -> float:
    risks = [index.risk_for(key) for key in drive_keys]
    rows = [index.drive_row(key) for key in drive_keys]
    return risk_module.loss_probability(risks, rows, table=index.table)


def expected_loss(units: list[Unit], solution: Solution, index: RiskIndex) -> float:
    """Expected bytes lost per year under a set of assignments."""
    total = 0.0
    for unit in units:
        keys = solution.assignments.get(unit.key, set(unit.drive_keys))
        total += unit.bytes * _unit_risk(unit, index, keys)
    return total


def _attributes(
    drive_key: int, destinations: list[Destination], index: RiskIndex
) -> tuple[str | None, str | None]:
    """A drive's batch and zone, whether or not it is a destination here.

    Existing copies may sit on drives this plan would never write to — excluded
    by the user, or holding no mountable destination volume — so the batch and
    zone come from the drive record when no destination describes them.
    """
    for destination in destinations:
        if destination.drive_key == drive_key:
            return destination.batch_key, destination.disaster_zone
    row = index.drive_row(drive_key)
    return row.get("batch_key"), row.get("disaster_zone")


def correlation_note(
    drive_keys: set[int] | tuple[int, ...],
    unit: Unit,
    policy: Policy,
    destinations: list[Destination],
    index: RiskIndex,
) -> str | None:
    """Why a set of copies is too alike to count as independent, if it is.

    Judged over the final set, so it serves both the choice (via
    :func:`_diversity_concession`) and the scoring of a finished solution. A
    plan whose copies are correlated has bought less durability than its copy
    count suggests, and the objective has to see that or an optimiser will
    trade real separation for a cheaper placement.
    """
    keys = set(drive_keys)
    if not policy.require_batch_diversity or len(keys) < 2:
        return None

    attributes = [_attributes(key, destinations, index) for key in keys]

    batches = [batch for batch, _zone in attributes if batch]
    if len(batches) == len(keys) and len(set(batches)) == 1:
        return (
            f"every copy would sit on a drive from purchase batch {batches[0]}; "
            "drives bought together tend to fail together, so this counts for "
            "less than a copy on a drive from a different batch"
        )

    wanted_zones = policy.zones_for(unit.ndsa_target)
    zones = [zone for _batch, zone in attributes if zone]
    if wanted_zones > 1 and len(zones) == len(keys) and len(set(zones)) < wanted_zones:
        return (
            f"every copy would be in {len(set(zones))} location(s) but the "
            f"policy asks for {wanted_zones}; a fire or theft that reaches one "
            "reaches them all"
        )
    return None


def correlated_units(
    units: list[Unit],
    solution: "Solution",
    policy: Policy,
    destinations: list[Destination],
    index: RiskIndex,
) -> int:
    """Units whose copies all share a batch or a zone, under this solution."""
    total = 0
    for unit in units:
        keys = solution.assignments.get(unit.key, set(unit.drive_keys))
        if correlation_note(keys, unit, policy, destinations, index):
            total += 1
    return total


def _diversity_concession(
    destination: Destination,
    current: set[int],
    chosen: list[Destination],
    unit: Unit,
    policy: Policy,
    required: int,
    index: RiskIndex,
) -> str | None:
    """Why this destination would leave the copies too alike, if it would.

    Judged over **every** copy the unit would then have, including the ones it
    already has. Judging only the copies the solver itself places would make
    the rule dead code in the normal case: this tool never moves anything, so a
    unit almost always arrives with one copy already in place, and a rule that
    waits until it is choosing all of them would happily put the second copy on
    the drive from the same order sitting on the same shelf. The correlation
    model (§8.2) is explicit that those are not two independent copies.

    Returned as a reason rather than a veto. With twenty drives bought in a few
    orders a hard rule is often infeasible, and a second copy in the same batch
    is still much better than no second copy — so the planner takes it and the
    plan says what was given up.
    """
    if not policy.require_batch_diversity or required < 2:
        return None

    future = set(current) | {destination.drive_key}
    if len(future) < required:
        # Not yet at the copy target: the shortfall machinery covers that, and
        # flagging it here would describe a half-built plan.
        return None
    return correlation_note(future, unit, policy, chosen + [destination], index)


def _candidate_score(
    unit: Unit,
    destination: Destination,
    index: RiskIndex,
    current: set[int],
    policy: Policy,
) -> float:
    """Lower is better: the objective change from placing a unit here."""
    size = footprint(unit, destination.cluster_bytes)
    after = current | {destination.drive_key}

    before_risk = _unit_risk(unit, index, current)
    after_risk = _unit_risk(unit, index, after)
    durability_gain = unit.bytes * (before_risk - after_risk)

    # Prefer a destination that is not already holding this unit's risk, is
    # roomy, and is in a different zone. Imbalance uses the share of the drive
    # consumed, so a small drive is not filled first just because it is cheap.
    remaining = destination.remaining(policy)
    imbalance = (size / remaining) if remaining > 0 else 1.0

    return (
        -policy.weight_durability * durability_gain
        + policy.weight_bytes_copied * size
        + policy.weight_imbalance * imbalance * unit.bytes
    )


def solve_greedy(
    units: list[Unit],
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
) -> Solution:
    """Place the riskiest, largest units first. Explainable by construction."""
    solution = Solution()
    for unit in units:
        # The starting point is where the unit already is. There is no code path
        # that removes a key from this set.
        solution.assignments[unit.key] = set(unit.drive_keys)
        solution.additions[unit.key] = []

    usable = [d for d in destinations if d.drive_key not in policy.excluded_drives]

    def urgency(unit: Unit) -> tuple:
        keys = set(unit.drive_keys)
        risk = _unit_risk(unit, index, keys)
        required = policy.copies_for(unit.ndsa_target)
        deficit = max(0, required - len(keys))
        return (-deficit, -(unit.bytes * risk), -unit.bytes)

    for unit in sorted(units, key=urgency):
        required = policy.copies_for(unit.ndsa_target)
        current = solution.assignments[unit.key]
        chosen: list[Destination] = []

        while len(current) < required:
            options = [
                destination
                for destination in usable
                if destination.drive_key not in current
                and destination.remaining(policy)
                >= footprint(unit, destination.cluster_bytes)
            ]
            # Diversity steers the choice; it does not veto one. Where some
            # destination gives genuine separation, only those are considered;
            # where none does, the best remaining drive is used and the
            # concession is recorded against the step.
            concessions = {
                destination.volume_id: _diversity_concession(
                    destination, current, chosen, unit, policy, required, index
                )
                for destination in options
            }
            diverse = [d for d in options if not concessions[d.volume_id]]
            if diverse:
                options = diverse
            if not options:
                needed = min(
                    (footprint(unit, d.cluster_bytes) for d in usable),
                    default=unit.bytes,
                )
                solution.shortfalls.append(
                    Shortfall(
                        unit_key=unit.key,
                        label=unit.label,
                        needed_copies=required,
                        achieved_copies=len(current),
                        bytes_needed=needed,
                        reason=_shortfall_reason(unit, usable, current, policy),
                    )
                )
                break

            best = min(
                options,
                key=lambda d: _candidate_score(unit, d, index, current, policy),
            )
            size = footprint(unit, best.cluster_bytes)
            best.committed += size
            current.add(best.drive_key)
            chosen.append(best)
            solution.additions[unit.key].append((best.volume_id, size))
            concession = concessions.get(best.volume_id)
            if concession:
                solution.concessions[(unit.key, best.volume_id)] = concession

    solution.objective = objective_breakdown(units, solution, destinations, policy, index)
    return solution


def _shortfall_reason(
    unit: Unit, usable: list[Destination], current: set[int], policy: Policy
) -> str:
    """Why no destination worked, in words a person can act on."""
    if not usable:
        return "no destination drives are available"

    elsewhere = [d for d in usable if d.drive_key not in current]
    if not elsewhere:
        return (
            "every available drive already holds a copy, so another drive is "
            "needed for the next one"
        )

    fits = [
        d
        for d in elsewhere
        if d.remaining(policy) >= footprint(unit, d.cluster_bytes)
    ]
    if not fits:
        smallest = min(footprint(unit, d.cluster_bytes) for d in elsewhere)
        largest_free = max(d.remaining(policy) for d in elsewhere)
        detail = ""
        if smallest > unit.bytes:
            # The exFAT trap, named where it bites rather than left as a
            # mysterious "does not fit".
            detail = (
                f" ({smallest - unit.bytes:,} bytes of that is cluster slack on "
                "the destination's filesystem)"
            )
        return (
            f"needs {smallest:,} bytes{detail} but the roomiest available drive "
            f"has {largest_free:,} after its reserve"
        )

    return (
        "the remaining drives would leave every copy in one purchase batch or "
        "one location, which is not independent storage"
    )


def objective_breakdown(
    units: list[Unit],
    solution: Solution,
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
) -> dict:
    """The objective, itemised, so a plan can show its own arithmetic.

    Every weighted term is a **fraction of the archive**, and durability is
    valued over the policy's horizon. That normalisation is not cosmetic: the
    raw figures are in different units — expected loss is bytes *per year*,
    bytes copied is bytes *once* — and summing them directly prices a one-off
    copy an order of magnitude above the annual loss it prevents. An objective
    built that way prefers doing nothing to every plan, which a solver driven by
    the objective will faithfully deliver.

    The raw figures are reported alongside, because they are what a person
    reads; the normalised ones are what gets compared.
    """
    before = expected_loss(units, Solution(
        assignments={u.key: set(u.drive_keys) for u in units}
    ), index)
    after = expected_loss(units, solution, index)

    copied = solution.bytes_copied()
    touched = len(solution.drives_touched())
    total_bytes = sum(unit.bytes for unit in units) or 1

    shares = []
    for destination in destinations:
        usable = policy.usable_bytes(destination.capacity_bytes)
        if usable:
            shares.append(destination.committed / usable)
    spread = (max(shares) - min(shares)) if shares else 0.0

    correlated = correlated_units(units, solution, policy, destinations, index)
    unmet_bytes = unmet_target_bytes(units, solution, policy)

    components = {
        # Fraction of the archive expected to be lost over the horizon.
        "durability": policy.horizon_years * after / total_bytes,
        "bytes_copied": copied / total_bytes,
        "drives_touched": touched / max(1, len(destinations)),
        "imbalance": spread,
        # Share of the archive still short of its copy target, weighted by how
        # short it is: one copy of three missing is less exposed than two.
        "unmet": unmet_bytes / total_bytes,
    }

    return {
        "expected_loss_before": before,
        "expected_loss_after": after,
        "expected_loss_reduction": before - after,
        "horizon_years": policy.horizon_years,
        "bytes_copied": copied,
        "drives_touched": touched,
        "imbalance": spread,
        "unmet_copy_targets": len(solution.shortfalls),
        "unmet_bytes": unmet_bytes,
        #: Units whose copies all share a purchase batch or a location.
        #: Reported, not separately priced: the risk model already charges for
        #: correlation through corr(D) in §8.2, so the durability term carries
        #: it. A second penalty here would bill the same fault twice.
        "correlated_copies": correlated,
        "total_bytes": total_bytes,
        "components": components,
        #: The tie-break *among plans that meet the policy equally well*. Unmet
        #: copy targets are deliberately not a term here: :func:`is_better`
        #: compares them first and separately, because no weight can make a
        #: constraint out of a term. A price here would change no outcome while
        #: reading as though it did.
        "weighted": (
            policy.weight_durability * components["durability"]
            + policy.weight_bytes_copied * components["bytes_copied"]
            + policy.weight_drives_touched * components["drives_touched"]
            + policy.weight_imbalance * components["imbalance"]
        ),
    }


def unmet_target_bytes(
    units: list[Unit], solution: Solution, policy: Policy
) -> int:
    """Archive bytes still short of their copy target, weighted by the shortfall.

    Computed from the assignments rather than from ``solution.shortfalls``, so
    it is the same number whichever solver produced the solution and whether or
    not that solver bothered to record a shortfall.
    """
    total = 0
    for unit in units:
        required = policy.copies_for(unit.ndsa_target)
        if required < 1:
            continue
        achieved = len(solution.assignments.get(unit.key, set(unit.drive_keys)))
        deficit = max(0, required - achieved)
        if deficit:
            total += int(unit.bytes * deficit / required)
    return total


def hill_climb(
    units: list[Unit],
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
    solution: Solution,
    *,
    rounds: int = 2,
) -> Solution:
    """Add a copy where it buys the most durability, while space allows.

    Only ever *adds*: there is no neighbourhood operator that removes a
    placement, which is what keeps "the solver may only add" true of the search
    as well as of the seed.
    """
    by_key = {unit.key: unit for unit in units}
    usable = [d for d in destinations if d.drive_key not in policy.excluded_drives]

    for _round in range(rounds):
        best_move = None
        best_gain = 0.0

        for key, drives in solution.assignments.items():
            unit = by_key[key]
            risk_now = _unit_risk(unit, index, drives)
            for destination in usable:
                if destination.drive_key in drives:
                    continue
                size = footprint(unit, destination.cluster_bytes)
                if destination.remaining(policy) < size:
                    continue

                after = drives | {destination.drive_key}
                gain = unit.bytes * (risk_now - _unit_risk(unit, index, after))
                cost = policy.weight_bytes_copied * size
                if gain * policy.weight_durability - cost > best_gain:
                    best_gain = gain * policy.weight_durability - cost
                    best_move = (key, destination, size)

        if best_move is None:
            break

        key, destination, size = best_move
        unit = by_key[key]
        required = policy.copies_for(unit.ndsa_target)
        if len(solution.assignments[key]) >= required:
            solution.beyond_target.add((key, destination.volume_id))
        concession = _diversity_concession(
            destination, solution.assignments[key], [], unit, policy, required, index
        )
        if concession:
            solution.concessions[(key, destination.volume_id)] = concession
        solution.assignments[key].add(destination.drive_key)
        solution.additions[key].append((destination.volume_id, size))
        destination.committed += size

    solution.objective = objective_breakdown(
        units, solution, destinations, policy, index
    )
    return solution


def solve(
    units: list[Unit],
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
    *,
    use_cp_sat: bool = True,
    time_limit_seconds: float = 20.0,
) -> Solution:
    """Greedy, improved by hill climbing, then CP-SAT if it is installed."""
    solution = solve_greedy(units, destinations, policy, index)
    solution = hill_climb(units, destinations, policy, index, solution)

    if use_cp_sat:
        improved = _solve_cp_sat(
            units, destinations, policy, index, solution, time_limit_seconds
        )
        if improved is not None and is_better(improved, solution):
            return improved
    return solution


def is_better(candidate: Solution, incumbent: Solution) -> bool:
    """Whether a candidate solution should replace the incumbent.

    Lexicographic, and deliberately so: **first** no more of the archive left
    short of its copy target, **then** the weighted objective. The copy target
    is a constraint the user set, not a term to be traded off, and no weighting
    can make it one — whatever price copying is given in the objective, a user
    can set one higher, and a purely weighted comparison would then adopt a
    plan that leaves data unprotected because protecting it looked expensive.
    """
    candidate_unmet = candidate.objective.get("unmet_bytes", 0)
    incumbent_unmet = incumbent.objective.get("unmet_bytes", 0)
    if candidate_unmet != incumbent_unmet:
        return candidate_unmet < incumbent_unmet
    return candidate.objective.get("weighted", float("inf")) < incumbent.objective.get(
        "weighted", float("inf")
    )


def _solve_cp_sat(
    units: list[Unit],
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
    seed: Solution,
    time_limit_seconds: float,
) -> Solution | None:
    """CP-SAT over the same model, warm-started from the greedy solution.

    Returns ``None`` when ortools is absent or finds nothing better, so the
    caller keeps the explainable baseline. The optimiser is an improvement step,
    never the only path to an answer.
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None

    model = cp_model.CpModel()
    usable = [d for d in destinations if d.drive_key not in policy.excluded_drives]
    if not usable or not units:
        return None

    variables: dict[tuple[str, int], object] = {}
    for unit in units:
        for position, destination in enumerate(usable):
            variable = model.NewBoolVar(f"x[{unit.key}][{position}]")
            variables[(unit.key, position)] = variable

            # Every existing copy is pinned to 1. This is the constraint that
            # makes a deletion unrepresentable rather than merely discouraged.
            if destination.drive_key in unit.drive_keys:
                model.Add(variable == 1)

    # Capacity, in cluster-rounded footprint rather than logical bytes.
    for position, destination in enumerate(usable):
        existing = sum(
            footprint(unit, destination.cluster_bytes)
            for unit in units
            if destination.drive_key in unit.drive_keys
        )
        budget = policy.usable_bytes(destination.capacity_bytes) - existing
        if budget <= 0:
            continue
        terms = [
            footprint(unit, destination.cluster_bytes)
            * variables[(unit.key, position)]
            for unit in units
            if destination.drive_key not in unit.drive_keys
        ]
        if terms:
            model.Add(sum(terms) <= budget)

    # Copy targets, as soft constraints: an infeasible model would tell the user
    # nothing, where a shortfall tells them how much storage to buy.
    penalties = []
    usable_keys = {destination.drive_key for destination in usable}
    for unit in units:
        required = policy.copies_for(unit.ndsa_target)
        # Copies on drives this plan cannot write to — excluded by the user, or
        # with no destination volume here — are still copies. Counting only the
        # ones that happen to be destinations would make CP-SAT demand the full
        # copy count again while the greedy path, which seeds from every
        # existing location, asks for one fewer: the same catalog would get a
        # different plan depending on whether ortools was installed.
        held_elsewhere = len(set(unit.drive_keys) - usable_keys)
        placed = held_elsewhere + sum(
            variables[(unit.key, p)] for p in range(len(usable))
        )
        deficit = model.NewIntVar(0, required, f"deficit[{unit.key}]")
        model.Add(deficit >= required - placed)
        penalties.append((unit, deficit))

    # Diversity, soft, for the same reason the copy target is soft. Modelled
    # explicitly rather than left to the objective: without it CP-SAT would
    # ignore the rule the greedy path enforces, and the plan a user got would
    # depend on whether an optional package happened to be installed.
    correlated_flags = _diversity_penalties(
        model, variables, units, usable, policy, index
    )

    # Objective: the same normalised terms as objective_breakdown, scaled to
    # integers because CP-SAT is integral. It has to be the same function: a
    # solver that optimises one objective and is then judged on another either
    # looks worse than it is or, as happened here, confidently returns a plan
    # with no copies in it.
    scale = 1_000_000_000
    total_bytes = sum(unit.bytes for unit in units) or 1
    terms = []

    def scaled(fraction: float) -> int:
        return int(round(fraction * scale))

    for unit in units:
        for position, destination in enumerate(usable):
            if destination.drive_key in unit.drive_keys:
                continue
            size = footprint(unit, destination.cluster_bytes)
            after = set(unit.drive_keys) | {destination.drive_key}
            gain = unit.bytes * (
                _unit_risk(unit, index, set(unit.drive_keys))
                - _unit_risk(unit, index, after)
            )
            cost = policy.weight_bytes_copied * size / total_bytes
            benefit = (
                policy.weight_durability * policy.horizon_years * gain / total_bytes
            )
            terms.append(scaled(cost - benefit) * variables[(unit.key, position)])

    # One missing copy of a unit costs its share of the archive, divided by the
    # copies required — matching unmet_target_bytes exactly, so the two agree.
    for unit, deficit in penalties:
        required = max(1, policy.copies_for(unit.ndsa_target))
        share = unit.bytes / required / total_bytes
        terms.append(scaled(policy.weight_unmet_target * share) * deficit)

    # Correlation is not priced again here; the durability term already carries
    # it through corr(D). These flags exist so the *choice* between equally
    # cheap destinations goes to the one that buys real separation, which the
    # linearised per-pair gain above cannot see.
    for penalty in correlated_flags:
        terms.append(scaled(policy.weight_durability * 0.001) * penalty)

    model.Minimize(sum(terms))

    for unit in units:
        for position, destination in enumerate(usable):
            hint = 1 if destination.drive_key in seed.assignments.get(unit.key, ()) else 0
            model.AddHint(variables[(unit.key, position)], hint)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    candidate = Solution(solver="cp-sat")
    for destination in destinations:
        destination.committed = 0

    for unit in units:
        keys = set(unit.drive_keys)
        additions: list[tuple[int, int]] = []
        for position, destination in enumerate(usable):
            if destination.drive_key in unit.drive_keys:
                continue
            if solver.Value(variables[(unit.key, position)]):
                size = footprint(unit, destination.cluster_bytes)
                keys.add(destination.drive_key)
                additions.append((destination.volume_id, size))
                destination.committed += size
        candidate.assignments[unit.key] = keys
        candidate.additions[unit.key] = additions

        # The same notes the greedy path records. Without these a CP-SAT plan
        # would silently lose both the "shares a purchase batch" caveat and the
        # "this copy is beyond the target" label, and would then claim a target
        # of two while listing three copies.
        required = policy.copies_for(unit.ndsa_target)
        note = correlation_note(keys, unit, policy, usable, index)
        for position, (volume_id, _size) in enumerate(additions):
            if len(unit.drive_keys) + position >= required:
                candidate.beyond_target.add((unit.key, volume_id))
            if note:
                candidate.concessions[(unit.key, volume_id)] = note

    for unit in units:
        required = policy.copies_for(unit.ndsa_target)
        achieved = len(candidate.assignments[unit.key])
        if achieved < required:
            candidate.shortfalls.append(
                Shortfall(
                    unit_key=unit.key,
                    label=unit.label,
                    needed_copies=required,
                    achieved_copies=achieved,
                    bytes_needed=min(
                        (footprint(unit, d.cluster_bytes) for d in usable),
                        default=unit.bytes,
                    ),
                    reason=_shortfall_reason(
                        unit, usable, candidate.assignments[unit.key], policy
                    ),
                )
            )

    candidate.objective = objective_breakdown(
        units, candidate, destinations, policy, index
    )

    # Keep whichever is better, so the optimiser can only improve on a result a
    # person can follow — and "better" means the copy target first.
    if is_better(candidate, seed):
        return candidate
    return None


def _diversity_penalties(
    model,
    variables: dict,
    units: list[Unit],
    usable: list[Destination],
    policy: Policy,
    index: RiskIndex,
) -> list:
    """Soft batch and zone spread, as deficit variables CP-SAT pays for.

    For each unit and each attribute, one indicator per distinct value says
    whether some copy lands in that group. A drive the unit already sits on
    pins its group to 1, so existing copies count exactly as they do on the
    greedy path. The deficit is how far the number of groups falls short, and
    it is penalised rather than forbidden: with twenty drives from a few orders
    a hard rule is often infeasible, and an infeasible model tells the user
    nothing.
    """
    if not policy.require_batch_diversity:
        return []

    penalties = []
    for unit in units:
        if policy.copies_for(unit.ndsa_target) < 2:
            continue

        for position_in_attributes, (attribute, wanted) in enumerate(
            (("batch_key", 2), ("disaster_zone", policy.zones_for(unit.ndsa_target)))
        ):
            if wanted < 2:
                continue

            groups: dict[str, list[int]] = {}
            for position, destination in enumerate(usable):
                value = getattr(destination, attribute)
                if value:
                    groups.setdefault(value, []).append(position)

            held: set[str] = set()
            for drive_key in unit.drive_keys:
                value = _attributes(drive_key, usable, index)[position_in_attributes]
                if value:
                    held.add(value)
                    groups.setdefault(value, [])

            if len(groups) < 2:
                # Every candidate shares the attribute, so no choice here can
                # improve matters. The concession is recorded on the step
                # instead of penalised, or every plan would carry a penalty the
                # user has no way to act on.
                continue

            flags = []
            for value, positions in groups.items():
                flag = model.NewBoolVar(f"group[{unit.key}][{attribute}][{value}]")
                if value in held:
                    model.Add(flag == 1)
                elif positions:
                    model.AddMaxEquality(
                        flag, [variables[(unit.key, p)] for p in positions]
                    )
                else:
                    model.Add(flag == 0)
                flags.append(flag)

            deficit = model.NewIntVar(0, wanted, f"spread[{unit.key}][{attribute}]")
            model.Add(deficit >= wanted - sum(flags))
            penalties.append(deficit)

    return penalties


def assert_additive(units: list[Unit], solution: Solution) -> None:
    """Every original placement must survive. Called before a plan is written.

    Belt and braces over a representation that already cannot express a
    removal: if a future refactor introduces one, this fails loudly at the point
    a plan would have been produced rather than quietly emitting a document that
    implies deleting someone's data.
    """
    for unit in units:
        after = solution.assignments.get(unit.key, set())
        missing = set(unit.drive_keys) - after
        if missing:
            raise AssertionError(
                f"solver dropped existing placements for {unit.key}: {sorted(missing)}. "
                "A plan may only add copies."
            )
