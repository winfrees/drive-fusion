"""Preservation policy: how many copies, how different they must be.

The planner needs numbers, and the numbers come from a published framework
rather than from taste: NDSA Levels of Digital Preservation give copy counts and
geographic separation per level, and a collection's ``ndsa_target`` picks which
row applies to it.

Policy is a *snapshot* on every plan. A plan solved last month against "two
copies" is not wrong when the target becomes three — it is a plan against a
different policy, and the document says which.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from drivefusion.core import standards

#: Copy targets by NDSA level, read from the published levels: level 1 is two
#: copies in separate locations, level 2 adds a third with one off-site.
COPIES_BY_LEVEL = {1: 2, 2: 3, 3: 3, 4: 3}

#: Levels at which copies must sit in different disaster zones.
ZONES_BY_LEVEL = {1: 1, 2: 2, 3: 2, 4: 3}

#: Default when a collection has no target set. Two copies is the floor at
#: which a single drive failure is survivable, and anything less is not a
#: preservation policy at all.
DEFAULT_LEVEL = 1


@dataclass(frozen=True)
class Policy:
    """The rules a plan is solved against."""

    min_copies: int = 2
    min_zones: int = 1
    require_batch_diversity: bool = True
    #: Fraction of each destination held back. A drive filled to the last byte
    #: cannot be defragmented, cannot take a manifest, and on NTFS slows down
    #: badly; a plan that fills one is a plan that causes a problem later.
    reserve_fraction: float = 0.10
    #: Years over which durability is valued. Expected loss is a rate —
    #: bytes per year — and the cost of copying is paid once. Without a horizon
    #: the two cannot be added at all: a one-off copy of 1.6 MB would be scored
    #: against the ~24 KB/year of loss it prevents, and *every* objective
    #: comparison would favour doing nothing. Five years is the interval over
    #: which the AFR tables are meaningful and roughly a drive generation.
    horizon_years: int = 5
    #: Objective weights (§9), applied to terms normalised to a fraction of the
    #: archive so they are commensurable. Durability dominates by design: the
    #: tool exists to stop data being lost, not to minimise plugging drives in.
    weight_durability: float = 1.0
    weight_bytes_copied: float = 0.05
    weight_drives_touched: float = 0.02
    weight_imbalance: float = 0.05
    #: What a missing copy costs **inside the CP-SAT model**, which has to
    #: choose in a single pass and so needs the deficit priced. Between
    #: finished solutions an unmet copy target is not weighed at all: it is a
    #: policy violation, and ``solver.is_better`` compares it first. Set below
    #: the cost of copying, a weighted comparison prefers leaving data
    #: unprotected — which is what an objective-driven solver will then do.
    weight_unmet_target: float = 4.0
    #: Drives the user has excluded as destinations, by id.
    excluded_drives: tuple[int, ...] = ()
    notes: str = ""
    #: Rubric versions this policy derives from, for the plan's provenance.
    standards: dict = field(default_factory=dict)

    @classmethod
    def for_level(cls, level: int = DEFAULT_LEVEL, **overrides) -> "Policy":
        """A policy from an NDSA level, with the rubric version recorded."""
        level = max(1, min(4, int(level)))
        rubric = standards.ndsa_levels()
        base = {
            "min_copies": COPIES_BY_LEVEL[level],
            "min_zones": ZONES_BY_LEVEL[level],
            "standards": {"ndsa": rubric.citation},
        }
        base.update(overrides)
        return cls(**base)

    def copies_for(self, collection_target: int | None) -> int:
        """Copies required for a unit, taking the stricter of the two rules.

        A collection asking for level 3 inside an archive whose default is
        level 1 gets level 3; the reverse does not weaken it.
        """
        if collection_target is None:
            return self.min_copies
        level = max(1, min(4, int(collection_target)))
        return max(self.min_copies, COPIES_BY_LEVEL[level])

    def zones_for(self, collection_target: int | None) -> int:
        if collection_target is None:
            return self.min_zones
        level = max(1, min(4, int(collection_target)))
        return max(self.min_zones, ZONES_BY_LEVEL[level])

    def usable_bytes(self, capacity: int | None) -> int:
        """Capacity a plan may fill, after the reserve."""
        if not capacity:
            return 0
        return int(capacity * (1.0 - self.reserve_fraction))

    def snapshot(self) -> str:
        """JSON for the plan record, so a plan says what it was solved against."""
        return json.dumps(asdict(self), sort_keys=True)

    def describe(self) -> str:
        parts = [f"{self.min_copies} copies on distinct drives"]
        if self.min_zones > 1:
            parts.append(f"across {self.min_zones} disaster zones")
        if self.require_batch_diversity:
            parts.append("not all from one purchase batch")
        parts.append(f"{self.reserve_fraction:.0%} of each drive held in reserve")
        return "; ".join(parts)


def from_snapshot(text: str) -> Policy:
    """Rebuild a policy from a plan's stored snapshot."""
    data = json.loads(text)
    data["excluded_drives"] = tuple(data.get("excluded_drives", ()))
    return Policy(**data)
