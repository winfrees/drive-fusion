"""The planner. Plans are documents; there is no executor (docs/PLAN.md §3.3).

The step vocabulary has no move and no delete kind, and the solver pins every
existing copy, so a plan cannot represent a state where data was removed.
"""

from drivefusion.core.plan.document import (
    STEP_KINDS,
    Plan,
    Step,
    build,
    load,
    plans,
    save,
)
from drivefusion.core.plan.solver import (
    Destination,
    Shortfall,
    Solution,
    assert_additive,
    solve,
    solve_greedy,
)
from drivefusion.core.plan.units import (
    Unit,
    footprint,
    placement_units,
    slack,
    volume_capacity,
)

__all__ = [
    "STEP_KINDS", "Plan", "Step", "build", "load", "plans", "save",
    "Destination", "Shortfall", "Solution", "assert_additive", "solve",
    "solve_greedy",
    "Unit", "footprint", "placement_units", "slack", "volume_capacity",
]
