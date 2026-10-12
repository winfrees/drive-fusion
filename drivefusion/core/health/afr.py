"""Annualized failure rates, loaded from a versioned file rather than compiled in.

A rubric that lives in code cannot be cited, cannot be updated without a
release, and cannot tell a report which version it scored against. Per §8.3
every rubric is a versioned data file naming its source and retrieval date, and
the AFR table is the first of them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_TABLE = "afr-defaults-v1.json"


def _standards_dir() -> Path:
    """Where the shipped tables live, in a checkout and in a frozen build.

    PyInstaller unpacks data files under ``sys._MEIPASS``, so a path derived from
    ``__file__`` alone resolves inside the bundle's module tree and finds
    nothing. Getting this wrong would not fail in CI — it would ship an
    executable that raises AfrError the first time a user asked for a report.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "standards" / "afr"
    return Path(__file__).resolve().parents[3] / "standards" / "afr"


#: Location of the shipped tables. Resolved once; see :func:`_standards_dir`.
STANDARDS_DIR = _standards_dir()


class AfrError(RuntimeError):
    """The table is missing or malformed. Never silently substituted."""


@dataclass(frozen=True)
class Band:
    low: float
    high: float | None
    multiplier: float

    def contains(self, value: float) -> bool:
        if value < self.low:
            return False
        return self.high is None or value < self.high


def _bands(raw) -> tuple[Band, ...]:
    return tuple(Band(float(low), None if high is None else float(high), float(m))
                 for low, high, m in raw)


def lookup(bands: tuple[Band, ...], value: float | None, *, default: float = 1.0) -> float:
    """The multiplier whose band contains ``value``.

    ``None`` means the drive did not report it, and an unreported measurement
    must not act as a discount: it returns the neutral multiplier and the caller
    records lowered confidence instead.
    """
    if value is None:
        return default
    for band in bands:
        if band.contains(value):
            return band.multiplier
    return bands[-1].multiplier if bands else default


@dataclass(frozen=True)
class AfrTable:
    """A loaded, versioned AFR reference."""

    id: str
    version: str
    retrieved_at: str
    kind: str
    base_afr: dict
    age_bands: tuple[Band, ...]
    hdd_duty_bands: tuple[Band, ...]
    ssd_duty_bands: tuple[Band, ...]
    reallocated_bands: tuple[Band, ...]
    crc_bands: tuple[Band, ...]
    pending_multiplier: float
    uncorrectable_multiplier: float
    unverified_multiplier: float
    bus_factors: dict
    same_batch_multiplier: float
    same_zone_multiplier: float
    per_model: dict

    @property
    def citation(self) -> str:
        """What a report prints to say which rubric produced its numbers."""
        return f"{self.id} v{self.version} ({self.kind}, retrieved {self.retrieved_at})"

    @property
    def is_generic(self) -> bool:
        """True when these are defaults rather than published fleet data.

        Reports say so. A number from a generic default and a number from
        Backblaze's published stats should not look alike on the page.
        """
        return self.kind == "generic-defaults"

    def base_for(self, media: str | None, model: str | None) -> tuple[float, bool]:
        """Baseline AFR and whether it came from per-model published data."""
        if model:
            entry = self.per_model.get(_normalise(model))
            if isinstance(entry, dict) and "afr" in entry:
                return float(entry["afr"]), True
        key = (media or "unknown").lower()
        return float(self.base_afr.get(key, self.base_afr["unknown"])), False


def _normalise(model: str) -> str:
    return " ".join(model.split()).upper()


@lru_cache(maxsize=4)
def load(path: str | None = None) -> AfrTable:
    """Load a table. Cached, because it is read per drive per report."""
    target = Path(path) if path else STANDARDS_DIR / DEFAULT_TABLE
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AfrError(f"no AFR table at {target}") from exc
    except json.JSONDecodeError as exc:
        raise AfrError(f"AFR table at {target} is not valid JSON: {exc}") from exc

    try:
        smart = document["smart_factor"]
        return AfrTable(
            id=document["id"],
            version=document["version"],
            retrieved_at=document["retrieved_at"],
            kind=document.get("kind", "unknown"),
            base_afr=document["base_afr"],
            age_bands=_bands(document["age_factor"]["bands"]),
            hdd_duty_bands=_bands(document["duty_factor"]["hdd_power_on_hours"]),
            ssd_duty_bands=_bands(document["duty_factor"]["ssd_fraction_of_endurance"]),
            reallocated_bands=_bands(smart["reallocated"]["bands"]),
            crc_bands=_bands(smart["crc_errors"]["bands"]),
            pending_multiplier=float(smart["pending_sector"]["any"]),
            uncorrectable_multiplier=float(smart["uncorrectable"]["any"]),
            unverified_multiplier=float(smart["unverified"]["multiplier"]),
            bus_factors=document["bus_factor"],
            same_batch_multiplier=float(document["correlation"]["same_batch_key"]),
            same_zone_multiplier=float(document["correlation"]["same_disaster_zone"]),
            per_model={
                _normalise(k): v
                for k, v in (document.get("per_model") or {}).items()
                if not k.startswith("$")
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AfrError(f"AFR table at {target} is missing or malformed: {exc}") from exc
