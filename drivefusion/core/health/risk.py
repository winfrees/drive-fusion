"""The durability model: turning drives into a probability, and bytes into risk.

Implements §8.2. The shape is::

    f_d = base_afr(model)      # published per-model data, or a generic default
          × age_factor         # bathtub: elevated year 0, floor 1-3, rising 4+
          × duty_factor        # HDD power-on hours; SSD writes against endurance
          × smart_factor       # reallocated, pending, uncorrectable, CRC
          × bus_factor         # a USB bridge is part of the failure unit

    P(loss of content on drive set D) = Π f_d × corr(D)

    expected_bytes_lost_per_year = Σ ( size × P(loss) )

Three rules that matter more than the arithmetic:

**An unreported measurement is never a discount.** A drive behind a USB bridge
that passes no SMART through has not proved it has zero reallocated sectors. It
gets the neutral multiplier and a *lowered confidence label*, never a clean bill
of health.

**A pending sector dominates.** It means the drive holds data it could not read
and has not yet relocated. That is a different kind of fact from a slightly
elevated hours count, and the model treats it as a step change.

**Correlation is the point, not a refinement.** Two copies bought in the same
order, from the same maker, sitting in the same room, are not two independent
copies. Multiplying their failure probabilities as if they were is the single
most flattering error this model could make, so `corr(D)` raises the joint
probability when drives share a batch or a location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import drivefusion.core.health.afr as afr_module
from drivefusion.core.health.afr import AfrTable, lookup
from drivefusion.core.health.smart import SmartReading

#: Confidence in a per-drive estimate, worst to best.
UNKNOWN = "unknown"
ESTIMATED = "estimated"
MEASURED = "measured"

_RANK = {UNKNOWN: 0, ESTIMATED: 1, MEASURED: 2}

#: A probability is still a probability. Even a drive failing every check has
#: some chance of surviving the year, and a model that returns 1.0 would claim
#: certainty it cannot have.
MAX_ANNUAL_PROBABILITY = 0.95


@dataclass(frozen=True)
class DriveRisk:
    """One drive's annual failure probability, and how it was arrived at."""

    drive_id: int | None
    annual_probability: float
    confidence: str
    factors: dict = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def is_measured(self) -> bool:
        return self.confidence == MEASURED

    def explain(self) -> str:
        """One line a human can check the arithmetic against."""
        parts = [f"{name} ×{value:.2f}" for name, value in self.factors.items()]
        return (
            f"{self.annual_probability * 100:.1f}%/yr ({self.confidence}): "
            + ", ".join(parts)
        )


def _years_since(date_text: str | None, *, now: datetime | None = None) -> float | None:
    if not date_text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            when = datetime.strptime(date_text[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
        when = when.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - when).total_seconds() / (365.25 * 24 * 3600))
    return None


def drive_risk(
    drive: dict,
    reading: SmartReading | None = None,
    *,
    table: AfrTable | None = None,
    now: datetime | None = None,
) -> DriveRisk:
    """Annual failure probability for one drive.

    ``drive`` is a row from the ``drive`` table (or a dict shaped like one).
    ``reading`` is the most recent SMART observation, or ``None``.
    """
    table = table or afr_module.load()
    reasons: list[str] = []
    factors: dict[str, float] = {}

    model = drive.get("model")
    media = (reading.media if reading else None) or drive.get("media")
    base, from_published = table.base_for(media, model)
    factors["base"] = base
    if from_published:
        reasons.append(f"published failure rate for {model}")
    else:
        reasons.append(f"generic {media or 'unknown'} baseline ({table.version})")

    # Age. Without a purchase date there is no age factor and no pretending.
    age_years = _years_since(drive.get("purchase_date"), now=now)
    if age_years is None:
        factors["age"] = 1.0
        reasons.append("no purchase date recorded, so no age adjustment")
    else:
        factors["age"] = lookup(table.age_bands, age_years)
        reasons.append(f"{age_years:.1f} years old")

    # Duty.
    duty = 1.0
    if reading is not None:
        if media in ("ssd", "nvme") and reading.wear_leveling is not None:
            duty = lookup(table.ssd_duty_bands, reading.wear_leveling / 100.0)
            reasons.append(f"{reading.wear_leveling}% of write endurance used")
        elif reading.power_on_hours is not None:
            duty = lookup(table.hdd_duty_bands, reading.power_on_hours)
            reasons.append(f"{reading.power_on_hours:,} power-on hours")
    factors["duty"] = duty

    # SMART. A drive verified clean and a drive that reported nothing are not in
    # the same state: the second could have fifty pending sectors with no way to
    # tell. Uncertainty is priced rather than waved through, so silence costs a
    # modest multiplier *and* drops the confidence label — pricing it at 1.0
    # would rank an unverifiable drive alongside one known to be healthy.
    smart = 1.0
    if reading is None or not reading.has_counters:
        smart = table.unverified_multiplier
    if reading is not None and reading.has_counters:
        if reading.pending:
            smart *= table.pending_multiplier
            reasons.append(
                f"{reading.pending} pending sector(s) — unreadable data awaiting "
                "relocation; back this drive up before anything else"
            )
        if reading.uncorrectable:
            smart *= table.uncorrectable_multiplier
            reasons.append(f"{reading.uncorrectable} uncorrectable error(s)")
        if reading.reallocated:
            smart *= lookup(table.reallocated_bands, reading.reallocated)
            reasons.append(f"{reading.reallocated} reallocated sector(s)")
        if reading.crc_errors:
            smart *= lookup(table.crc_bands, reading.crc_errors)
            reasons.append(
                f"{reading.crc_errors} interface CRC error(s) — usually a cable "
                "or bridge rather than the drive itself"
            )
        if reading.passed is False:
            smart *= table.pending_multiplier
            reasons.append("the drive's own SMART health check reports FAILING")
    factors["smart"] = smart

    bus = (drive.get("bus") or "unknown").lower()
    factors["bus"] = float(table.bus_factors.get(bus, table.bus_factors["unknown"]))
    if bus in ("usb", "ieee1394"):
        reasons.append(f"{bus} enclosure — the bridge is part of the failure unit")

    probability = min(
        MAX_ANNUAL_PROBABILITY,
        factors["base"] * factors["age"] * factors["duty"] * factors["smart"] * factors["bus"],
    )

    # Confidence describes the evidence, not the answer.
    if reading is not None and reading.has_counters:
        confidence = MEASURED
    elif age_years is not None or media:
        confidence = ESTIMATED
        reasons.append(
            "no SMART counters available — estimated from age and media class"
        )
    else:
        confidence = UNKNOWN
        reasons.append(
            "nothing known about this drive but its presence; treat the number "
            "as a placeholder"
        )

    return DriveRisk(
        drive_id=drive.get("id"),
        annual_probability=probability,
        confidence=confidence,
        factors=factors,
        reasons=tuple(reasons),
    )


def correlation(drives: list[dict], *, table: AfrTable | None = None) -> float:
    """How much less independent a set of drives is than it looks.

    Returns a multiplier ≥ 1 applied to the joint loss probability. Drives
    sharing a ``batch_key`` came from one order — same manufacturing run, same
    shelf, same duty cycle — and drives sharing a ``disaster_zone`` share a
    flood, a fire and a burglary.
    """
    table = table or afr_module.load()
    if len(drives) < 2:
        return 1.0

    multiplier = 1.0
    batches = [d.get("batch_key") for d in drives if d.get("batch_key")]
    if len(batches) > 1 and len(set(batches)) < len(batches):
        multiplier *= table.same_batch_multiplier

    zones = [d.get("disaster_zone") for d in drives if d.get("disaster_zone")]
    if len(zones) > 1 and len(set(zones)) < len(zones):
        multiplier *= table.same_zone_multiplier

    return multiplier


def loss_probability(
    risks: list[DriveRisk],
    drives: list[dict] | None = None,
    *,
    table: AfrTable | None = None,
) -> float:
    """Probability that every copy is lost within a year.

    Content on no drive at all is lost already, which is what an empty set
    means: probability 1.0, not 0.0. Returning 0.0 for "no copies" would rank
    the most endangered content in the archive as the safest.
    """
    if not risks:
        return 1.0

    joint = 1.0
    for risk in risks:
        joint *= risk.annual_probability

    if drives:
        joint *= correlation(drives, table=table)
    return min(1.0, joint)


def worst_confidence(risks: list[DriveRisk]) -> str:
    """A chain is as well-understood as its least-understood link."""
    if not risks:
        return UNKNOWN
    return min((r.confidence for r in risks), key=lambda c: _RANK[c])
