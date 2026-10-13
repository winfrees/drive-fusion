"""The plan document: a dependency-ordered checklist, not an operation.

A plan's terminal state is "exported", never "applied" (§3.3). There is no
executor anywhere in this codebase, and the step vocabulary itself cannot
express a destructive action: ``copy``, ``organize``, ``describe``, ``verify``,
``review-candidate``, ``acquire``. There is no ``move`` and no ``delete``, so a
plan cannot say "remove this" even by accident.

Every step carries a plain-language rationale and its ``risk_delta``, because a
checklist you cannot interrogate is a checklist you should not follow. The
document also carries a before/after simulation — per-drive free space, bytes to
copy, wall clock from measured throughput, and the change in expected bytes lost
per year — so the cost and the benefit are visible together.

Reclamation candidates are appended as a **segregated appendix**, evidence only,
never as steps. Mixing "you could free 400 GB" into an actionable checklist is
how a planning tool becomes a deletion tool by inches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from drivefusion.core.analysis.duplicates import reclamation_candidates
from drivefusion.core.analysis.exposure import RiskIndex
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.fair import ndsa, scorecard
from drivefusion.core.plan import units as units_module
from drivefusion.core.plan.solver import Destination, Solution, assert_additive, solve
from drivefusion.core.policy.rules import Policy
from drivefusion.core import wording
from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store.schema import utcnow

#: Step kinds. Deliberately exhaustive and deliberately missing every verb that
#: would remove or relocate data.
STEP_KINDS = (
    "copy",               # duplicate a unit onto another drive
    "organize",           # a proposed old -> new path mapping, for you to run
    "describe",           # fill metadata a scorecard found missing
    "verify",             # re-hash to confirm a copy landed intact
    "review-candidate",   # confirm or decline a proposed collection
    "acquire",            # buy storage: the honest answer to a shortfall
)

#: Conservative sustained throughput for wall-clock estimates, in bytes/second.
#: USB-attached spinning disks are the common case in this fleet and 60 MB/s is
#: what they actually sustain on mixed content, not what the bus is rated for.
ASSUMED_BYTES_PER_SECOND = 60 * 1024 * 1024


@dataclass
class Step:
    seq: int
    kind: str
    rationale: str
    unit_key: str | None = None
    src_volume_id: int | None = None
    dst_volume_id: int | None = None
    dst_path: str | None = None
    collection_id: int | None = None
    bytes: int = 0
    footprint_bytes: int = 0
    depends_on: tuple[int, ...] = ()
    risk_delta: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in STEP_KINDS:
            raise ValueError(
                f"{self.kind!r} is not a plan step kind. The vocabulary is "
                f"{', '.join(STEP_KINDS)} — there is no move or delete kind, "
                "because a plan cannot imply one."
            )


@dataclass
class Plan:
    name: str
    policy: Policy
    steps: list[Step] = field(default_factory=list)
    simulation: dict = field(default_factory=dict)
    objective: dict = field(default_factory=dict)
    solver: str = "greedy"
    #: Evidence, not steps. Kept apart so it can never be executed by accident.
    reclamation_appendix: list[dict] = field(default_factory=list)
    scorecards_before: list[dict] = field(default_factory=list)
    scorecards_after: list[dict] = field(default_factory=list)
    standards: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    #: Set by :func:`save`. An unsaved plan has none, and an export that
    #: includes one can then mark exactly that row 'exported' (§3.3) rather
    #: than guessing which stored plan it was.
    plan_id: int | None = None

    @property
    def bytes_to_copy(self) -> int:
        return sum(s.footprint_bytes for s in self.steps if s.kind == "copy")

    @property
    def copy_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "copy"]

    @property
    def acquire_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "acquire"]

    def to_json(self) -> str:
        """JSON so plans diff against later plans and archive as provenance."""
        return json.dumps(
            {
                "name": self.name,
                "created_at": self.created_at,
                "solver": self.solver,
                "policy": json.loads(self.policy.snapshot()),
                "standards": self.standards,
                "objective": self.objective,
                "simulation": self.simulation,
                "steps": [
                    {
                        "seq": s.seq,
                        "kind": s.kind,
                        "rationale": s.rationale,
                        "unit_key": s.unit_key,
                        "src_volume_id": s.src_volume_id,
                        "dst_volume_id": s.dst_volume_id,
                        "dst_path": s.dst_path,
                        "collection_id": s.collection_id,
                        "bytes": s.bytes,
                        "footprint_bytes": s.footprint_bytes,
                        "depends_on": list(s.depends_on),
                        "risk_delta": s.risk_delta,
                    }
                    for s in self.steps
                ],
                "scorecards_before": self.scorecards_before,
                "scorecards_after": self.scorecards_after,
                "reclamation_appendix": self.reclamation_appendix,
                "note": (
                    "This is a document, not an operation. Drive Fusion has no "
                    "executor: every step is something you perform with your "
                    "own tools. No step kind removes or relocates data."
                ),
            },
            indent=2,
        )


def _destinations(catalog: Catalog, policy: Policy) -> list[Destination]:
    out = []
    for row in units_module.volume_capacity(catalog):
        out.append(
            Destination(
                volume_id=int(row["volume_id"]),
                drive_key=int(row["drive_key"]),
                name=row["name"],
                cluster_bytes=int(row["cluster_bytes"]),
                capacity_bytes=int(row["capacity_bytes"] or 0),
                free_bytes=int(row["free_bytes"] or 0),
                batch_key=row["batch_key"],
                disaster_zone=row["disaster_zone"],
                fs_type=row["fs_type"],
            )
        )
    return out


def build(
    catalog: Catalog,
    *,
    name: str | None = None,
    policy: Policy | None = None,
    use_cp_sat: bool = True,
    target_bytes: int = units_module.UNIT_TARGET_BYTES,
) -> Plan:
    """Solve, then turn the solution into a document a person can follow."""
    policy = policy or Policy.for_level()
    index = RiskIndex(catalog)
    units = units_module.placement_units(catalog, target_bytes=target_bytes)
    destinations = _destinations(catalog, policy)
    by_volume = {d.volume_id: d for d in destinations}

    solution = solve(units, destinations, policy, index, use_cp_sat=use_cp_sat)
    return from_solution(
        catalog,
        units=units,
        solution=solution,
        policy=policy,
        index=index,
        destinations=destinations,
        name=name,
    )


def from_solution(
    catalog: Catalog,
    *,
    units: list[units_module.Unit],
    solution: Solution,
    policy: Policy,
    index: RiskIndex,
    destinations: list[Destination],
    name: str | None = None,
) -> Plan:
    """Turn a solved placement into a document, whatever produced it.

    Separate from :func:`build` so a solution can be written up independently
    of how it was found — which is what lets the document's wording be tested
    against a constructed solution rather than against whatever the search
    happens to settle on.
    """
    by_volume = {d.volume_id: d for d in destinations}

    # Checked before anything is written: a plan that dropped a copy must never
    # reach a document, however it came to be.
    assert_additive(units, solution)

    plan = Plan(
        name=name or f"Plan {utcnow()[:16]}",
        policy=policy,
        solver=solution.solver,
        objective=solution.objective,
        standards={
            "ndsa": ndsa.standards.ndsa_levels().citation,
            "fair": scorecard.standards.fair_rubric().citation,
        },
    )

    plan.scorecards_before = _scorecards(catalog)
    _add_copy_steps(plan, units, solution, by_volume, index, policy)
    _add_acquire_steps(plan, solution)
    _add_curation_steps(plan, catalog)
    plan.simulation = _simulate(catalog, units, solution, destinations, policy, index)
    plan.scorecards_after = _projected_scorecards(catalog, plan)
    plan.reclamation_appendix = _reclamation(catalog)

    # Read what was written before handing it over. Every rationale in this
    # module is composed from templates, so this should never fire — which is
    # the point: if it does, a template has started telling someone to remove
    # data, and that must not reach a user as a numbered instruction.
    wording.assert_safe(plan.to_json(), what="plan")
    return plan


def _drive_key_for(volume_id: int, by_volume: dict, fallback: set[int]) -> int:
    """The drive key a destination volume contributes a copy to."""
    destination = by_volume.get(volume_id)
    if destination is not None:
        return destination.drive_key
    # A volume with no destination record cannot be identified with a physical
    # drive, so it counts as its own, using the negative-key convention.
    return -int(volume_id)


def _add_copy_steps(
    plan: Plan,
    units: list[units_module.Unit],
    solution: Solution,
    by_volume: dict[int, Destination],
    index: RiskIndex,
    policy: Policy,
) -> None:
    from drivefusion.core.plan.solver import _unit_risk

    by_key = {unit.key: unit for unit in units}
    ordered: list[tuple[float, units_module.Unit, int, int]] = []

    for key, additions in solution.additions.items():
        unit = by_key.get(key)
        if unit is None:
            continue

        # The *marginal* gain of each copy, taken in order, not the gain of the
        # whole plan repeated against every step. Attributing the full benefit
        # to each copy would have two steps each claiming the entire reduction,
        # so the per-step figures would sum to several times what the plan
        # actually delivers — and someone deciding whether to skip the third
        # copy would be told it protects data the second copy already protects.
        held = set(unit.drive_keys)
        risk = _unit_risk(unit, index, held)
        for volume_id, size in additions:
            after_keys = held | {_drive_key_for(volume_id, by_volume, held)}
            after_risk = _unit_risk(unit, index, after_keys)
            ordered.append((unit.bytes * (risk - after_risk), unit, volume_id, size))
            held, risk = after_keys, after_risk

    # Biggest durability gain first: a checklist someone abandons halfway should
    # already have done the most valuable work. A stable sort, so each unit's
    # copies keep the order their marginal gains were computed in.
    ordered.sort(key=lambda item: item[0], reverse=True)

    for gain, unit, volume_id, size in ordered:
        destination = by_volume.get(volume_id)
        name = destination.name if destination else f"volume {volume_id}"
        slack_bytes = (
            units_module.slack(unit, destination.cluster_bytes) if destination else 0
        )

        required = policy.copies_for(unit.ndsa_target)
        if (unit.key, volume_id) in solution.beyond_target:
            rationale = (
                f"{unit.label} already reaches its {required}-copy target; this "
                f"extra copy on {name} is proposed because it still reduces "
                f"expected loss by about {gain:,.0f} bytes/year, which outweighs "
                "the cost of copying it. It can be dropped without breaking the "
                "policy."
            )
        else:
            rationale = (
                f"{unit.label} is on {unit.copies} drive(s); policy asks for "
                f"{required}. Copying it to {name} reduces expected loss by "
                f"about {gain:,.0f} bytes/year."
            )
        if slack_bytes > unit.bytes * 0.1:
            # Named rather than left as an unexplained discrepancy between the
            # unit's size and the space it needs.
            rationale += (
                f" It needs {size:,} bytes there rather than {unit.bytes:,} "
                f"because {name} has {destination.cluster_bytes:,}-byte clusters."
            )

        concession = solution.concessions.get((unit.key, volume_id))
        if concession:
            # A compromise the user should see while comparing plans, not one
            # buried in the objective value.
            rationale += f" Note: {concession}."

        plan.steps.append(
            Step(
                seq=len(plan.steps) + 1,
                kind="copy",
                rationale=rationale,
                unit_key=unit.key,
                src_volume_id=None,
                dst_volume_id=volume_id,
                dst_path=unit.label,
                collection_id=unit.collection_id,
                bytes=unit.bytes,
                footprint_bytes=size,
                risk_delta=-gain,
            )
        )
        # Every copy is followed by a verify that depends on it: a copy nobody
        # checked is a copy you do not know you have.
        plan.steps.append(
            Step(
                seq=len(plan.steps) + 1,
                kind="verify",
                rationale=(
                    f"Re-hash {unit.label} on {name} to confirm the copy landed "
                    "intact before relying on it."
                ),
                unit_key=unit.key,
                dst_volume_id=volume_id,
                bytes=unit.bytes,
                depends_on=(len(plan.steps),),
            )
        )


def _add_acquire_steps(plan: Plan, solution: Solution) -> None:
    """Capacity shortfalls, as the honest answer rather than a quiet removal."""
    if not solution.shortfalls:
        return

    total = sum(s.bytes_needed for s in solution.shortfalls)
    for shortfall in solution.shortfalls:
        plan.steps.append(
            Step(
                seq=len(plan.steps) + 1,
                kind="acquire",
                rationale=(
                    f"{shortfall.label} has {shortfall.achieved_copies} of "
                    f"{shortfall.needed_copies} required copies and no drive "
                    f"can take another: {shortfall.reason}. About "
                    f"{shortfall.bytes_needed:,} bytes of new storage would "
                    "close this. Drive Fusion will not propose deleting "
                    "anything to make room."
                ),
                unit_key=shortfall.unit_key,
                bytes=shortfall.bytes_needed,
                footprint_bytes=shortfall.bytes_needed,
            )
        )

    plan.steps.append(
        Step(
            seq=len(plan.steps) + 1,
            kind="acquire",
            rationale=(
                f"In total about {total:,} bytes of additional storage would "
                f"bring {len(solution.shortfalls)} unit(s) up to their copy "
                "target."
            ),
            bytes=total,
            footprint_bytes=total,
        )
    )


def _add_curation_steps(plan: Plan, catalog: Catalog) -> None:
    """Describe and review steps from what the scorecards found missing."""
    from drivefusion.core.curate import candidates as candidates_module

    for collection in collections_module.all_collections(catalog):
        card = scorecard.score(catalog, collection.id)
        for failure in card.failures:
            if failure.id in ("F2", "R1", "R1.1", "I2"):
                plan.steps.append(
                    Step(
                        seq=len(plan.steps) + 1,
                        kind="describe",
                        rationale=(
                            f"{collection.title}: {failure.gap} {failure.remedy}"
                        ),
                        collection_id=collection.id,
                    )
                )

    queue = candidates_module.pending(catalog, limit=20)
    for row in queue:
        plan.steps.append(
            Step(
                seq=len(plan.steps) + 1,
                kind="review-candidate",
                rationale=(
                    f"'{row['proposed_title']}' ({row['files']:,} files) is "
                    "proposed as a collection and awaiting your decision. "
                    "Confirming it lets the planner place it as a unit and lets "
                    "it be described and scored."
                ),
                bytes=int(row["bytes"]),
            )
        )


def _simulate(
    catalog: Catalog,
    units: list[units_module.Unit],
    solution: Solution,
    destinations: list[Destination],
    policy: Policy,
    index: RiskIndex,
) -> dict:
    """Before and after, in the terms a person decides on."""
    from drivefusion.core.plan.solver import expected_loss

    before_solution = Solution(
        assignments={unit.key: set(unit.drive_keys) for unit in units}
    )
    before = expected_loss(units, before_solution, index)
    after = expected_loss(units, solution, index)

    copied = solution.bytes_copied()
    per_drive = []
    for destination in destinations:
        free_before = destination.free_bytes or policy.usable_bytes(
            destination.capacity_bytes
        )
        per_drive.append(
            {
                "volume_id": destination.volume_id,
                "name": destination.name,
                "fs_type": destination.fs_type,
                "cluster_bytes": destination.cluster_bytes,
                "capacity_bytes": destination.capacity_bytes,
                "free_before": free_before,
                "free_after": max(0, free_before - destination.committed),
                "bytes_added": destination.committed,
            }
        )

    return {
        "units": len(units),
        "bytes_to_copy": copied,
        "drives_touched": len(solution.drives_touched()),
        "estimated_hours": round(copied / ASSUMED_BYTES_PER_SECOND / 3600, 2),
        "throughput_assumption": (
            f"{ASSUMED_BYTES_PER_SECOND // (1024 * 1024)} MB/s sustained, which "
            "is what USB-attached spinning disks actually manage on mixed "
            "content rather than the bus rating"
        ),
        "expected_loss_before": before,
        "expected_loss_after": after,
        "expected_loss_reduction": before - after,
        "unmet_copy_targets": len(solution.shortfalls),
        "per_drive": per_drive,
        "policy": policy.describe(),
    }


def _scorecards(catalog: Catalog) -> list[dict]:
    out = []
    for collection in collections_module.all_collections(catalog):
        card = scorecard.score(catalog, collection.id)
        levels = ndsa.score(catalog, collection.id)
        out.append(
            {
                "collection_id": collection.id,
                "title": collection.title,
                "local_pid": collection.local_pid,
                "fair_score": round(card.score, 4),
                "fair_grade": card.grade,
                "assessed_criteria": card.assessed,
                "blocking": [r.id for r in card.blocking_failures],
                "ndsa_level": levels.overall,
            }
        )
    return out


def _projected_scorecards(catalog: Catalog, plan: Plan) -> list[dict]:
    """What the scorecards would read after the plan, and what is out of reach.

    Projected rather than recomputed: the plan has not been carried out, and
    presenting a hypothetical as a measurement would be exactly the kind of
    confident falsehood this tool is built to avoid. Only the storage level is
    projected, because copies are what the plan changes.
    """
    projected = []
    copies_added: dict[int, int] = {}
    for step in plan.copy_steps:
        if step.collection_id is not None:
            copies_added[step.collection_id] = (
                copies_added.get(step.collection_id, 0) + 1
            )

    for entry in plan.scorecards_before:
        collection_id = entry["collection_id"]
        added = copies_added.get(collection_id, 0)
        projected.append(
            {
                **entry,
                "copies_added_by_plan": added,
                "ndsa_storage_after": (
                    entry["ndsa_level"] + (1 if added else 0)
                    if entry["ndsa_level"] < 4
                    else entry["ndsa_level"]
                ),
                "note": (
                    "Projected from the copy steps only. Describe steps would "
                    "lift the metadata and FAIR grades, but only once the "
                    "metadata is actually written — this is a forecast, not a "
                    "measurement."
                ),
            }
        )
    return projected


def _reclamation(catalog: Catalog, limit: int = 25) -> list[dict]:
    """The segregated appendix. Evidence only; never a step."""
    from drivefusion.core.analysis.duplicates import content_paths

    out = []
    for group in reclamation_candidates(catalog, limit=limit):
        out.append(
            {
                "content_id": group.content_id,
                "size_bytes": group.size_bytes,
                "paths": group.paths,
                "drives": group.drives,
                "physical_copies": group.physical_copies,
                "reclaimable_bytes": group.reclaimable_bytes,
                "locations": content_paths(catalog, group.content_id, limit=6),
                "note": (
                    "Duplicated within a single drive, so these bytes buy no "
                    "durability. This is evidence for you to act on with your "
                    "own tools; Drive Fusion does not delete and does not "
                    "generate a command that would."
                ),
            }
        )
    return out


# -- persistence --------------------------------------------------------------

def save(catalog: Catalog, plan: Plan) -> int:
    """Store a plan and its steps. Status starts, and stays, non-executable."""
    cursor = catalog.conn.execute(
        "INSERT INTO plan (created_at, name, policy_snapshot, standards_version, "
        "objective_json, simulation_json, solver, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
        (
            plan.created_at,
            plan.name,
            plan.policy.snapshot(),
            json.dumps(plan.standards),
            json.dumps(plan.objective),
            json.dumps(plan.simulation),
            plan.solver,
        ),
    )
    plan_id = int(cursor.lastrowid)

    catalog.conn.executemany(
        "INSERT INTO plan_step (plan_id, seq, kind, rationale, src_volume_id, "
        "src_dir_id, dst_volume_id, dst_path, content_id, collection_id, "
        "unit_key, bytes, footprint_bytes, depends_on, risk_delta) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        [
            (
                plan_id,
                step.seq,
                step.kind,
                step.rationale,
                step.src_volume_id,
                step.dst_volume_id,
                step.dst_path,
                step.collection_id,
                step.unit_key,
                step.bytes,
                step.footprint_bytes,
                json.dumps(list(step.depends_on)) if step.depends_on else None,
                step.risk_delta,
            )
            for step in plan.steps
        ],
    )
    catalog.conn.commit()
    plan.plan_id = plan_id
    return plan_id


def load(catalog: Catalog, plan_id: int) -> dict:
    row = catalog.conn.execute("SELECT * FROM plan WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError(f"no plan {plan_id}")
    steps = [
        dict(step)
        for step in catalog.conn.execute(
            "SELECT * FROM plan_step WHERE plan_id = ? ORDER BY seq", (plan_id,)
        )
    ]
    return {"plan": dict(row), "steps": steps}


def plans(catalog: Catalog, limit: int = 20) -> list[dict]:
    return [
        dict(row)
        for row in catalog.conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM plan_step s WHERE s.plan_id = p.id) "
            "AS steps FROM plan p ORDER BY p.id DESC LIMIT ?",
            (limit,),
        )
    ]
