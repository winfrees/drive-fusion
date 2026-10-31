"""The greedy planner (ROADMAP M7.4): properties over random fleets, and cases."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from drivefusion.core.planner import greedy
from drivefusion.core.planner.capacity import Destination
from drivefusion.core.planner.units import Unit

GiB = 1 << 30
AFR = 0.02


def toy_risk(dests: list[Destination]):
    """P(loss) as a product of AFRs, doubled per extra drive sharing a batch."""
    by_id = {d.drive_id: d for d in dests}

    def p_of(holders: frozenset) -> float:
        if not holders:
            return AFR
        p = AFR ** len(holders)
        batches = Counter(by_id[h].batch_key for h in holders if h in by_id and by_id[h].batch_key)
        for n in batches.values():
            p *= 2 ** (n - 1)
        return min(p, AFR)
    return p_of


def dest(i, room, *, cluster=4096, batch="", zone="", excluded=""):
    return Destination(i, i, f"D{i}", cluster, capacity=room * 2, free=room, reserve=0,
                       batch_key=batch, zone=zone, excluded=excluded)


def unit(key, size, holders=(), target=2, clusters=(4096, 131072)):
    return Unit(key, "folder", key, 0, 0, target=target, files=1, bytes=size, items=1,
                holders=frozenset(holders),
                footprint={c: -(-size // c) * c for c in clusters}, p_loss=AFR ** max(1, len(holders)))


def check(units, dests, solution) -> None:
    by_id = {d.drive_id: d for d in dests}
    used = Counter()
    for a in solution.added:
        used[a.drive_id] += a.footprint
        assert a.footprint == next(u for u in units if u.key == a.unit).footprint[
            by_id[a.drive_id].cluster_bytes]
    for d in dests:
        assert used[d.drive_id] <= d.room, "never over a destination's room by footprint"
        assert not (d.excluded and used[d.drive_id]), "never onto an excluded drive"
    for u in units:
        after = solution.holders[u.key]
        assert u.holders <= after, "existing copies are pinned"
        assert len(after) >= len(u.holders), "never fewer copies than before"
        added = after - u.holders
        limit = max(1, u.target - 1)
        for attr in ("batch_key", "zone"):
            values = Counter(getattr(by_id[h], attr) for h in after if h in by_id)
            for value, n in values.items():
                if value and n > limit:
                    assert not any(getattr(by_id[h], attr) == value for h in added), \
                        f"{attr} {value!r} over-concentrated by the plan"
        short = solution.short.get(u.key, (0, 0))[0]
        copies = len(after) + (0 if u.holders else 1)    # an unverified copy is one
        assert copies + short >= u.target, "a shortfall is an acquire"
        assert solution.p_after[u.key] <= solution.p_before[u.key] + 1e-15


@pytest.mark.parametrize("seed", range(40))
def test_properties_hold_on_random_fleets(seed) -> None:
    rnd = random.Random(seed)
    dests = [dest(i, rnd.choice([0, 5, 20, 80]) * GiB, cluster=rnd.choice([4096, 131072]),
                  batch=rnd.choice(["", "b1", "b2"]), zone=rnd.choice(["", "home", "office"]),
                  excluded=rnd.choice([""] * 5 + ["retired"]))
             for i in range(1, rnd.randint(2, 7))]
    ids = [d.drive_id for d in dests]
    units = [unit(f"u{k}", rnd.randint(1, 30) * GiB + rnd.randint(0, 9999),
                  holders=rnd.sample(ids, rnd.randint(0, min(2, len(ids)))),
                  target=rnd.choice([2, 2, 3]))
             for k in range(rnd.randint(1, 12))]
    solution = greedy.solve(units, dests, toy_risk(dests))
    check(units, dests, solution)


def test_a_single_copy_gets_a_second_on_the_safer_drive() -> None:
    dests = [dest(1, 100 * GiB, batch="b1"), dest(2, 100 * GiB, batch="b1"),
             dest(3, 100 * GiB, batch="b2")]
    solution = greedy.solve([unit("u", GiB, holders=[1])], dests, toy_risk(dests))
    assert solution.holders["u"] == {1, 3}, "the other batch: lower P(loss), and diverse"
    assert solution.short == {}


def test_batch_diversity_is_a_rule_not_just_a_cost() -> None:
    dests = [dest(1, 100 * GiB, batch="b1"), dest(2, 100 * GiB, batch="b1")]
    solution = greedy.solve([unit("u", GiB, holders=[1])], dests, lambda h: AFR ** len(h))
    assert solution.holders["u"] == {1} and solution.short == {"u": (1, GiB)}


def test_zone_separation_and_unknown_zones() -> None:
    dests = [dest(1, 100 * GiB, zone="home"), dest(2, 100 * GiB, zone="home"),
             dest(3, 100 * GiB)]
    solution = greedy.solve([unit("u", GiB, holders=[1])], dests, lambda h: AFR ** len(h))
    assert solution.holders["u"] == {1, 3}


def test_footprint_not_size_decides_fit() -> None:
    small = unit("u", 10 * 4096, holders=[1])       # 40 KiB, but 128 KiB on 128 KiB clusters
    dests = [dest(1, GiB), dest(2, 100 * 1024, cluster=131072)]
    solution = greedy.solve([small], dests, lambda h: AFR ** len(h))
    assert solution.short == {"u": (1, small.bytes)}
    dests[1] = dest(2, 131072, cluster=131072)
    assert greedy.solve([small], dests, lambda h: AFR ** len(h)).holders["u"] == {1, 2}


def test_riskiest_first_gets_the_room() -> None:
    dests = [dest(1, 100 * GiB), dest(2, 10 * GiB)]
    risky, safe = unit("risky", 8 * GiB, holders=[1]), unit("safe", 8 * GiB, holders=[1])
    risky.p_loss, safe.p_loss = 0.05, 0.01
    solution = greedy.solve([risky, safe], dests, lambda h: AFR ** len(h))
    assert solution.holders["risky"] == {1, 2} and "safe" in solution.short


def test_targets_already_met_add_nothing() -> None:
    dests = [dest(1, GiB), dest(2, GiB), dest(3, 100 * GiB)]
    solution = greedy.solve([unit("u", GiB, holders=[1, 2])], dests, lambda h: AFR ** len(h))
    assert solution.added == [] and solution.p_after["u"] == solution.p_before["u"]


def test_an_unverified_unit_counts_as_one_copy() -> None:
    dests = [dest(1, 100 * GiB), dest(2, 100 * GiB)]
    solution = greedy.solve([unit("u", GiB)], dests, lambda h: AFR ** max(1, len(h)))
    assert len(solution.holders["u"]) == 1, "one proven copy added; the other is unverified"


def test_end_to_end_on_a_catalog(tmp_path) -> None:
    from drivefusion.core.planner import capacity, units
    from drivefusion.core.store.catalog import Catalog
    from tests.test_planner_units import Fleet

    with Catalog(tmp_path / "catalog.db") as catalog:
        f = Fleet(catalog)
        a, b = f.drive("A", purchase_date="2020-01-01"), f.drive("B", purchase_date="2024-01-01")
        f.drive("C", free=0)
        folder = f.mkdir("A", "Photos")
        f.file("A", folder, 5 << 20)
        found = units.build(catalog)
        dests = capacity.destinations(catalog)
        p_of = greedy.risk_function(catalog)
        solution = greedy.solve(found, dests, p_of)
        check(found, dests, solution)
        photos = next(u for u in found if u.label.endswith("Photos"))
        assert solution.holders[photos.key] == {a, b}
        assert solution.p_after[photos.key] < solution.p_before[photos.key]


def test_a_copy_counts_only_where_every_item_gains_a_drive() -> None:
    """Found by VV-PLAN-03: one item on an unverified volume, another on A. The
    unit holds no drive in common, but a copy onto A adds nothing to the item
    already there; it has to go to B."""
    dests = [dest(1, 100 * GiB), dest(2, 100 * GiB)]
    u = unit("u", GiB)
    u.holder_sets = frozenset({frozenset(), frozenset({1})})
    assert u.copies == 1 and u.copies_with({1}) == 1 and u.copies_with({2}) == 2
    solution = greedy.solve([u], dests, lambda h: AFR ** max(1, len(h)))
    assert [a.drive_id for a in solution.added] == [2] and solution.short == {}
    only_a = greedy.solve([u], dests[:1], lambda h: AFR ** max(1, len(h)))
    assert only_a.added == [] and only_a.short == {"u": (1, GiB)}
