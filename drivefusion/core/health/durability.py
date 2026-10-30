"""How likely each piece of content is to be lost, in bytes a year (PLAN §8.2, ROADMAP M5.5).

```
P(loss) = Π f_d × corr(D)            capped at the lowest f_d
expected_bytes_lost_per_year = Σ_over_content ( size × P(loss) )
```

``f_d`` is each drive's AFR (``core/health/afr.py``). ``corr(D)`` makes copies
that can fail together worth less: ``same_batch^(k-1)`` for each group of
``k`` drives from one purchase batch, ``same_zone^(k-1)`` for each group in
one disaster zone (parameters in ``standards/afr/model-*.json``). The cap
holds because losing every copy can't be likelier than losing the safest one.

P(loss) depends only on the *set* of drives holding a copy, and a catalog
has few distinct sets however many files it has, so the work is per set.
Drives are counted exactly as the redundancy figures count them
(``core/analysis/duplicates.py``): identified drives only, and a content item
whose every copy is on a volume with an unverified drive is treated as being
on one drive of unknown rate. Content not yet hashed whose size another file
shares could be a copy of anything and is left out, and said to be.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from drivefusion.core.analysis.duplicates import _LIVE, _SINGLE_COPY, _UNIQUE_SIZES, ContentGroup
from drivefusion.core.health import afr, metadata, smart

#: The drive set of content whose copies are all on unverified drives.
UNVERIFIED = ()

CONFIDENCE_ORDER = ("unknown", "estimated", "measured")

_UNVERIFIED_DRIVE = {"model": None, "media": None, "purchase_date": None, "bus": None}


@dataclass
class DriveSetRisk:
    """The content held on exactly one set of drives, and its chance of loss."""

    drive_ids: tuple[int, ...]
    p_loss: float
    confidence: str
    correlation: float
    items: int = 0
    bytes: int = 0
    assumptions: list[str] = field(default_factory=list)

    @property
    def expected_bytes_per_year(self) -> float:
        return self.bytes * self.p_loss


@dataclass
class Exposure:
    sets: list[DriveSetRisk]                # worst expected loss first
    expected_bytes_per_year: float
    confidence: str                         # the weakest of any set holding bytes
    unhashed_files: int                     # left out: could be copies of anything
    model_version: str
    rate_tables: list[str]


def _weakest(confidences) -> str:
    return min(confidences, key=CONFIDENCE_ORDER.index, default="unknown")


def correlation(drives: list, params: afr.Model) -> tuple[float, list[str]]:
    """``corr(D)`` for drive rows, and what it assumed."""
    corr = params.correlation
    factor, notes = 1.0, []
    batches = Counter(key for key, _ in (metadata.batch_key(d) for d in drives) if key)
    for size in batches.values():
        if size > 1:
            factor *= corr["same_batch"] ** (size - 1)
            notes.append(f"{size} drives from one purchase batch")
    zones = Counter(d["disaster_zone"] or None for d in drives)
    for zone, size in zones.items():
        if size > 1:
            factor *= corr["same_zone"] ** (size - 1)
            notes.append(f"{size} drives in disaster zone {zone}" if zone else
                         f"{size} drives with no disaster zone recorded, assumed together")
    return factor, notes


def set_risk(drive_ids: tuple[int, ...], risks: dict[int, afr.DriveRisk], drives: dict,
             params: afr.Model, unverified: afr.DriveRisk) -> DriveSetRisk:
    if not drive_ids:
        return DriveSetRisk(UNVERIFIED, unverified.afr, unverified.confidence, 1.0,
                            assumptions=["every copy is on a volume whose drive is unverified"])
    rates = [risks[d].afr for d in drive_ids]
    corr, notes = correlation([drives[d] for d in drive_ids], params)
    product = 1.0
    for rate in rates:
        product *= rate
    return DriveSetRisk(drive_ids, min(product * corr, min(rates)),
                        _weakest(risks[d].confidence for d in drive_ids), corr,
                        assumptions=notes)


#: One row per live content item: its size and its identified drives, as a
#: comma list (NULL when every copy is on an unverified drive).
_CONTENT_DRIVES = (
    "SELECT x.content_id AS ident, x.size_bytes, GROUP_CONCAT(x.drive_id) AS drives FROM ("
    "  SELECT DISTINCT f.content_id, c.size_bytes, v.drive_id FROM file f "
    "  JOIN content c ON c.id = f.content_id JOIN volume v ON v.id = f.volume_id "
    f"  WHERE {_LIVE}) x GROUP BY x.content_id"
)

#: Single-copy files of unique size, likewise.
_FILE_DRIVES = (
    "SELECT f.id AS ident, f.size_bytes, CAST(v.drive_id AS TEXT) AS drives FROM file f "
    f"JOIN volume v ON v.id = f.volume_id WHERE {_SINGLE_COPY}"
)


def _key(drives: str | None) -> tuple[int, ...]:
    return tuple(sorted({int(d) for d in drives.split(",")})) if drives else UNVERIFIED


def exposure(catalog, *, today: dt.date | None = None) -> Exposure:
    """Every drive set's P(loss), the bytes it holds, and the expected loss.

    The grouping is done by SQLite, so the rows that reach Python are one per
    drive set, not one per file.
    """
    params = afr.model()
    tables = afr.tables(catalog)

    totals: dict[tuple[int, ...], list[int]] = defaultdict(lambda: [0, 0])
    for query in (_CONTENT_DRIVES, _FILE_DRIVES):
        for row in catalog.conn.execute(
            f"SELECT drives, COUNT(*) AS items, SUM(size_bytes) AS bytes FROM ({query}) "
            "GROUP BY drives"
        ):
            total = totals[_key(row["drives"])]
            total[0] += row["items"]
            total[1] += row["bytes"] or 0

    all_ids = {d for key in totals for d in key}
    drives = {d: catalog.drive(d) for d in all_ids}
    risks = {d: afr.drive_risk(drives[d], today=today, health=smart.latest(catalog, d),
                               rate_tables=tables, params=params) for d in all_ids}
    unverified = afr.drive_risk(_UNVERIFIED_DRIVE, today=today, rate_tables=tables,
                                params=params)
    sets = []
    for key, (items, size) in totals.items():
        risk = set_risk(key, risks, drives, params, unverified)
        risk.items, risk.bytes = items, size
        sets.append(risk)
    sets.sort(key=lambda s: s.expected_bytes_per_year, reverse=True)

    unhashed = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file WHERE vanished_at IS NULL AND content_id IS NULL "
        f"AND size_bytes NOT IN ({_UNIQUE_SIZES})"
    ).fetchone()["n"]
    return Exposure(
        sets, sum(s.expected_bytes_per_year for s in sets),
        _weakest(s.confidence for s in sets if s.bytes) if sets else "unknown",
        int(unhashed), params.version, [t.name for t in tables],
    )


def drive_sets(catalog) -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[int, ...]]]:
    """Each content item's drive set, and each single-copy file's."""
    return tuple({row["ident"]: _key(row["drives"]) for row in catalog.conn.execute(query)}
                 for query in (_CONTENT_DRIVES, _FILE_DRIVES))


def ranked_under_protected(catalog, groups: list[ContentGroup],
                           found: Exposure | None = None) -> list[tuple[ContentGroup, DriveSetRisk]]:
    """Under-protected content ranked by size × P(loss) instead of size."""
    found = found or exposure(catalog)
    content_sets, file_sets = drive_sets(catalog)
    by_key = {s.drive_ids: s for s in found.sets}
    paired = []
    for group in groups:
        key = (content_sets.get(group.content_id) if group.content_id is not None
               else file_sets.get(group.file_id))
        if key in by_key:
            paired.append((group, by_key[key]))
    paired.sort(key=lambda pair: pair[0].size_bytes * pair[1].p_loss, reverse=True)
    return paired
