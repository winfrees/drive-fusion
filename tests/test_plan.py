"""The planner: documents that cannot imply a deletion, and sums that are right.

Two failure modes dominate this milestone, and neither raises an exception.

**A plan that implies removing something.** The whole tool rests on never doing
that, so the solver's representation has no way to express it: existing
placements are pinned and only ever unioned with. Tested from both ends — the
assertion that catches a regression, and the step vocabulary that has no verb
for it.

**A plan that says "this fits" and is wrong.** A unit's cost on a destination is
its cluster-rounded footprint, not its logical size. On the exFAT half of this
fleet, with 128 KiB clusters, a pile of small files costs tens of times its byte
count, and a plan estimated from logical bytes overflows the destination partway
through a copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.identity import hash_pass
from drivefusion.core.plan import document, solver
from drivefusion.core.plan import units as units_module
from drivefusion.core.plan.units import Unit, footprint, slack
from drivefusion.core.policy import Policy
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

GB = 1024 ** 3


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def add_volume(
    catalog: Catalog,
    name: str,
    *,
    cluster_bytes: int = 4096,
    fs_type: str = "ntfs",
    capacity: int = 500 * GB,
    free: int | None = None,
    batch_key: str | None = None,
    disaster_zone: str | None = None,
) -> tuple[int, int]:
    drive_id = catalog.upsert_drive(serial=f"SER-{name}", nickname=name)
    if batch_key or disaster_zone:
        catalog.update_drive(
            drive_id, batch_key=batch_key, disaster_zone=disaster_zone
        )
    volume_id = catalog.upsert_volume(
        volume_guid=f"vol:{name}",
        drive_id=drive_id,
        fs_type=fs_type,
        cluster_bytes=cluster_bytes,
        capacity_bytes=capacity,
        free_bytes=capacity if free is None else free,
    )
    return drive_id, volume_id


def scan_into(catalog: Catalog, volume_id: int, root: Path) -> None:
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)


def small_files(root: Path, count: int = 40, size: int = 5000) -> Path:
    target = root / "project"
    target.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (target / f"f{index:04d}.dat").write_bytes(b"D" * size)
    return target


def a_unit(sizes: dict[int, int]) -> Unit:
    """A unit built straight from a size histogram, for footprint arithmetic."""
    total = sum(size * count for size, count in sizes.items())
    return Unit(
        key="unit:test",
        label="test unit",
        files=sum(sizes.values()),
        bytes=total,
        drive_keys=(1,),
        size_histogram=tuple(sorted(sizes.items())),
    )


# -- footprint: the arithmetic a plan's promise rests on ----------------------

def test_footprint_rounds_every_file_up_to_a_cluster() -> None:
    """What the filesystem does, so "this fits" is a claim that holds."""
    unit = a_unit({5000: 1})
    assert unit.bytes == 5000
    assert footprint(unit, 4096) == 8192          # two 4 KiB clusters
    assert footprint(unit, 128 * 1024) == 131072  # one 128 KiB cluster


def test_small_files_on_a_big_cluster_cost_many_times_their_size() -> None:
    """The exFAT trap, stated as a number.

    Estimating from logical bytes here would under-count by a factor of 26 and
    produce a plan that overflows the destination partway through the copy.
    """
    unit = a_unit({5000: 40})
    assert unit.bytes == 200_000
    assert footprint(unit, 4096) == 327_680
    assert footprint(unit, 128 * 1024) == 5_242_880
    assert footprint(unit, 128 * 1024) > unit.bytes * 25
    assert slack(unit, 128 * 1024) == 5_242_880 - 200_000


def test_a_file_that_exactly_fills_clusters_has_no_slack() -> None:
    unit = a_unit({8192: 3})
    assert footprint(unit, 4096) == unit.bytes
    assert slack(unit, 4096) == 0


def test_empty_files_occupy_no_clusters() -> None:
    """A zero-length file takes a directory entry, not a cluster."""
    unit = a_unit({0: 100})
    assert footprint(unit, 4096) == 0


def test_an_unknown_cluster_size_falls_back_rather_than_to_one_byte() -> None:
    """A fallback of 1 would silently become a logical-byte estimate again."""
    unit = a_unit({5000: 1})
    assert footprint(unit, None) == footprint(unit, units_module.DEFAULT_CLUSTER_BYTES)
    assert footprint(unit, 0) == footprint(unit, units_module.DEFAULT_CLUSTER_BYTES)
    assert footprint(unit, 1) == footprint(unit, units_module.DEFAULT_CLUSTER_BYTES)


def test_the_plan_counts_footprint_not_logical_bytes(
    catalog: Catalog, tmp_path: Path
) -> None:
    """End to end: the copy step's size is what the destination will actually use."""
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(catalog, "SOURCE")
    scan_into(catalog, source_volume, source)
    add_volume(catalog, "EXFAT", cluster_bytes=128 * 1024, fs_type="exfat")

    plan = document.build(catalog, use_cp_sat=False)
    copy = plan.copy_steps[0]

    assert copy.bytes == 200_000, "logical size"
    assert copy.footprint_bytes == 5_242_880, "space needed on the destination"
    assert plan.simulation["bytes_to_copy"] == copy.footprint_bytes
    # And the discrepancy is explained rather than left to look like a bug.
    assert "clusters" in copy.rationale


# -- the solver may only add --------------------------------------------------

def test_the_solver_keeps_every_existing_copy(catalog: Catalog, tmp_path: Path) -> None:
    """The formal expression of read-only operation (§9)."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    units = units_module.placement_units(catalog)
    assert units

    from drivefusion.core.analysis.exposure import RiskIndex

    destinations = document._destinations(catalog, Policy.for_level())
    solution = solver.solve(
        units, destinations, Policy.for_level(), RiskIndex(catalog), use_cp_sat=False
    )

    for unit in units:
        assert set(unit.drive_keys) <= solution.assignments[unit.key], (
            "an existing placement was dropped"
        )
    solver.assert_additive(units, solution)


def test_dropping_a_placement_is_caught_before_a_plan_is_written() -> None:
    """Belt and braces: if a refactor ever introduces a removal, it fails loudly."""
    unit = a_unit({1000: 1})
    unit = Unit(**{**unit.__dict__, "drive_keys": (1, 2)})

    bad = solver.Solution(assignments={unit.key: {1}})
    with pytest.raises(AssertionError, match="only add copies"):
        solver.assert_additive([unit], bad)


def test_no_step_kind_can_express_a_destructive_action() -> None:
    """The vocabulary itself, not just the code paths."""
    for forbidden in ("move", "delete", "remove", "dedupe", "purge", "clean"):
        assert forbidden not in document.STEP_KINDS

    with pytest.raises(ValueError, match="no move or delete kind"):
        document.Step(seq=1, kind="delete", rationale="nope")


def test_a_capacity_shortfall_becomes_an_acquire_step(
    catalog: Catalog, tmp_path: Path
) -> None:
    """"You need another N TB", never a quiet proposal to remove something."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "ONLY", capacity=GB, free=1024)
    scan_into(catalog, volume, source)

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.acquire_steps, "a shortfall produced no acquire step"

    rationale = " ".join(step.rationale for step in plan.acquire_steps)
    assert "new storage" in rationale
    assert "will not propose deleting" in rationale
    assert not plan.copy_steps, "there was nowhere to copy to"


def test_a_shortfall_explains_cluster_slack_when_that_is_the_cause(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Otherwise "does not fit" is mysterious for a unit smaller than the space."""
    source = tmp_path / "source"
    small_files(source, count=200, size=1000)
    _, volume = add_volume(catalog, "SOURCE")
    scan_into(catalog, volume, source)
    # A destination with room for the logical bytes but not the footprint.
    add_volume(
        catalog, "TIGHT", cluster_bytes=128 * 1024, fs_type="exfat",
        capacity=4 * 1024 * 1024, free=4 * 1024 * 1024,
    )

    plan = document.build(catalog, use_cp_sat=False)
    reasons = " ".join(step.rationale for step in plan.acquire_steps)
    assert "cluster slack" in reasons, (
        "200 KB of data did not fit in 4 MB and the plan did not say why"
    )


# -- policy -------------------------------------------------------------------

def test_copy_targets_come_from_the_ndsa_level() -> None:
    assert Policy.for_level(1).min_copies == 2
    assert Policy.for_level(2).min_copies == 3
    assert Policy.for_level(2).min_zones == 2


def test_a_collection_target_can_only_strengthen_the_policy() -> None:
    """Level 3 inside a level-1 archive gets level 3; the reverse does not weaken."""
    policy = Policy.for_level(2)
    assert policy.copies_for(None) == 3
    assert policy.copies_for(1) == 3, "a weaker collection target must not win"
    assert policy.copies_for(4) == 3


def test_the_reserve_is_held_back_from_every_destination() -> None:
    """A drive filled to the last byte is a problem created, not solved."""
    policy = Policy(reserve_fraction=0.1)
    assert policy.usable_bytes(1000) == 900
    assert policy.usable_bytes(None) == 0


def test_a_policy_round_trips_through_its_snapshot() -> None:
    """A plan says what it was solved against, so an old plan stays readable."""
    from drivefusion.core.policy import from_snapshot

    policy = Policy.for_level(2, excluded_drives=(3, 4), notes="quarterly")
    restored = from_snapshot(policy.snapshot())
    assert restored == policy


def test_excluded_drives_are_never_chosen(catalog: Catalog, tmp_path: Path) -> None:
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(catalog, "A")
    scan_into(catalog, source_volume, source)
    excluded_drive, _ = add_volume(catalog, "B")

    plan = document.build(
        catalog,
        policy=Policy.for_level(1, excluded_drives=(excluded_drive,)),
        use_cp_sat=False,
    )
    assert not plan.copy_steps, "a copy was planned onto an excluded drive"
    assert plan.acquire_steps


# -- diversity ----------------------------------------------------------------

def _names(catalog: Catalog) -> dict[int, str]:
    return {
        row["volume_id"]: row["name"] for row in units_module.volume_capacity(catalog)
    }


def test_copies_are_not_all_left_in_one_purchase_batch(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Two drives from one order are not two independent copies (§8.2).

    The same-batch drive is deliberately the more attractive one on every other
    axis — ten times the free space, so the lowest imbalance cost — which is
    what makes this test able to fail. Judged on the objective alone the
    planner picks it.
    """
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(
        catalog, "A", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SAME-BATCH", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )
    add_volume(
        catalog, "DIFFERENT", batch_key="SEAGATE|IW|2023-08", disaster_zone="parents",
        capacity=400 * GB,
    )

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.copy_steps

    names = _names(catalog)
    chosen = {names[step.dst_volume_id] for step in plan.copy_steps}
    assert chosen == {"DIFFERENT"}, (
        f"the planner chose {chosen}, leaving every copy in one batch and one room"
    )


def test_diversity_steers_the_choice_but_never_blocks_a_copy(
    catalog: Catalog, tmp_path: Path
) -> None:
    """When no drive gives separation, a second copy still beats no second copy.

    A hard rule would report "buy another drive" to someone who has a perfectly
    good empty one, which is the kind of advice that gets a tool uninstalled.
    The compromise has to be visible in the step instead.
    """
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(
        catalog, "A", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office"
    )

    plan = document.build(catalog, use_cp_sat=False)
    names = _names(catalog)

    assert [names[s.dst_volume_id] for s in plan.copy_steps] == ["SIBLING"], (
        "a copy was refused because no diverse drive existed"
    )
    assert "purchase batch" in plan.copy_steps[0].rationale, (
        "the plan took a correlated copy without saying so"
    )
    assert not plan.acquire_steps or "batch" not in " ".join(
        s.rationale for s in plan.acquire_steps
    )


def test_an_existing_copy_counts_towards_diversity(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The rule is about where the copies end up, not about who placed them.

    Content already on one drive and a policy of two copies is the ordinary
    case for a tool that never moves anything, so a diversity rule that only
    looks at the copies the solver itself places is dead code.
    """
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(
        catalog, "EXISTING", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )
    add_volume(
        catalog, "OFFSITE", batch_key="HGST|DC|2024-01", disaster_zone="parents",
        capacity=400 * GB,
    )

    plan = document.build(catalog, use_cp_sat=False)
    names = _names(catalog)
    assert [names[s.dst_volume_id] for s in plan.copy_steps] == ["OFFSITE"]

    # And with zones required, the existing drive's zone is what makes the
    # sibling inadequate.
    plan2 = document.build(
        catalog, policy=Policy.for_level(2), use_cp_sat=False
    )
    zones = {names[s.dst_volume_id] for s in plan2.copy_steps}
    assert "OFFSITE" in zones


# -- the document -------------------------------------------------------------

def test_every_copy_is_followed_by_a_verify_that_depends_on_it(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A copy nobody checked is a copy you do not know you have."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    verifies = [s for s in plan.steps if s.kind == "verify"]
    assert len(verifies) == len(plan.copy_steps)
    for verify in verifies:
        assert verify.depends_on, "a verify step with no dependency is unordered"


def test_every_step_carries_a_rationale(catalog: Catalog, tmp_path: Path) -> None:
    """A checklist you cannot interrogate is one you should not follow."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.steps
    for step in plan.steps:
        assert step.rationale.strip(), f"step {step.seq} ({step.kind}) has no rationale"


def test_the_simulation_shows_cost_and_benefit_together(
    catalog: Catalog, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")
    hash_pass(catalog)

    plan = document.build(catalog, use_cp_sat=False)
    simulation = plan.simulation

    assert simulation["bytes_to_copy"] > 0
    assert simulation["estimated_hours"] >= 0
    assert simulation["expected_loss_after"] < simulation["expected_loss_before"], (
        "a plan that adds copies must reduce expected loss"
    )
    assert simulation["per_drive"], "no per-drive before/after"
    for entry in simulation["per_drive"]:
        assert entry["free_after"] <= entry["free_before"]
    # The throughput figure is an assumption and must say so.
    assert "sustained" in simulation["throughput_assumption"]


def test_reclamation_is_an_appendix_and_never_a_step(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Mixing "you could free 400 GB" into a checklist is how this becomes a
    deletion tool by inches."""
    source = tmp_path / "source"
    duplicated = source / "dupes"
    duplicated.mkdir(parents=True)
    payload = b"same content" * 500
    for name in ("a.bin", "b.bin", "c.bin"):
        (duplicated / name).write_bytes(payload)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    hash_pass(catalog)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.reclamation_appendix, "same-drive duplication was not reported"
    for entry in plan.reclamation_appendix:
        assert "does not delete" in entry["note"]

    # Nothing from the appendix leaks into the checklist under any kind.
    content_ids = {entry["content_id"] for entry in plan.reclamation_appendix}
    assert content_ids
    step_keys = {str(step.unit_key) for step in plan.steps if step.unit_key}
    for content_id in content_ids:
        assert f"content:{content_id}" not in step_keys
    assert {step.kind for step in plan.steps} <= set(document.STEP_KINDS)


def test_a_plan_serialises_for_diffing_and_archiving(
    catalog: Catalog, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    parsed = json.loads(plan.to_json())

    assert parsed["policy"]["min_copies"] == 2
    assert parsed["standards"], "a plan must name the rubric versions it used"
    assert parsed["steps"]
    assert "no executor" in parsed["note"]


def test_a_plan_is_stored_as_a_draft_and_has_no_applied_state(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A plan's terminal state is "exported", never "applied" (§3.3)."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    plan_id = document.save(catalog, plan)

    stored = document.load(catalog, plan_id)
    assert stored["plan"]["status"] == "draft"
    assert stored["steps"]
    assert stored["plan"]["policy_snapshot"], "the policy must travel with the plan"
    assert len(stored["steps"]) == len(plan.steps)


def test_scorecards_are_captured_before_and_projected_after(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The after figures are a forecast, and must be labelled as one."""
    from drivefusion.core.curate import collections as collections_module

    source = tmp_path / "source"
    project = small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")
    hash_pass(catalog)

    dir_id = int(
        catalog.conn.execute(
            "SELECT id FROM dir WHERE name = ?", (project.name,)
        ).fetchone()["id"]
    )
    collection = collections_module.create(catalog, title="Study", license="CC0-1.0")
    collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.scorecards_before
    assert plan.scorecards_after
    for entry in plan.scorecards_after:
        assert "forecast, not a measurement" in entry["note"]


def test_units_cover_content_in_no_collection(catalog: Catalog, tmp_path: Path) -> None:
    """§8.4: the planner does not wait for curation."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)

    units = units_module.placement_units(catalog)
    assert units, "uncurated content produced no placement units"
    assert sum(unit.files for unit in units) == 40


def test_a_collection_and_a_subtree_do_not_place_the_same_bytes_twice(
    catalog: Catalog, tmp_path: Path
) -> None:
    from drivefusion.core.curate import collections as collections_module

    source = tmp_path / "source"
    project = small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)

    dir_id = int(
        catalog.conn.execute(
            "SELECT id FROM dir WHERE name = ?", (project.name,)
        ).fetchone()["id"]
    )
    collection = collections_module.create(catalog, title="Study")
    collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))

    units = units_module.placement_units(catalog)
    total_files = sum(unit.files for unit in units)
    assert total_files == 40, (
        f"{total_files} files across units but only 40 exist — a collection and "
        "a subtree are both claiming them"
    )


# -- the search layer ---------------------------------------------------------

def _solver_fixture(catalog: Catalog, tmp_path: Path):
    """Set the catalog up once, and return a factory for fresh solver inputs.

    ``Destination.committed`` is mutated as a solve proceeds, so comparing two
    solves means building new destinations for each. Re-running the whole
    fixture would re-scan and double the catalog.
    """
    source = tmp_path / "source"
    big = source / "big"
    big.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        (big / f"v{index}.bin").write_bytes(b"x" * (64 * 1024))
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B", capacity=200 * GB)
    add_volume(catalog, "C", capacity=200 * GB, disaster_zone="parents")

    from drivefusion.core.analysis.exposure import RiskIndex

    def fresh():
        policy = Policy.for_level()
        return (
            units_module.placement_units(catalog),
            document._destinations(catalog, policy),
            policy,
            RiskIndex(catalog),
        )

    return fresh


def test_hill_climbing_never_makes_the_objective_worse(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The greedy result is the floor: a search that can lose is not an
    improvement, and a user following a worse plan has been actively harmed."""
    units, destinations, policy, index = _solver_fixture(catalog, tmp_path)()

    greedy = solver.solve_greedy(units, destinations, policy, index)
    before = solver.expected_loss(units, greedy, index)
    climbed = solver.hill_climb(units, destinations, policy, index, greedy)
    after = solver.expected_loss(units, climbed, index)

    assert after <= before
    solver.assert_additive(units, climbed)


def test_the_search_only_ever_adds(catalog: Catalog, tmp_path: Path) -> None:
    """Checked on the search as well as the seed: the add-only property has to
    hold for every stage, or a later one could undo it."""
    units, destinations, policy, index = _solver_fixture(catalog, tmp_path)()

    greedy = solver.solve_greedy(units, destinations, policy, index)
    seeded = {key: set(value) for key, value in greedy.assignments.items()}
    climbed = solver.hill_climb(units, destinations, policy, index, greedy)

    for key, drives in seeded.items():
        assert drives <= climbed.assignments[key]


def test_whatever_solver_ran_only_ever_added(catalog: Catalog, tmp_path: Path) -> None:
    """The add-only guarantee must hold for the solver that actually ran, which
    on a machine with ortools installed is CP-SAT — a different code path with
    its own chance to drop a placement."""
    fresh = _solver_fixture(catalog, tmp_path)
    units, destinations, policy, index = fresh()

    solution = solver.solve(units, destinations, policy, index, use_cp_sat=True)
    solver.assert_additive(units, solution)
    for unit in units:
        assert set(unit.drive_keys) <= solution.assignments[unit.key]


def test_a_missing_cp_sat_costs_quality_not_correctness(
    catalog: Catalog, tmp_path: Path, monkeypatch
) -> None:
    """``ortools`` is optional, so the branch where it is absent is the one many
    users take. It must return the explainable baseline, not nothing.

    The import is blocked rather than skipped-on-absence, so this is exercised
    whether or not ortools happens to be installed here.
    """
    import builtins

    real_import = builtins.__import__

    def no_ortools(name, *args, **kwargs):
        if name.startswith("ortools"):
            raise ImportError("ortools is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ortools)

    units, destinations, policy, index = _solver_fixture(catalog, tmp_path)()
    solution = solver.solve(units, destinations, policy, index, use_cp_sat=True)

    assert solution.assignments
    assert solution.solver != "cp-sat", "cp-sat reported without ortools"
    solver.assert_additive(units, solution)
    assert solution.objective["expected_loss_after"] <= (
        solution.objective["expected_loss_before"]
    )


def test_cp_sat_is_kept_only_when_it_beats_the_baseline(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The optimiser is an improvement step, never the only path to an answer."""
    pytest.importorskip("ortools", reason="CP-SAT is an optional improvement")
    fresh = _solver_fixture(catalog, tmp_path)

    units, destinations, policy, index = fresh()
    baseline = solver.solve(units, destinations, policy, index, use_cp_sat=False)

    units2, destinations2, policy2, index2 = fresh()
    optimised = solver.solve(units2, destinations2, policy2, index2, use_cp_sat=True)

    if optimised.solver == "cp-sat":
        assert optimised.objective["weighted"] < baseline.objective["weighted"], (
            "cp-sat was adopted without beating the baseline"
        )
    solver.assert_additive(units2, optimised)


def test_cp_sat_respects_the_same_diversity_rule_as_the_greedy_path(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Two solvers that disagree about the policy would make the plan depend on
    whether an optional package happens to be installed."""
    pytest.importorskip("ortools", reason="CP-SAT is an optional improvement")

    source = tmp_path / "source"
    small_files(source, count=400, size=4096)
    _, source_volume = add_volume(
        catalog, "EXISTING", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )
    add_volume(
        catalog, "OFFSITE", batch_key="HGST|DC|2024-01", disaster_zone="parents",
        capacity=400 * GB,
    )

    plan = document.build(catalog, use_cp_sat=True)
    names = _names(catalog)
    chosen = {names[step.dst_volume_id] for step in plan.copy_steps}
    assert "OFFSITE" in chosen, (
        f"{plan.solver} left every copy in one batch: {chosen}"
    )


def test_the_objective_is_broken_down_rather_than_a_single_number(
    catalog: Catalog, tmp_path: Path
) -> None:
    """"Plan A scores 0.72" is not something a person can argue with."""
    units, destinations, policy, index = _solver_fixture(catalog, tmp_path)()
    solution = solver.solve(units, destinations, policy, index, use_cp_sat=False)

    breakdown = solution.objective
    for term in (
        "expected_loss_before",
        "expected_loss_after",
        "expected_loss_reduction",
        "bytes_copied",
        "drives_touched",
        "imbalance",
        "unmet_copy_targets",
        "correlated_copies",
        "weighted",
    ):
        assert term in breakdown, f"the objective hides its {term} term"


def test_an_extra_copy_beyond_the_target_says_it_is_optional(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Otherwise a three-copy plan under a two-copy policy looks like a bug.

    Driven from a constructed solution rather than from whatever the search
    happens to produce, so the wording is actually asserted.
    """
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")
    add_volume(catalog, "C")

    units = units_module.placement_units(catalog)
    policy = Policy.for_level(1)
    destinations = document._destinations(catalog, policy)
    from drivefusion.core.analysis.exposure import RiskIndex

    index = RiskIndex(catalog)
    solution = solver.solve(units, destinations, policy, index, use_cp_sat=False)

    # Force a third copy, as the hill climb would on a more valuable unit.
    unit = units[0]
    spare = next(
        d for d in destinations if d.drive_key not in solution.assignments[unit.key]
    )
    solution.assignments[unit.key].add(spare.drive_key)
    solution.additions[unit.key].append((spare.volume_id, unit.bytes))
    solution.beyond_target.add((unit.key, spare.volume_id))

    plan = document.from_solution(
        catalog, units=units, solution=solution, policy=policy, index=index,
        destinations=destinations, name="forced",
    )
    extras = [s for s in plan.copy_steps if "can be dropped" in s.rationale]
    assert extras, "an extra copy was proposed without saying it was optional"
    assert "already reaches its 2-copy target" in extras[0].rationale


def test_the_objective_never_prefers_leaving_data_unprotected(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The objective must rank a plan that meets the policy above doing nothing.

    This failed once, and silently. Expected loss is a rate — bytes per year —
    while the cost of copying is paid once, so summing them directly priced a
    one-off 1.6 MB copy roughly ten times the annual loss it prevented. The
    greedy path hid it, because that path is driven by the copy-target loop
    rather than by the objective; CP-SAT optimises the objective faithfully and
    duly returned a plan with no copies in it, which then *beat* the greedy plan
    on score and was adopted.

    An objective that cannot prefer acting is not a tie-break between good
    plans, it is a recommendation to do nothing.
    """
    source = tmp_path / "source"
    small_files(source, count=400, size=4096)
    _, source_volume = add_volume(catalog, "A")
    scan_into(catalog, source_volume, source)
    add_volume(catalog, "B", capacity=4000 * GB)

    from drivefusion.core.analysis.exposure import RiskIndex

    units = units_module.placement_units(catalog)
    policy = Policy.for_level(1)
    index = RiskIndex(catalog)

    planned = solver.solve(
        units, document._destinations(catalog, policy), policy, index,
        use_cp_sat=False,
    )

    idle_destinations = document._destinations(catalog, policy)
    idle = solver.Solution(
        assignments={u.key: set(u.drive_keys) for u in units},
        additions={u.key: [] for u in units},
    )
    idle.objective = solver.objective_breakdown(
        units, idle, idle_destinations, policy, index
    )

    assert planned.objective["weighted"] < idle.objective["weighted"], (
        f"doing nothing scores {idle.objective['weighted']:.4f} against "
        f"{planned.objective['weighted']:.4f} for a plan that meets the policy"
    )
    assert idle.objective["unmet_bytes"] > 0
    assert planned.objective["unmet_bytes"] == 0


def test_the_objective_terms_are_commensurable(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Each weighted term is a fraction of the archive, so the weights mean
    what they say. A term in raw bytes swamps every other term at any realistic
    archive size, which is how the bug above survived being looked at."""
    source = tmp_path / "source"
    small_files(source, count=400, size=4096)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B", capacity=4000 * GB)

    from drivefusion.core.analysis.exposure import RiskIndex

    units = units_module.placement_units(catalog)
    policy = Policy.for_level(1)
    solution = solver.solve(
        units, document._destinations(catalog, policy), policy,
        RiskIndex(catalog), use_cp_sat=False,
    )

    for name, value in solution.objective["components"].items():
        assert 0.0 <= value <= 100.0, f"{name} is not a fraction: {value}"


def test_the_horizon_is_recorded_so_the_arithmetic_can_be_checked(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A durability figure without the period it covers is not a figure."""
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    plan = document.build(catalog, use_cp_sat=False)
    assert plan.objective["horizon_years"] == plan.policy.horizon_years
    assert plan.policy.horizon_years >= 1


def test_the_weighted_objective_actually_pays_for_correlated_copies(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Listing the term is not the same as charging for it.

    Two solutions over identical units, differing only in whether the second
    copy lands in the same purchase batch, must not score the same — otherwise
    an optimiser is free to pick either and will pick the cheaper one.

    The charge arrives through the durability term, because corr(D) in §8.2
    already raises the loss probability of copies that share a batch or a
    room. There is deliberately no second penalty: billing the same fault twice
    would make the weights mean something other than what they say.
    """
    source = tmp_path / "source"
    small_files(source)
    _, source_volume = add_volume(
        catalog, "EXISTING", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office")
    add_volume(catalog, "OFFSITE", batch_key="HGST|DC|2024-01", disaster_zone="parents")

    from drivefusion.core.analysis.exposure import RiskIndex

    units = units_module.placement_units(catalog)
    policy = Policy.for_level(1)
    index = RiskIndex(catalog)
    unit = units[0]

    def score(destination_name: str) -> dict:
        destinations = document._destinations(catalog, policy)
        chosen = next(d for d in destinations if d.name == destination_name)
        size = units_module.footprint(unit, chosen.cluster_bytes)
        chosen.committed += size
        solution = solver.Solution(
            assignments={u.key: set(u.drive_keys) for u in units},
            additions={u.key: [] for u in units},
        )
        solution.assignments[unit.key].add(chosen.drive_key)
        solution.additions[unit.key].append((chosen.volume_id, size))
        return solver.objective_breakdown(
            units, solution, destinations, policy, index
        )

    correlated = score("SIBLING")
    separated = score("OFFSITE")

    assert correlated["correlated_copies"] == 1
    assert separated["correlated_copies"] == 0
    assert separated["weighted"] < correlated["weighted"], (
        "the objective reports correlated copies but does not charge for them"
    )
    # And the charge is the durability term, not a surcharge on top of it.
    assert separated["expected_loss_after"] < correlated["expected_loss_after"]


def test_cp_sat_counts_a_copy_on_a_drive_it_cannot_write_to(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A copy on an excluded drive is still a copy, and still carries a batch.

    This is the case where the pinning in the CP-SAT model does real work: the
    drive holding the existing copy is not among the destinations, so the only
    way its purchase batch can be known is from the drive record. Without that
    the model sees no copies at all and has no reason to prefer separation.

    The solver is driven directly, with a deliberately empty seed, because the
    safety net in :func:`solve` — keep CP-SAT only if it beats the greedy
    baseline — would otherwise mask the model's behaviour entirely.
    """
    pytest.importorskip("ortools", reason="CP-SAT is an optional improvement")

    source = tmp_path / "source"
    small_files(source)
    excluded_drive, source_volume = add_volume(
        catalog, "LOCKED", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )
    add_volume(
        catalog, "OFFSITE", batch_key="HGST|DC|2024-01", disaster_zone="parents",
        capacity=400 * GB,
    )

    from drivefusion.core.analysis.exposure import RiskIndex

    # A copy cost high enough that no *voluntary* extra copy pays for itself,
    # so exactly one placement happens and the test is about which drive it
    # lands on rather than about how many copies the solver fancies.
    policy = Policy.for_level(
        1, excluded_drives=(excluded_drive,), weight_bytes_copied=0.5
    )
    units = units_module.placement_units(catalog)
    destinations = document._destinations(catalog, policy)
    index = RiskIndex(catalog)

    empty = solver.Solution(
        assignments={u.key: set(u.drive_keys) for u in units},
        additions={u.key: [] for u in units},
    )
    empty.objective = {"weighted": float("inf")}

    candidate = solver._solve_cp_sat(units, destinations, policy, index, empty, 10.0)
    assert candidate is not None

    by_volume = {d.volume_id: d.name for d in destinations}
    placed = {
        by_volume[volume_id]
        for additions in candidate.additions.values()
        for volume_id, _size in additions
    }
    assert placed == {"OFFSITE"}, (
        f"cp-sat chose {placed}: it did not see that the existing copy is "
        "already in the WD batch"
    )


def test_the_copy_target_is_honoured_even_when_it_does_not_pay_for_itself(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The policy is a constraint, not a preference.

    Where the durability arithmetic alone would not justify a copy — a healthy
    drive, a cheap archive, an expensive copy — the objective must still rank
    meeting the target above skipping it. Otherwise "two copies" quietly
    becomes "two copies when it seems worth it", which is not a preservation
    policy and is not what the plan says it is doing.
    """
    source = tmp_path / "source"
    small_files(source, count=400, size=4096)
    _, source_volume = add_volume(catalog, "A")
    scan_into(catalog, source_volume, source)
    add_volume(catalog, "B", capacity=4000 * GB)

    from drivefusion.core.analysis.exposure import RiskIndex

    # A copy cost far above any durability gain it could buy.
    policy = Policy.for_level(1, weight_bytes_copied=5.0)
    units = units_module.placement_units(catalog)
    index = RiskIndex(catalog)

    planned = solver.solve(
        units, document._destinations(catalog, policy), policy, index,
        use_cp_sat=False,
    )
    idle = solver.Solution(
        assignments={u.key: set(u.drive_keys) for u in units},
        additions={u.key: [] for u in units},
    )
    idle.objective = solver.objective_breakdown(
        units, idle, document._destinations(catalog, policy), policy, index
    )

    # The target is met, and that is not a consequence of how copying happens
    # to be priced: the comparison between solutions puts unmet bytes first.
    assert planned.objective["unmet_bytes"] == 0
    assert idle.objective["unmet_bytes"] > 0
    assert solver.is_better(planned, idle), (
        "with copying priced above its durability gain, the solver preferred "
        "leaving the copy target unmet"
    )
    assert not solver.is_better(idle, planned)
    # Priced purely by weight, doing nothing does look cheaper here — which is
    # exactly why the copy target cannot be just another weighted term.
    assert idle.objective["weighted"] < planned.objective["weighted"]


def test_a_plan_is_refused_if_the_solution_dropped_a_copy(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The gate is on the document, not only on the solver.

    ``assert_additive`` is tested directly elsewhere, but what matters is that
    nothing can write a plan around it: the check has to sit on the path a
    document is actually produced by.
    """
    source = tmp_path / "source"
    small_files(source)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B")

    from drivefusion.core.analysis.exposure import RiskIndex

    policy = Policy.for_level(1)
    units = units_module.placement_units(catalog)
    destinations = document._destinations(catalog, policy)
    index = RiskIndex(catalog)
    solution = solver.solve(units, destinations, policy, index, use_cp_sat=False)

    # Forget where the content already is, as a bad refactor would.
    solution.assignments[units[0].key] = set()

    with pytest.raises(AssertionError, match="only add copies"):
        document.from_solution(
            catalog, units=units, solution=solution, policy=policy, index=index,
            destinations=destinations, name="broken",
        )


def test_cp_sat_labels_its_own_extra_copies_and_concessions(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The notes are a property of the plan, not of one code path.

    CP-SAT builds its candidate from scratch rather than from the greedy
    solution, so everything the document needs has to be populated there too —
    otherwise installing an optional package silently strips the caveats off
    the plan.
    """
    pytest.importorskip("ortools", reason="CP-SAT is an optional improvement")

    source = tmp_path / "source"
    small_files(source, count=400, size=4096)
    _, source_volume = add_volume(
        catalog, "EXISTING", batch_key="WD|RED|2021-03", disaster_zone="office"
    )
    scan_into(catalog, source_volume, source)
    add_volume(
        catalog, "SIBLING", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )
    # A third drive, so CP-SAT can and will go past the two-copy target: with
    # durability valued over the horizon an extra copy pays for itself, which
    # is intended — and has to be labelled.
    add_volume(
        catalog, "SPARE", batch_key="WD|RED|2021-03", disaster_zone="office",
        capacity=4000 * GB,
    )

    from drivefusion.core.analysis.exposure import RiskIndex

    policy = Policy.for_level(1)
    units = units_module.placement_units(catalog)
    destinations = document._destinations(catalog, policy)
    index = RiskIndex(catalog)

    seed = solver.Solution(
        assignments={u.key: set(u.drive_keys) for u in units},
        additions={u.key: [] for u in units},
        objective={"weighted": float("inf")},
    )
    candidate = solver._solve_cp_sat(units, destinations, policy, index, seed, 10.0)
    assert candidate is not None
    assert any(additions for additions in candidate.additions.values())

    # Every drive is a sibling of the one already holding it, so the caveat is
    # unavoidable — and must therefore be stated.
    assert candidate.concessions, "cp-sat recorded no correlation caveat"
    assert "purchase batch" in next(iter(candidate.concessions.values()))
    assert candidate.beyond_target, "cp-sat labelled no copy as beyond the target"

    plan = document.from_solution(
        catalog, units=units, solution=candidate, policy=policy, index=index,
        destinations=destinations, name="cp-sat",
    )
    assert any("purchase batch" in step.rationale for step in plan.copy_steps)
    assert any("can be dropped" in step.rationale for step in plan.copy_steps), (
        "cp-sat proposed a copy past the target without saying it was optional"
    )


def test_step_gains_sum_to_what_the_plan_delivers(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Each copy claims its marginal gain, not the whole plan's.

    Attributing the full reduction to every step made two copies each claim the
    entire benefit, so the per-step figures summed to roughly twice what the
    plan achieved — and anyone weighing whether to skip the last copy was told
    it protects data an earlier copy already protects.
    """
    source = tmp_path / "source"
    small_files(source, count=200, size=8192)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B", disaster_zone="office")
    add_volume(catalog, "C", disaster_zone="parents")

    plan = document.build(catalog, policy=Policy.for_level(2), use_cp_sat=False)
    assert len(plan.copy_steps) >= 2

    claimed = sum(-step.risk_delta for step in plan.copy_steps)
    delivered = plan.simulation["expected_loss_reduction"]
    assert claimed == pytest.approx(delivered, rel=0.02), (
        f"steps claim {claimed:,.0f} bytes/year but the plan delivers "
        f"{delivered:,.0f}"
    )
    # And the total is shared out, not concentrated in the first step with the
    # rest claiming nothing: a copy that buys no durability has no business
    # being in a plan, so every copy step must show a positive gain.
    for step in plan.copy_steps:
        assert -step.risk_delta > 0, (
            f"step {step.seq} proposes a copy that reduces expected loss by "
            "nothing at all"
        )


def test_each_extra_copy_claims_less_than_the_one_before(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Diminishing returns are the whole shape of the problem: the second copy
    removes most of the risk and the third removes most of what is left. A plan
    that reports them as equal is not telling the user where the value is."""
    source = tmp_path / "source"
    small_files(source, count=200, size=8192)
    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "B", disaster_zone="office")
    add_volume(catalog, "C", disaster_zone="parents")

    plan = document.build(catalog, policy=Policy.for_level(2), use_cp_sat=False)
    gains = [-step.risk_delta for step in plan.copy_steps]
    assert len(gains) >= 2
    assert gains == sorted(gains, reverse=True)
    assert gains[1] < gains[0], "the second copy claimed as much as the first"


def test_a_tiny_durability_gain_is_described_rather_than_rounded_to_zero(
    catalog: Catalog, tmp_path: Path
) -> None:
    """"reduces expected loss by about 0 bytes/year" reads as a step that
    achieves nothing, and invites the obvious question of why it is in the plan.

    The answer is usually that the policy asks for the copy, so the rationale
    says that instead of printing a rounded-down number.
    """
    from drivefusion.core.plan.document import _gain_phrase

    assert "about 5,000 bytes/year" in _gain_phrase(5000.4)
    assert "about 1 bytes/year" in _gain_phrase(1.0)

    for tiny in (0.9, 0.001, 1e-9):
        phrase = _gain_phrase(tiny)
        assert "less than a byte" in phrase
        assert "policy asks for it" in phrase
        assert "about 0" not in phrase

    none = _gain_phrase(0.0)
    assert "does not measurably reduce" in none
    assert "about 0" not in none


def test_no_copy_step_claims_a_gain_of_about_zero(
    catalog: Catalog, tmp_path: Path
) -> None:
    """End to end over a fixture that produces one: a tiny leftover unit copied
    to a big-cluster destination buys almost nothing, and that is exactly the
    step whose wording was wrong."""
    source = tmp_path / "source"
    (source / "project").mkdir(parents=True)
    for index in range(30):
        (source / "project" / f"big{index}.bin").write_bytes(b"x" * 40_000)
    # A couple of bytes sitting directly in the scanned root, which becomes its
    # own tiny unit.
    (source / "stray.txt").write_text("hi")

    _, volume = add_volume(catalog, "A")
    scan_into(catalog, volume, source)
    add_volume(catalog, "EXFAT", cluster_bytes=131072, fs_type="exfat",
               disaster_zone="office")
    add_volume(catalog, "OFFSITE", cluster_bytes=4096, disaster_zone="parents")
    hash_pass(catalog)

    plan = document.build(catalog, policy=Policy.for_level(2), use_cp_sat=False)
    assert plan.copy_steps

    for step in plan.copy_steps:
        assert "about 0 bytes/year" not in step.rationale, step.rationale
