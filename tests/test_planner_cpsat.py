"""CP-SAT over the greedy plan (ROADMAP M7.5): only ever an improvement."""

from __future__ import annotations

import random

import pytest

from drivefusion.core.planner import cpsat, greedy
from tests.test_planner_greedy import AFR, GiB, check, dest, toy_risk, unit

needs_ortools = pytest.mark.skipif(not cpsat.available(), reason="ortools not installed")


def p_simple(h):
    return AFR ** max(1, len(h))


def test_without_ortools_the_greedy_plan_stands(monkeypatch) -> None:
    monkeypatch.setattr(cpsat, "available", lambda: False)
    dests = [dest(1, 100 * GiB), dest(2, 100 * GiB)]
    found, solver = cpsat.solve([unit("u", GiB, holders=[1])], dests, p_simple)
    assert solver == "greedy" and found.holders["u"] == {1, 2}


@needs_ortools
def test_packing_better_than_riskiest_first() -> None:
    """Greedy gives the riskiest unit the room and leaves two short; CP-SAT one."""
    dests = [dest(1, 100 * GiB), dest(2, 10 * GiB)]
    big, b, c = (unit("big", 6 * GiB, holders=[1]), unit("b", 5 * GiB, holders=[1]),
                 unit("c", 5 * GiB, holders=[1]))
    big.p_loss = 0.05
    baseline = greedy.solve([big, b, c], dests, p_simple)
    assert cpsat.short_copies(baseline) == 2
    found, solver = cpsat.solve([big, b, c], dests, p_simple, time_limit=5)
    assert solver == "cp-sat" and cpsat.short_copies(found) == 1
    check([big, b, c], dests, found)


@needs_ortools
@pytest.mark.parametrize("seed", range(15))
def test_properties_hold_and_it_never_does_worse(seed) -> None:
    rnd = random.Random(1000 + seed)
    dests = [dest(i, rnd.choice([0, 5, 20, 80]) * GiB, cluster=rnd.choice([4096, 131072]),
                  batch=rnd.choice(["", "b1", "b2"]), zone=rnd.choice(["", "home", "office"]),
                  excluded=rnd.choice([""] * 5 + ["retired"]))
             for i in range(1, rnd.randint(2, 7))]
    ids = [d.drive_id for d in dests]
    units = [unit(f"u{k}", rnd.randint(1, 30) * GiB,
                  holders=rnd.sample(ids, rnd.randint(0, min(2, len(ids)))),
                  target=rnd.choice([2, 2, 3]))
             for k in range(rnd.randint(1, 12))]
    p_of = toy_risk(dests)
    found, _ = cpsat.solve(units, dests, p_of, time_limit=2)
    check(units, dests, found)
    baseline = greedy.solve(units, dests, p_of)
    assert cpsat._rank(units, found) <= cpsat._rank(units, baseline)
