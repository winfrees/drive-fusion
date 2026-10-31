"""Making exports (ROADMAP M8.6): each kind into its own bundle, the root remembered.

The export root is always chosen by a person (D22). Once chosen it is
remembered per catalog as the root of the last export made, so the next
export needs no choosing; a different ``root`` simply becomes the new one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from drivefusion.core.export import writer

KINDS = ("manifests", "collections", "mappings", "reports")


def last_root(catalog) -> str | None:
    row = catalog.conn.execute("SELECT root FROM export ORDER BY id DESC LIMIT 1").fetchone()
    return row["root"] if row else None


def _standards(catalog) -> dict:
    from drivefusion.core.fair import rubrics
    from drivefusion.core.health import afr

    return {"rubrics": {r: {"version": rubrics.current(r).name,
                            "retrieval": rubrics.current(r).retrieval} for r in rubrics.RUBRICS},
            "failure_rates": [t.name for t in afr.tables(catalog)],
            "risk_model": afr.model().version}


def make(catalog, kinds, root: str | Path | None = None, *, template: str | None = None,
         min_copies: int = 2, now: dt.datetime | None = None) -> list[dict]:
    """One bundle per kind; returns ``[{kind, id, path, summary}]``."""
    from drivefusion.core.export import manifests, mappings, packages, reports

    root = root if root is not None else last_root(catalog)
    if root is None:
        raise writer.ExportError("choose an export folder first (--to FOLDER); Drive Fusion "
                                 "never picks one for you")
    kinds = list(KINDS) if "all" in kinds else list(kinds)
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise writer.ExportError(f"unknown export kind(s) {sorted(unknown)}; use {KINDS}")
    bodies = {
        "manifests": lambda b: manifests.write(b, catalog),
        "collections": lambda b: packages.write(b, catalog),
        "mappings": lambda b: mappings.write(b, catalog, **(
            {"template": template} if template else {})),
        "reports": lambda b: reports.write(b, catalog, min_copies=min_copies),
    }
    standards = _standards(catalog)
    made = []
    for kind in kinds:
        bundle = writer.open_bundle(catalog, root, kind, now=now)
        summary = bodies[kind](bundle)
        export_id = bundle.close(standards=standards, summary=summary)
        made.append({"kind": kind, "id": export_id, "path": str(bundle.path),
                     "summary": summary})
    return made
