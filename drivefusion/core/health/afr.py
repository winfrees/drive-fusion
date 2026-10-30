"""A drive's annualised failure rate (PLAN §8.2, ROADMAP M5.4).

```
f_d = base_afr(model_family) × age_factor × duty_factor × smart_factor × bus_factor
```

The base rates and every factor's parameters are data, not code: versioned
files under ``drivefusion/standards/afr/`` that name their source, so a
report can say which version it scored against (PLAN §8.3). Each factor is a
pure function that is **1.0 when its input is unknown**: a missing fact never
makes a drive look safer, it only makes the estimate less certain.

``confidence`` says how much is known:

* ``measured``: a SMART reading fed the duty and SMART factors;
* ``estimated``: no SMART reading, but the base rate matched the drive's
  model or media class (SMART reads are shelved, so this is the usual case);
* ``unknown``: neither model nor media matched, and the purchase date is
  missing, so the rate is a placeholder for "a drive".
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

#: Fleet statistics first, then manufacturer ratings, then placeholders.
BASIS_ORDER = ("fleet", "rated", "provisional")

#: Model prefixes vendors put in front of the part number in some listings.
_VENDOR_PREFIXES = ("HGST", "HITACHI", "WDC", "WD", "TOSHIBA", "SEAGATE", "SAMSUNG", "CT")

#: A model string shorter than this can't identify a family.
_MIN_MATCH = 6


@dataclass(frozen=True)
class RateTable:
    source: str
    version: str
    basis: str
    classes: dict[str, float]
    models: tuple[tuple[str, float], ...]      # (normalised model, AFR), longest first

    @property
    def name(self) -> str:
        return f"{self.source}-{self.version}"


@dataclass(frozen=True)
class Model:
    version: str
    max_afr: float
    age_years: dict[str, list]
    duty: dict[str, list]
    smart: dict[str, float]
    bus: dict[str, float]


@dataclass
class DriveRisk:
    """One drive's estimated AFR and how it was reached."""

    afr: float
    confidence: str
    base: float
    base_from: str                                  # "model ST4000DM004 (backblaze-2025Q2)"
    factors: dict[str, float] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    model_version: str = ""


# -- the data files --------------------------------------------------------------

def _files() -> list[dict]:
    folder = resources.files("drivefusion.standards.afr")
    return [json.loads(entry.read_text(encoding="utf-8"))
            for entry in sorted(folder.iterdir(), key=lambda e: e.name)
            if entry.name.endswith(".json")]


def normalise_model(text: str | None) -> str:
    value = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    for prefix in _VENDOR_PREFIXES:
        if value.startswith(prefix) and len(value) - len(prefix) >= _MIN_MATCH:
            return value[len(prefix):]
    return value


def rate_table(data: dict) -> RateTable:
    models = sorted(((normalise_model(m["model"]), float(m["afr"])) for m in data.get("models", [])),
                    key=lambda pair: -len(pair[0]))
    return RateTable(data["source"], str(data["version"]), data.get("basis", "provisional"),
                     {k: float(v) for k, v in (data.get("classes") or {}).items()},
                     tuple(models))


@lru_cache(maxsize=1)
def tables() -> tuple[RateTable, ...]:
    """Every shipped rate table, best basis first."""
    found = [rate_table(d) for d in _files() if d.get("kind") == "afr-rates"]
    rank = {basis: i for i, basis in enumerate(BASIS_ORDER)}
    return tuple(sorted(found, key=lambda t: (rank.get(t.basis, len(rank)), t.name)))


@lru_cache(maxsize=1)
def model() -> Model:
    [data] = [d for d in _files() if d.get("kind") == "afr-model"]
    return Model(str(data["version"]), float(data["max_afr"]), data["age_years"],
                 data["duty"], data["smart"], data["bus"])


# -- the factors, each pure --------------------------------------------------------

def _in_ranges(value: float, ranges: list) -> float:
    for low, high, factor in ranges:
        if value >= low and (high is None or value < high):
            return float(factor)
    return 1.0


def _media(media: str | None) -> str:
    return media if media in ("hdd", "ssd") else "unknown"


def base_afr(model_name: str | None, media: str | None,
             rate_tables: tuple[RateTable, ...]) -> tuple[float, str, str]:
    """The base rate, what it matched (``model``, ``class`` or ``fallback``),
    and a description of where it came from."""
    wanted = normalise_model(model_name)
    if len(wanted) >= _MIN_MATCH:
        for table in rate_tables:
            for family, rate in table.models:
                if len(family) >= _MIN_MATCH and wanted.startswith(family):
                    return rate, "model", f"model {family} ({table.name})"
    kind = _media(media)
    for table in rate_tables:
        if kind in table.classes:
            match = "class" if kind != "unknown" else "fallback"
            return table.classes[kind], match, f"{kind} class ({table.name})"
    raise LookupError("no AFR table gives a rate for this drive")


def age_years(purchase_date: str | None, today: dt.date) -> float | None:
    if not purchase_date:
        return None
    parts = [int(p) for p in purchase_date[:10].split("-")]
    bought = dt.date(parts[0], parts[1], parts[2] if len(parts) > 2 else 15)
    return max(0.0, (today - bought).days / 365.25)


def age_factor(years: float | None, media: str | None, params: Model) -> float:
    if years is None:
        return 1.0
    return _in_ranges(years, params.age_years[_media(media)])


def duty_factor(media: str | None, power_on_hours: int | None,
                life_remaining: int | None, params: Model) -> float:
    if media == "hdd" and power_on_hours is not None:
        return _in_ranges(power_on_hours, params.duty["hdd_power_on_hours"])
    if media == "ssd" and life_remaining is not None:
        return _in_ranges(life_remaining, params.duty["ssd_life_remaining_percent"])
    return 1.0


def smart_factor(reallocated: int | None, pending: int | None, uncorrectable: int | None,
                 crc: int | None, params: Model) -> float:
    """The worst sector sign sets the factor; a pending sector is a step
    change and dominates. CRC errors (usually the cable) add a little."""
    s = params.smart
    sectors = 1.0
    if pending:
        sectors = max(sectors, s["pending"])
    if uncorrectable:
        sectors = max(sectors, s["uncorrectable"])
    if reallocated:
        many = reallocated >= s["reallocated_many_from"]
        sectors = max(sectors, s["reallocated_many"] if many else s["reallocated"])
    return sectors * (s["crc"] if crc else 1.0)


def bus_factor(bus: str | None, params: Model) -> float:
    return float(params.bus.get(bus or "", 1.0))


# -- one drive ---------------------------------------------------------------------

def drive_risk(drive, *, today: dt.date | None = None, health=None,
               rate_tables: tuple[RateTable, ...] | None = None,
               params: Model | None = None) -> DriveRisk:
    """The estimated AFR of a ``drive`` row (or any mapping with its columns).

    ``health`` is the drive's latest ``drive_health`` row, if any; only a
    ``measured`` one is used.
    """
    today = today or dt.date.today()
    rate_tables = tables() if rate_tables is None else rate_tables
    params = params or model()
    media = drive["media"]
    assumptions = []

    base, matched, base_from = base_afr(drive["model"], media, rate_tables)
    if matched != "model":
        assumptions.append(f"no rate for this model; used the {base_from}")

    years = age_years(drive["purchase_date"], today)
    if years is None:
        assumptions.append("purchase date unknown: age not counted")

    measured = health is not None and health["confidence"] == "measured"
    if not measured:
        assumptions.append("no SMART reading: duty and sector health not counted")
    h = dict(health) if measured else {}
    factors = {
        "age": age_factor(years, media, params),
        "duty": duty_factor(media, h.get("power_on_hours"), h.get("wear_leveling"), params),
        "smart": smart_factor(h.get("reallocated"), h.get("pending"), h.get("uncorrectable"),
                              h.get("crc_errors"), params),
        "bus": bus_factor(drive["bus"], params),
    }
    afr = base
    for value in factors.values():
        afr *= value
    if measured:
        confidence = "measured"
    elif matched == "fallback" and years is None:
        confidence = "unknown"
    else:
        confidence = "estimated"
    return DriveRisk(min(afr, params.max_afr), confidence, base, base_from, factors,
                     assumptions, params.version)
