"""FAIR scorecard, scored per collection against a versioned rubric.

§8.3. Each failing criterion names the gap *and its remedy*, because a
scorecard that only grades is a reproach; one that says what to do next is a
work queue.

Three rules this module holds to:

**A missing licence is a top-line finding.** Unlicensed data is not reusable by
anyone else, whatever else is in place, so R1.1 is blocking: the grade is capped
at "incomplete" however well the rest scores. "Not for sharing" is a clear
answer and passes; blank does not.

**Not-applicable is not a pass.** A criterion this build cannot assess is
excluded from the denominator and listed, so a score of 1.0 over four of
fourteen criteria cannot masquerade as a perfect one. Every criterion in the
shipped rubric is assessable as of v2.0.0: F3 and I1 were the last two
excluded, and they became answerable once export bundles existed — they fail
before a bundle is written and pass after. The remaining case is a newer
rubric naming a check this build does not implement, which is why the
mechanism stays.

**A score names its rubric version.** The criteria below are this tool's
operationalisation of the published principles, not the principles themselves,
and a changed interpretation must be visible as a changed version.
"""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass, field

from drivefusion.core import standards
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.store.catalog import Catalog

PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "n/a"
NOT_ASSESSABLE = "not-assessable"


@dataclass(frozen=True)
class CriterionResult:
    id: str
    principle: str
    title: str
    status: str
    weight: float
    blocking: bool = False
    detail: str = ""
    gap: str = ""
    remedy: str = ""

    @property
    def counts(self) -> bool:
        """Whether this criterion is in the denominator."""
        return self.status in (PASS, FAIL)


@dataclass(frozen=True)
class Scorecard:
    collection_id: int
    local_pid: str
    title: str
    score: float
    grade: str
    results: tuple[CriterionResult, ...]
    rubric: str
    measures: dict = field(default_factory=dict)

    @property
    def failures(self) -> tuple[CriterionResult, ...]:
        return tuple(r for r in self.results if r.status == FAIL)

    @property
    def blocking_failures(self) -> tuple[CriterionResult, ...]:
        return tuple(r for r in self.failures if r.blocking)

    @property
    def not_assessed(self) -> tuple[CriterionResult, ...]:
        return tuple(r for r in self.results if not r.counts)

    @property
    def assessed(self) -> int:
        return sum(1 for r in self.results if r.counts)

    def summary(self) -> str:
        line = f"{self.grade} ({self.score:.0%} of {self.assessed} assessed criteria)"
        if self.blocking_failures:
            ids = ", ".join(r.id for r in self.blocking_failures)
            line += f" — blocked on {ids}"
        return line


def _exports(catalog: Catalog, collection_id: int) -> list[dict]:
    """Export records covering this collection, newest first.

    Read here rather than imported from ``core.export`` so that scoring a
    collection never pulls the export package in — the scorecard is a reader,
    and the export package is the one thing in the tool that writes files.
    """
    try:
        rows = catalog.conn.execute(
            "SELECT e.id, e.created_at, x.has_manifest, x.has_ro_crate, "
            "       x.has_datacite "
            "FROM export_collection x JOIN export e ON e.id = x.export_id "
            "WHERE x.collection_id = ? ORDER BY e.created_at DESC, e.id DESC",
            (collection_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # A catalog from before schema v8 has no export tables. Treated as "no
        # exports", which is the truthful answer for it.
        return []
    return [dict(row) for row in rows]


def _measures(catalog: Catalog, collection) -> dict:
    """Everything the criteria are scored against, gathered once."""
    stats = collections_module.stats(catalog, collection.id)
    where, params = collections_module.member_file_filter(catalog, collection.id)

    # Files whose size is unique in the catalog are never opened by design, so
    # they cannot be hashed. Counting them as failures would make F1b
    # unreachable for any collection holding one.
    hashable = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file f "
        "WHERE f.vanished_at IS NULL AND f.size_bytes IN ("
        "  SELECT size_bytes FROM file WHERE vanished_at IS NULL "
        "  GROUP BY size_bytes HAVING COUNT(*) > 1"
        f") AND {where}",
        params,
    ).fetchone()["n"]

    drives = catalog.conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(v.drive_id, -v.id)) AS n "
        "FROM file f JOIN volume v ON v.id = f.volume_id "
        f"WHERE f.vanished_at IS NULL AND {where}",
        params,
    ).fetchone()["n"]

    zones = catalog.conn.execute(
        "SELECT COUNT(DISTINCT d.disaster_zone) AS n "
        "FROM file f JOIN volume v ON v.id = f.volume_id "
        "JOIN drive d ON d.id = v.drive_id "
        f"WHERE f.vanished_at IS NULL AND d.disaster_zone IS NOT NULL AND {where}",
        params,
    ).fetchone()["n"]

    fixity = catalog.conn.execute(
        "SELECT COUNT(DISTINCT fc.file_id) AS checked, "
        "  SUM(CASE WHEN fc.result != 'ok' THEN 1 ELSE 0 END) AS incidents "
        "FROM fixity_check fc JOIN file f ON f.id = fc.file_id "
        f"WHERE {where}",
        params,
    ).fetchone()

    vocab = standards.vocabularies()
    licence_ids = {entry["id"] for entry in vocab["licenses"]}
    type_ids = {entry["id"] for entry in vocab["data_types"]}

    at_risk_extensions = {
        entry["ext"] for entry in vocab["format_risk"]["at_risk"]
    }
    if at_risk_extensions:
        placeholders = ",".join("?" * len(at_risk_extensions))
        at_risk = catalog.conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(f.size_bytes), 0) AS bytes "
            f"FROM file f WHERE f.vanished_at IS NULL "
            f"AND f.ext IN ({placeholders}) AND {where}",
            [*at_risk_extensions, *params],
        ).fetchone()
        at_risk_files, at_risk_bytes = int(at_risk["n"]), int(at_risk["bytes"])
    else:
        at_risk_files = at_risk_bytes = 0

    described = sum(1 for name, _ in collections_module.DESCRIPTIVE_FIELDS
                    if collection.row.get(name))
    reusable = sum(1 for name, _ in collections_module.REUSE_FIELDS
                   if collection.row.get(name))

    scans = catalog.conn.execute("SELECT COUNT(*) AS n FROM scan").fetchone()["n"]

    return {
        "files": stats["files"],
        "bytes": stats["bytes"],
        "identified": stats["identified"],
        "hashable": int(hashable),
        "content_identified_share": (
            stats["identified"] / hashable if hashable else 1.0
        ),
        "drives": int(drives),
        "zones": int(zones),
        "fixity_checked": int(fixity["checked"] or 0),
        "fixity_incidents": int(fixity["incidents"] or 0),
        "fixity_checked_share": (
            (fixity["checked"] or 0) / stats["files"] if stats["files"] else 0.0
        ),
        "metadata_completeness": described / len(collections_module.DESCRIPTIVE_FIELDS),
        "reuse_completeness": reusable / len(collections_module.REUSE_FIELDS),
        "license": collection.license,
        "license_controlled": collection.license in licence_ids,
        "data_type_controlled": collection.data_type in type_ids,
        "relations": len(collections_module.relations(catalog, collection.id)),
        # Exports describing this collection, newest first. The evidence F3 and
        # I1 are scored on since M8.
        "exports": _exports(catalog, collection.id),
        "at_risk_files": at_risk_files,
        "at_risk_bytes": at_risk_bytes,
        "at_risk_share": (at_risk_bytes / stats["bytes"]) if stats["bytes"] else 0.0,
        "scans": int(scans),
        "missing_descriptive": [
            purpose
            for name, purpose in collections_module.DESCRIPTIVE_FIELDS
            if not collection.row.get(name)
        ],
    }


def _evaluate(criterion: dict, collection, measures: dict) -> tuple[str, str]:
    """Status and a one-line detail for one criterion."""
    check = criterion["check"]
    threshold = criterion.get("threshold")

    if check == "local_pid":
        ok = bool(collection.local_pid)
        return (PASS if ok else FAIL), f"identifier {collection.local_pid or '—'}"

    if check == "license":
        if not collection.license:
            return FAIL, "no licence recorded"
        return PASS, f"licence {collection.license}"

    if check == "vocabulary_terms":
        uncontrolled = []
        if collection.license and not measures["license_controlled"]:
            uncontrolled.append(f"licence '{collection.license}'")
        if collection.data_type and not measures["data_type_controlled"]:
            uncontrolled.append(f"data type '{collection.data_type}'")
        if not collection.license and not collection.data_type:
            return FAIL, "neither licence nor data type is set"
        if uncontrolled:
            return FAIL, "not from the shipped vocabulary: " + ", ".join(uncontrolled)
        return PASS, "licence and data type use controlled terms"

    if check == "relations":
        count = measures["relations"]
        return (PASS if count else FAIL), f"{count} typed relation(s)"

    if check == "locatable":
        drives = measures["drives"]
        if not measures["files"]:
            return FAIL, "no live files are recorded for this collection"
        return (PASS if drives else FAIL), f"on {drives} drive(s)"

    if check == "tombstones_kept":
        # True by construction: the catalog marks vanished rows rather than
        # deleting them, which is what lets a description outlive the media.
        return PASS, "vanished entries are tombstoned, not deleted"

    if check == "indexed":
        return (
            (PASS if measures["files"] else FAIL),
            f"{measures['files']:,} files in the searchable catalog",
        )

    if check == "provenance":
        if measures["scans"] and measures["fixity_checked"]:
            return PASS, (
                f"{measures['scans']} scan(s) and "
                f"{measures['fixity_checked']:,} fixity event(s)"
            )
        if measures["scans"]:
            return FAIL, "scan history exists but no fixity events were recorded"
        return FAIL, "no scan history"

    if check == "format_risk":
        share = measures["at_risk_share"]
        if not measures["bytes"]:
            return NOT_APPLICABLE, "no content to assess"
        if share < 0.1:
            return PASS, f"{share:.1%} of bytes are in at-risk formats"
        return FAIL, (
            f"{share:.0%} of bytes ({measures['at_risk_files']:,} files) are in "
            "formats identified as at risk"
        )

    if check in ("manifest_linkage", "export_format"):
        # Assessable since M8. Before bundles existed these were excluded from
        # the denominator rather than failed, so a missing milestone did not
        # depress anyone's grade. Now the question has an answer, and it is a
        # fact about this catalog: has a bundle been written that describes
        # this collection?
        exported = measures.get("exports") or []
        if not exported:
            return FAIL, "no export bundle describes this collection yet"

        latest = exported[0]
        if check == "manifest_linkage":
            if latest.get("has_manifest"):
                return (
                    PASS,
                    f"described by the export of {latest['created_at'][:10]}, "
                    "which binds the identifier to checksums, paths and drives",
                )
            return FAIL, "the latest export contains no content inventory"

        if latest.get("has_ro_crate") and latest.get("has_datacite"):
            return (
                PASS,
                f"RO-Crate and DataCite written in the export of "
                f"{latest['created_at'][:10]}",
            )
        missing = []
        if not latest.get("has_ro_crate"):
            missing.append("RO-Crate")
        if not latest.get("has_datacite"):
            missing.append("DataCite")
        return FAIL, f"the latest export is missing {' and '.join(missing)}"

    if threshold is not None and check in measures:
        value = measures[check]
        ok = value >= threshold
        detail = f"{value:.0%} (threshold {threshold:.0%})"
        if check == "metadata_completeness" and not ok:
            missing = ", ".join(measures["missing_descriptive"])
            detail += f"; missing: {missing}"
        return (PASS if ok else FAIL), detail

    return NOT_ASSESSABLE, f"no check implemented for '{check}'"


def score(catalog: Catalog, collection_id: int) -> Scorecard:
    """Score one collection against the shipped FAIR rubric."""
    rubric = standards.fair_rubric()
    collection = collections_module.get(catalog, collection_id)
    measures = _measures(catalog, collection)

    results: list[CriterionResult] = []
    for criterion in rubric["criteria"]:
        status, detail = _evaluate(criterion, collection, measures)
        results.append(
            CriterionResult(
                id=criterion["id"],
                principle=criterion["principle"],
                title=criterion["title"],
                status=status,
                weight=float(criterion.get("weight", 1.0)),
                blocking=bool(criterion.get("blocking")),
                detail=detail,
                gap=criterion.get("gap", ""),
                remedy=criterion.get("remedy", ""),
            )
        )

    counted = [r for r in results if r.counts]
    total_weight = sum(r.weight for r in counted)
    earned = sum(r.weight for r in counted if r.status == PASS)
    fraction = (earned / total_weight) if total_weight else 0.0

    grade = "poor"
    for threshold, label in rubric["scoring"]["grades"]:
        if fraction >= float(threshold):
            grade = label
            break
    if any(r.blocking and r.status == FAIL for r in results):
        # However well the rest scores: unlicensed data is not reusable, and an
        # "adequate" badge on it would be the scorecard lying.
        grade = "incomplete"

    return Scorecard(
        collection_id=collection.id,
        local_pid=collection.local_pid,
        title=collection.title,
        score=fraction,
        grade=grade,
        results=tuple(results),
        rubric=rubric.citation,
        measures=measures,
    )


def score_all(catalog: Catalog) -> list[Scorecard]:
    return [
        score(catalog, collection.id)
        for collection in collections_module.all_collections(catalog)
    ]


def format_recommendations(catalog: Catalog, collection_id: int) -> list[dict]:
    """At-risk formats in a collection, with an open alternative for each.

    A recommendation only. Drive Fusion never converts anything, and the text
    says so wherever this is displayed.
    """
    vocab = standards.vocabularies()
    risk = {entry["ext"]: entry["why"] for entry in vocab["format_risk"]["at_risk"]}
    alternatives = vocab["format_risk"]["alternatives"]

    where, params = collections_module.member_file_filter(catalog, collection_id)
    if not risk:
        return []

    placeholders = ",".join("?" * len(risk))
    rows = catalog.conn.execute(
        f"SELECT f.ext, COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes "
        f"FROM file f WHERE f.vanished_at IS NULL "
        f"AND f.ext IN ({placeholders}) AND {where} "
        "GROUP BY f.ext ORDER BY bytes DESC",
        [*risk, *params],
    )
    return [
        {
            "extension": row["ext"],
            "files": int(row["files"]),
            "bytes": int(row["bytes"]),
            "why": risk.get(row["ext"], ""),
            "alternative": alternatives.get(row["ext"]),
        }
        for row in rows
    ]
