"""HTML and CSV reports (PLAN §11 screen 8, ROADMAP M8.5).

CSV for spreadsheets and scripts, one file per question:

* ``summary.csv``: the redundancy headline numbers and the expected loss;
* ``under-protected.csv``: each item on fewer drives than the target;
* ``duplicates.csv``: content at more than one path, with bytes duplicated on
  one drive (evidence only: Drive Fusion never removes anything);
* ``durability.csv``: each set of drives, what it holds and its P(loss);
* ``scorecards.csv``: every FAIR and NDSA criterion per collection;
* ``plan.csv``: the current plan's steps.

``report.html`` puts the same on one page: self-contained, no scripts, no
links out, readable offline years from now.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io

from drivefusion.core import disclaimer
from drivefusion.core.analysis import (duplicate_groups, group_paths, redundancy_summary,
                                       under_protected)

#: Rows given in the HTML per table; the CSVs have them all.
HTML_ROWS = 200


def _csv(header, rows) -> str:
    buffer = io.StringIO()
    out = csv.writer(buffer, lineterminator="\n")
    out.writerow(header)
    out.writerows(rows)
    return buffer.getvalue()


def _labels(catalog) -> dict[int, str]:
    return {r["id"]: r["nickname"] or r["model"] or r["serial"] or f"drive {r['id']}"
            for r in catalog.conn.execute("SELECT id, nickname, model, serial FROM drive")}


def tables(catalog, *, min_copies: int = 2, today: dt.date | None = None) -> dict:
    """Every report as (header, rows), keyed by file name."""
    from drivefusion.core.fair import rubrics, scorecard
    from drivefusion.core.health import durability
    from drivefusion.core.planner import document
    from drivefusion.core.store import curation, plans

    labels = _labels(catalog)
    summary = redundancy_summary(catalog, min_copies=min_copies)
    exposure = durability.exposure(catalog, today=today)
    summary_rows = [(k, v) for k, v in summary.items()] + [
        ("min_copies", min_copies),
        ("expected_bytes_lost_per_year", round(exposure.expected_bytes_per_year, 3)),
        ("risk_confidence", exposure.confidence),
        ("risk_model", exposure.model_version),
        ("rate_tables", "; ".join(exposure.rate_tables)),
    ]

    def paths(group):
        return "; ".join(group_paths(catalog, group, limit=20))

    out = {
        "summary.csv": (("metric", "value"), summary_rows),
        "under-protected.csv": (
            ("bytes", "proven_drives", "possible_drives", "paths", "where"),
            [(g.size_bytes, g.drives, g.drives_possible or g.drives, g.paths, paths(g))
             for g in under_protected(catalog, min_copies=min_copies)]),
        "duplicates.csv": (
            ("bytes", "paths", "drives", "duplicated_on_one_drive_bytes", "where"),
            [(g.size_bytes, g.paths, g.drives, g.reclaimable_bytes, paths(g))
             for g in duplicate_groups(catalog)]),
        "durability.csv": (
            ("drives", "items", "bytes", "p_loss_per_year", "expected_bytes_lost_per_year",
             "confidence", "assumptions"),
            [("; ".join(labels.get(d, f"drive {d}") for d in s.drive_ids) or "unverified",
              s.items, s.bytes, f"{s.p_loss:.6g}", round(s.expected_bytes_per_year, 3),
              s.confidence, "; ".join(s.assumptions)) for s in exposure.sets]),
    }
    score_rows = []
    for coll in curation.collections(catalog):
        for rubric_id in rubrics.RUBRICS:
            card = scorecard.score(catalog, coll["id"], rubric_id)
            score_rows += [(coll["title"], card.rubric, r.criterion, r.status, r.detail,
                            r.remedy) for r in card.results]
    out["scorecards.csv"] = (("collection", "rubric", "criterion", "result", "detail",
                              "remedy"), score_rows)
    current = plans.current(catalog)
    plan_rows = []
    if current is not None:
        doc = document.to_json(catalog, current["id"])
        plan_rows = [(s["seq"], s["kind"], s["src_drive"] or "", s["src_path"] or "",
                      s["dst_drive"] or "", s["dst_path"] or "", s["bytes"] or "",
                      "" if s["risk_delta"] is None else round(s["risk_delta"], 3),
                      s["rationale"]) for s in doc["steps"]]
    out["plan.csv"] = (("seq", "kind", "from_drive", "from_path", "to_drive", "to_path",
                        "bytes", "loss_change_per_year", "why"), plan_rows)
    return out


def _table(header, rows, limit=HTML_ROWS) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
                   for row in rows[:limit])
    more = (f"<p>{len(rows) - limit:,} more rows in the CSV.</p>" if len(rows) > limit else "")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"


TITLES = {
    "summary.csv": "Redundancy and risk",
    "under-protected.csv": "Under-protected items",
    "duplicates.csv": "Duplicates (evidence only)",
    "durability.csv": "Durability by drive set",
    "scorecards.csv": "Scorecards",
    "plan.csv": "Current plan",
}


def page(found: dict, *, generated: str) -> str:
    sections = "".join(
        f"<h2>{html.escape(TITLES[name])}</h2><p class=f>{html.escape(name)}: "
        f"{len(rows):,} rows</p>{_table(header, rows)}"
        for name, (header, rows) in found.items())
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<title>Drive Fusion report</title><style>"
        "body{font:14px system-ui,sans-serif;margin:2em;max-width:72em}"
        "table{border-collapse:collapse;margin:.5em 0 1.5em}"
        "td,th{border:1px solid #bbb;padding:.2em .5em;text-align:left;vertical-align:top}"
        "th{background:#eee}.f{color:#555;margin:0}"
        "</style></head><body>"
        f"<h1>Drive Fusion report</h1><p>Generated {html.escape(generated)}. Drive Fusion "
        "reads drives and writes only its catalog and this export; nothing here was "
        "changed on any drive.</p>"
        f"<p><em>{html.escape(disclaimer.TEXT)}</em></p>"
        f"{sections}</body></html>\n")


def write(bundle, catalog, *, min_copies: int = 2, today: dt.date | None = None) -> dict:
    found = tables(catalog, min_copies=min_copies, today=today)
    for name, (header, rows) in found.items():
        bundle.write(name, _csv(header, rows))
    bundle.write("report.html", page(found, generated=dt.datetime.now(dt.timezone.utc)
                                     .isoformat(timespec="seconds")))
    return {name: len(rows) for name, (_, rows) in found.items()}
