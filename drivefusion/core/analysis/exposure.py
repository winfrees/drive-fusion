"""Exposure: what the redundancy picture costs in expected bytes per year.

M3 ranked the under-protected queue by size and said so in a comment: "the best
available proxy until M5 supplies each drive's failure probability and the
ranking becomes size × P(loss)". This module is that, and the ranking now uses
it.

Why the change matters. Ranked by size, the top of the queue is whatever is
biggest — which is usually a pile of video on a healthy two-year-old drive.
Ranked by expected loss, the top is content on the drive with pending sectors,
which may be a hundredth the size and is the thing that will actually be gone
next month. Those are different lists, and only one of them is a work queue.

Every figure here carries a confidence label, because most of them rest on
estimates: a drive behind a USB bridge reports no SMART, and an honest "we do
not know, here is the age-based guess" is worth more than a decimal place.
"""

from __future__ import annotations

from dataclasses import dataclass

from drivefusion.core.analysis.duplicates import DRIVE_KEY
import drivefusion.core.health.risk as risk_module
from drivefusion.core.health.collect import reading_from_row
from drivefusion.core.health.afr import AfrTable
from drivefusion.core.health.risk import DriveRisk
from drivefusion.core.store.catalog import Catalog


@dataclass(frozen=True)
class ContentExposure:
    """One content item, the drives holding it, and what that costs per year."""

    content_id: int
    size_bytes: int
    paths: int
    drive_ids: tuple[int | None, ...]
    loss_probability: float
    confidence: str

    @property
    def expected_bytes_lost_per_year(self) -> float:
        return self.size_bytes * self.loss_probability

    @property
    def drives(self) -> int:
        return len(self.drive_ids)


class RiskIndex:
    """Per-drive risk, computed once and reused across thousands of rows.

    Recomputing a drive's failure probability for every file on it would make a
    report over a 50M-file catalog quadratic in the worst way; there are twenty
    drives and millions of files.
    """

    def __init__(self, catalog: Catalog, *, table: AfrTable | None = None) -> None:
        self.catalog = catalog
        self.table = table
        self._drives: dict[int, dict] = {}
        self._risks: dict[int | None, DriveRisk] = {}

        for row in catalog.drives():
            drive = dict(row)
            drive_id = int(drive["id"])
            self._drives[drive_id] = drive
            reading = reading_from_row(catalog.latest_health(drive_id))
            self._risks[drive_id] = risk_module.drive_risk(
                drive, reading, table=table
            )

    def risk_for(self, drive_key: int | None) -> DriveRisk:
        """Risk for a drive key as the copy-counting rule produces it.

        Negative keys are volumes with no identified physical drive (the
        ``COALESCE(drive_id, -volume_id)`` convention from ``duplicates.py``).
        Nothing is known about such a device, so it gets the unknown-drive
        estimate rather than being dropped from the calculation — dropping it
        would quietly improve the durability of everything stored on it.
        """
        known = self._risks.get(drive_key) if drive_key is not None else None
        if known is not None:
            return known

        placeholder = risk_module.drive_risk(
            {"id": drive_key, "media": None, "bus": None}, None, table=self.table
        )
        self._risks[drive_key] = placeholder
        return placeholder

    def drive_row(self, drive_key: int | None) -> dict:
        if drive_key is not None and drive_key in self._drives:
            return self._drives[drive_key]
        return {"id": drive_key}

    def exposure_for(self, drive_keys: tuple[int | None, ...]) -> tuple[float, str]:
        risks = [self.risk_for(key) for key in drive_keys]
        rows = [self.drive_row(key) for key in drive_keys]
        probability = risk_module.loss_probability(risks, rows, table=self.table)
        return probability, risk_module.worst_confidence(risks)


def _content_rows(catalog: Catalog, *, min_size: int = 0):
    """Content with the set of drive keys holding it, in one pass."""
    return catalog.conn.execute(
        "SELECT c.id AS content_id, c.size_bytes, COUNT(f.id) AS paths, "
        f"  GROUP_CONCAT(DISTINCT {DRIVE_KEY}) AS drive_keys "
        "FROM file f "
        "JOIN content c ON c.id = f.content_id "
        "JOIN volume v ON v.id = f.volume_id "
        "WHERE f.vanished_at IS NULL AND f.content_id IS NOT NULL "
        "  AND c.size_bytes >= ? "
        "GROUP BY c.id",
        (min_size,),
    )


def _keys(text: str | None) -> tuple[int | None, ...]:
    if not text:
        return ()
    return tuple(int(part) for part in text.split(",") if part.strip())


def content_exposure(
    catalog: Catalog,
    *,
    table: AfrTable | None = None,
    index: RiskIndex | None = None,
    min_size: int = 0,
    limit: int | None = None,
) -> list[ContentExposure]:
    """Expected annual loss per content item, worst first."""
    index = index or RiskIndex(catalog, table=table)

    exposures: list[ContentExposure] = []
    for row in _content_rows(catalog, min_size=min_size):
        keys = _keys(row["drive_keys"])
        probability, confidence = index.exposure_for(keys)
        exposures.append(
            ContentExposure(
                content_id=int(row["content_id"]),
                size_bytes=int(row["size_bytes"]),
                paths=int(row["paths"]),
                drive_ids=keys,
                loss_probability=probability,
                confidence=confidence,
            )
        )

    exposures.sort(key=lambda e: e.expected_bytes_lost_per_year, reverse=True)
    return exposures[:limit] if limit else exposures


def expected_bytes_lost_per_year(
    catalog: Catalog,
    *,
    table: AfrTable | None = None,
    index: RiskIndex | None = None,
) -> dict:
    """The headline figure, with the coverage it rests on.

    ``unhashed_files`` is repeated here deliberately: content with no identity
    is not in this sum at all, and a total presented without that caveat would
    understate the archive's exposure while looking authoritative.
    """
    index = index or RiskIndex(catalog, table=table)
    exposures = content_exposure(catalog, index=index)

    total = sum(e.expected_bytes_lost_per_year for e in exposures)
    measured = sum(
        e.expected_bytes_lost_per_year
        for e in exposures
        if e.confidence == risk_module.MEASURED
    )
    unhashed = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file "
        "WHERE vanished_at IS NULL AND content_id IS NULL"
    ).fetchone()["n"]

    by_confidence: dict[str, int] = {}
    for exposure in exposures:
        by_confidence[exposure.confidence] = by_confidence.get(exposure.confidence, 0) + 1

    return {
        "expected_bytes_lost_per_year": total,
        "measured_share": (measured / total) if total else 0.0,
        "content_items": len(exposures),
        "by_confidence": by_confidence,
        "unhashed_files": int(unhashed),
        "table": (index.table or _default_table()).citation,
        "generic_rates": (index.table or _default_table()).is_generic,
    }


def _default_table() -> AfrTable:
    from drivefusion.core.health import afr as afr_module

    return afr_module.load()


def riskiest_drives(
    catalog: Catalog, *, table: AfrTable | None = None, limit: int = 10
) -> list[dict]:
    """Drives ranked by how much unique content they alone are holding up.

    "Riskiest" is not the drive most likely to fail: it is the drive whose
    failure would cost the most, which is failure probability times the bytes
    that exist nowhere else.
    """
    index = RiskIndex(catalog, table=table)
    sole_bytes: dict[int | None, int] = {}

    for row in _content_rows(catalog):
        keys = _keys(row["drive_keys"])
        if len(keys) == 1:
            sole_bytes[keys[0]] = sole_bytes.get(keys[0], 0) + int(row["size_bytes"])

    out = []
    for key, only_here in sole_bytes.items():
        risk = index.risk_for(key)
        drive = index.drive_row(key)
        out.append(
            {
                "drive_id": key,
                "name": (
                    drive.get("nickname")
                    or drive.get("model")
                    or drive.get("serial")
                    or (f"unmapped volume {-key}" if key and key < 0 else f"drive {key}")
                ),
                "annual_probability": risk.annual_probability,
                "confidence": risk.confidence,
                "sole_copy_bytes": only_here,
                "expected_bytes_lost_per_year": only_here * risk.annual_probability,
                "reasons": risk.reasons,
            }
        )

    out.sort(key=lambda d: d["expected_bytes_lost_per_year"], reverse=True)
    return out[:limit]
