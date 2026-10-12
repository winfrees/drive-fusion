"""NDSA Levels of Digital Preservation, scored where a catalog can see them.

Scored across the five published functional areas at four levels. The honest
part of this module is what it *refuses* to score: several published rows are
about written policy, staff training and succession planning, and a catalog
cannot see any of them. Those are reported as **not assessable here** rather
than passed (which would be flattering) or failed (which would be unfair and
would make level 4 unreachable for everyone forever).

A level is reached only when every level at or below it is met. Reporting
"level 3" because one row happens to pass while level 2 does not would make the
number mean nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from drivefusion.core import standards
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.fair import scorecard as fair_scorecard
from drivefusion.core.store.catalog import Catalog

NOT_ASSESSABLE = "not-assessable"


@dataclass(frozen=True)
class LevelResult:
    level: int
    statement: str
    status: str          # pass | fail | not-assessable
    detail: str


@dataclass(frozen=True)
class AreaResult:
    area: str
    title: str
    reached: int
    levels: tuple[LevelResult, ...]

    @property
    def ceiling(self) -> int | None:
        """The highest level that could be reached given what is assessable.

        Without this, an area whose level 3 is policy-only looks permanently
        stuck at 2 with no explanation.
        """
        assessable = [
            result.level for result in self.levels if result.status != NOT_ASSESSABLE
        ]
        return max(assessable) if assessable else None


@dataclass(frozen=True)
class NdsaResult:
    collection_id: int | None
    title: str
    areas: tuple[AreaResult, ...]
    rubric: str

    @property
    def overall(self) -> int:
        """The weakest area, because preservation is a chain.

        Averaging the areas would let excellent storage paper over absent
        metadata, and the content would still be unfindable.
        """
        return min((area.reached for area in self.areas), default=0)

    def summary(self) -> str:
        weakest = min(self.areas, key=lambda a: a.reached, default=None)
        line = f"NDSA level {self.overall}"
        if weakest is not None and weakest.reached == self.overall:
            line += f" — held there by {weakest.title.lower()}"
        return line


def _check(name: str, target, measures: dict) -> tuple[str, str]:
    if name == "not_assessable":
        return NOT_ASSESSABLE, "not visible from a catalog"

    if name == "copies_at_least":
        drives = measures["drives"]
        return (
            ("pass" if drives >= target else "fail"),
            f"on {drives} distinct drive(s), target {target}",
        )

    if name == "distinct_zones_at_least":
        zones = measures["zones"]
        if not zones:
            return "fail", (
                "no disaster zones recorded — set them on the Drives screen so "
                "geographic separation can be assessed"
            )
        return (
            ("pass" if zones >= target else "fail"),
            f"{zones} distinct disaster zone(s), target {target}",
        )

    if name == "content_identified_share":
        share = measures["content_identified_share"]
        return (
            ("pass" if share >= target else "fail"),
            f"{share:.0%} of hashable content identified, target {target:.0%}",
        )

    if name == "fixity_checked_share":
        share = measures["fixity_checked_share"]
        return (
            ("pass" if share >= target else "fail"),
            f"{share:.0%} of files have a fixity record, target {target:.0%}",
        )

    if name == "fixity_incident_response":
        if not measures["fixity_checked"]:
            return "fail", "no fixity checks have been run"
        return "pass", (
            f"{measures['fixity_incidents']} incident(s) recorded and reported"
        )

    if name == "read_only_access":
        return "pass", "this tool holds read-only access by construction"

    if name == "inventory_exists":
        return (
            ("pass" if measures["files"] else "fail"),
            f"{measures['files']:,} files inventoried with their locations",
        )

    if name == "metadata_completeness":
        share = measures["metadata_completeness"]
        return (
            ("pass" if share >= target else "fail"),
            f"{share:.0%} of descriptive fields filled, target {target:.0%}",
        )

    if name == "metadata_standard_export":
        return NOT_ASSESSABLE, "standard metadata export arrives with M8"

    if name == "formats_recorded":
        return (
            ("pass" if measures["files"] else "fail"),
            "file formats recorded by extension (weaker than PRONOM "
            "identification, which is not implemented)",
        )

    if name == "format_risk_known":
        return "pass", (
            f"{measures['at_risk_share']:.0%} of bytes flagged as at-risk "
            "formats against the shipped vocabulary"
        )

    return NOT_ASSESSABLE, f"no check implemented for '{name}'"


def score(catalog: Catalog, collection_id: int) -> NdsaResult:
    """Score one collection against the NDSA levels."""
    rubric = standards.ndsa_levels()
    collection = collections_module.get(catalog, collection_id)
    measures = fair_scorecard._measures(catalog, collection)
    return _score_with(rubric, measures, collection.id, collection.title)


def _score_with(rubric, measures: dict, collection_id, title: str) -> NdsaResult:
    areas: list[AreaResult] = []
    for key, area in rubric["areas"].items():
        levels: list[LevelResult] = []
        reached = 0
        broken = False

        for number in sorted(area["levels"], key=int):
            spec = area["levels"][number]
            status, detail = _check(spec["check"], spec.get("value"), measures)
            levels.append(
                LevelResult(int(number), spec["statement"], status, detail)
            )
            # A level counts only if every level below it was met; a pass above
            # a failure is recorded but does not raise the number.
            if status == "pass" and not broken:
                reached = int(number)
            elif status == "fail":
                broken = True

        areas.append(
            AreaResult(key, area["title"], reached, tuple(levels))
        )

    return NdsaResult(
        collection_id=collection_id,
        title=title,
        areas=tuple(areas),
        rubric=rubric.citation,
    )


def score_unfiled(catalog: Catalog) -> NdsaResult:
    """Score the unfiled bucket, which is most of an archive to begin with.

    Reported because a level assigned to the curated 12% of a drive and
    presented as the archive's level would be the most misleading number the
    tool could print.
    """
    rubric = standards.ndsa_levels()
    where, params = collections_module.unfiled_dir_filter(catalog)

    totals = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
        "  COUNT(f.content_id) AS identified "
        f"FROM file f WHERE f.vanished_at IS NULL AND {where}",
        params,
    ).fetchone()

    hashable = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file f WHERE f.vanished_at IS NULL "
        "AND f.size_bytes IN (SELECT size_bytes FROM file "
        "  WHERE vanished_at IS NULL GROUP BY size_bytes HAVING COUNT(*) > 1) "
        f"AND {where}",
        params,
    ).fetchone()["n"]

    drives = catalog.conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(v.drive_id, -v.id)) AS n FROM file f "
        "JOIN volume v ON v.id = f.volume_id "
        f"WHERE f.vanished_at IS NULL AND {where}",
        params,
    ).fetchone()["n"]

    zones = catalog.conn.execute(
        "SELECT COUNT(DISTINCT d.disaster_zone) AS n FROM file f "
        "JOIN volume v ON v.id = f.volume_id JOIN drive d ON d.id = v.drive_id "
        f"WHERE f.vanished_at IS NULL AND d.disaster_zone IS NOT NULL AND {where}",
        params,
    ).fetchone()["n"]

    files = int(totals["files"])
    measures = {
        "files": files,
        "bytes": int(totals["bytes"]),
        "identified": int(totals["identified"]),
        "drives": int(drives),
        "zones": int(zones),
        "content_identified_share": (
            int(totals["identified"]) / int(hashable) if hashable else 1.0
        ),
        "fixity_checked": 0,
        "fixity_incidents": 0,
        "fixity_checked_share": 0.0,
        # Unfiled content has no descriptive metadata by definition. Scoring it
        # as 0 is the point, not an oversight.
        "metadata_completeness": 0.0,
        "at_risk_share": 0.0,
    }
    return _score_with(rubric, measures, None, "Unfiled content")
