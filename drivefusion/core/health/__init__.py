"""Drive health and the durability model (docs/PLAN.md §8.2).

Reading SMART is the one place this tool runs an external program against the
user's hardware, so ``smart.py`` holds an allowlist of read-only arguments:
smartctl can start a self-test or disable SMART, and neither the read-only
gateway nor the no-touch test would catch that.
"""

from drivefusion.core.health.afr import AfrError, AfrTable, load
# `collect_health`, not `collect`: a function sharing a name with its own
# submodule shadows it, and `import ...health.collect as m` then silently
# binds the function instead of the module.
from drivefusion.core.health.collect import (
    CollectionResult,
    collect_health,
    reading_from_row,
)
from drivefusion.core.health.risk import (
    ESTIMATED,
    MEASURED,
    UNKNOWN,
    DriveRisk,
    correlation,
    drive_risk,
    loss_probability,
    worst_confidence,
)
from drivefusion.core.health.smart import SmartError, SmartReading

__all__ = [
    "AfrError", "AfrTable", "load",
    "CollectionResult", "collect_health", "reading_from_row",
    "DriveRisk", "drive_risk", "correlation", "loss_probability",
    "worst_confidence", "MEASURED", "ESTIMATED", "UNKNOWN",
    "SmartError", "SmartReading",
]
