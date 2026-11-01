"""Test suites: named sets of requirements that can run on their own.

A suite picks the requirements to judge and, from them, the phases a run
needs: every run scans twice (baseline, rescan); ``verify`` runs only when a
suite judges fixity, and ``export`` only when one judges exports. The
read-only requirement (R-RO-01) is judged in every run whatever is chosen,
because it is the guarantee every phase must keep.

``all`` is every requirement, which is what ``python -m vv run`` judges.
"""

from __future__ import annotations

from dataclasses import dataclass

from vv.requirements import REQUIREMENTS


@dataclass(frozen=True)
class Suite:
    name: str
    title: str
    prefixes: tuple[str, ...]
    description: str

    def requirements(self) -> list[str]:
        return [r.id for r in REQUIREMENTS if r.id.startswith(self.prefixes)]


SUITES: dict[str, Suite] = {s.name: s for s in (
    Suite("safety", "Read-only and scope", ("R-RO-", "R-SC-"),
          "Nothing in a scanned tree changes; only what is in scope is catalogued."),
    Suite("catalog", "Catalog and rescans", ("R-CAT-", "R-INC-"),
          "Every file and folder is catalogued, and rescans see adds, edits, deletes, moves."),
    Suite("identity", "Content identity", ("R-ID-",),
          "Copies are recognised as one content; near-misses are not."),
    Suite("redundancy", "Copies and duplicates", ("R-RED-",),
          "Copies counted over distinct drives; under-protected and headline numbers true."),
    Suite("fixity", "Fixity", ("R-FIX-",),
          "Silent corruption is found, an edit is not mistaken for it."),
    Suite("health", "Drive health and risk", ("R-HL-",),
          "Recorded health, failure rates, P(loss) and expected loss."),
    Suite("curation", "Collections, versions, scorecards", ("R-COL-", "R-VER-", "R-FAIR-"),
          "Proposals, membership, coverage, version sets, FAIR and NDSA results."),
    Suite("planner", "Planner", ("R-PLAN-",),
          "Plans only add copies, fit their destinations and meet targets."),
    Suite("exports", "Exports", ("R-EXP-",),
          "Bundles, manifests, packages, mappings and reports are complete and true."),
)}

ALWAYS = ("R-RO-01",)


def resolve(names) -> list[str]:
    """Suite names (or ``all``) to suite names, refusing unknown ones."""
    names = list(names or ["all"])
    if "all" in names:
        return list(SUITES)
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise ValueError(f"unknown suite(s) {', '.join(unknown)}; choose from "
                         f"{', '.join(SUITES)} or all")
    return [n for n in SUITES if n in names]


def requirements(names) -> set[str]:
    out = set(ALWAYS)
    for name in resolve(names):
        out.update(SUITES[name].requirements())
    return out


def phases(names) -> set[str]:
    """The tool phases a run of these suites needs."""
    chosen = set(resolve(names))
    out = {"baseline", "rescan"}
    if "fixity" in chosen or "exports" in chosen:
        out.add("verify")       # exports follow verify, and fixity is judged there
    if "exports" in chosen:
        out.add("export")
    return out


def select(results: list[dict], names) -> list[dict]:
    """The results that judge a chosen requirement, plus every phase error."""
    if names is None or "all" in list(names):
        return results
    wanted = requirements(names)
    return [r for r in results if not r["requirements"] or set(r["requirements"]) & wanted]


def suite_of(requirement: str) -> str | None:
    return next((s.name for s in SUITES.values() if requirement.startswith(s.prefixes)), None)
