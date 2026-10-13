"""Reports: CSV for a spreadsheet, HTML for a browser.

CSV because the realistic consumer is a person with Excel and a question, and
HTML because the realistic consumer is the same person three years later with
no Drive Fusion installed. Both are plain files in the bundle: no server, no
JavaScript, no fonts to fetch. An export that needs a network to read is not an
archival format.

The HTML is deliberately one self-contained file with inline styles. A bundle
whose report depends on a CDN stops rendering the day the CDN moves, which is
exactly the failure archival formats exist to avoid.
"""

from __future__ import annotations

import csv
import html
import io

from drivefusion.core.analysis.duplicates import drive_key
from drivefusion.core.store.catalog import Catalog


def to_csv(header: list[str], rows) -> str:
    """CSV with CRLF line endings, which is what Excel expects."""
    buffer = io.StringIO()
    out = csv.writer(buffer, lineterminator="\r\n")
    out.writerow(header)
    for row in rows:
        out.writerow(["" if value is None else value for value in row])
    return buffer.getvalue()



def _utc(mtime_ns) -> str:
    """A nanosecond timestamp as an ISO date, or empty when unrecorded."""
    if not mtime_ns:
        return ""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(
            int(mtime_ns) / 1_000_000_000, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def content_inventory(catalog: Catalog, *, limit: int = 500_000):
    """One row per catalogued file: the index that answers "which drive is this on".

    §10.3. Capped, because a CSV with 50 million rows opens in nothing — the
    cap is stated in the bundle's README rather than applied silently, and the
    catalog database ships alongside for anyone who needs all of it.
    """
    header = [
        "path",
        "name",
        "extension",
        "size_bytes",
        "modified_utc",
        "drive",
        "volume",
        "filesystem",
        "checksum_algorithm",
        "checksum",
        "physical_copies",
    ]

    def rows():
        query = catalog.conn.execute(
            "SELECT f.id, f.name, f.ext, f.size_bytes, f.mtime_ns, f.dir_id, "
            "       c.full_hash, c.quick_hash, "
            "       COALESCE(d.nickname, d.model, v.label, v.volume_guid) AS drive, "
            "       v.volume_guid, v.fs_type, "
            # drive_key("v2"), not DRIVE_KEY: the constant names the outer
            # alias, and an aggregate over only outer columns is hoisted into
            # an aggregate of the outer query, collapsing it to a single row.
            "       (SELECT COUNT(DISTINCT " + drive_key("v2") + ") "
            "        FROM file f2 JOIN volume v2 ON v2.id = f2.volume_id "
            "        WHERE f2.content_id = f.content_id "
            "          AND f2.vanished_at IS NULL) AS copies "
            "FROM file f "
            "JOIN volume v ON v.id = f.volume_id "
            "LEFT JOIN drive d ON d.id = v.drive_id "
            "LEFT JOIN content c ON c.id = f.content_id "
            "WHERE f.vanished_at IS NULL "
            "ORDER BY f.id LIMIT ?",
            (limit,),
        )
        for row in query:
            entry = dict(row)
            full = entry.get("full_hash")
            quick = entry.get("quick_hash")
            digest = full or quick
            yield [
                catalog.dir_path(int(entry["dir_id"])) if entry["dir_id"] else "",
                entry["name"],
                entry["ext"],
                entry["size_bytes"],
                _utc(entry["mtime_ns"]),
                entry["drive"],
                entry["volume_guid"],
                entry["fs_type"],
                # Which tier produced it matters: a quick hash is a sample, and
                # presenting it in the same column as a full hash without
                # saying so would make it look like whole-file verification.
                ("blake2b-full" if full else "blake2b-quick" if quick else ""),
                digest.hex() if digest else "",
                entry.get("copies") or 1,
            ]

    return header, rows()


def collection_summary(catalog: Catalog):
    from drivefusion.core.curate import collections as collections_module

    header = [
        "local_pid",
        "title",
        "licence",
        "data_type",
        "project",
        "ndsa_target",
        "files",
        "bytes",
        "files_with_checksums",
    ]

    def rows():
        for collection in collections_module.all_collections(catalog):
            stats = collections_module.stats(catalog, collection.id)
            yield [
                collection.local_pid,
                collection.title,
                collection.license or "",
                collection.data_type or "",
                collection.project or "",
                collection.ndsa_target or "",
                stats.get("files", 0),
                stats.get("bytes", 0),
                stats.get("identified", 0),
            ]

    return header, rows()


def drive_summary(catalog: Catalog):
    """Drives ranked by what their failure would cost.

    Not by failure probability: the drive most likely to die is not the drive
    whose death is most expensive. ``sole_copy_bytes`` — content that exists
    nowhere else — is the column that matters, and it is why this ordering is
    a work queue rather than a list.
    """
    from drivefusion.core.analysis.exposure import riskiest_drives

    header = [
        "drive",
        "annual_failure_probability",
        "confidence",
        "bytes_held_nowhere_else",
        "expected_bytes_lost_per_year",
        "why",
    ]

    def rows():
        for entry in riskiest_drives(catalog, limit=1000):
            yield [
                entry.get("name"),
                f"{entry.get('annual_probability', 0):.6f}",
                entry.get("confidence") or "unknown",
                entry.get("sole_copy_bytes") or 0,
                f"{entry.get('expected_bytes_lost_per_year', 0):.0f}",
                "; ".join(entry.get("reasons") or []),
            ]

    return header, rows()


def _table(header: list[str], rows: list[list]) -> str:
    cells = "".join(f"<th>{html.escape(str(name))}</th>" for name in header)
    body = []
    for row in rows:
        body.append(
            "<tr>"
            + "".join(f"<td>{html.escape(str(value))}</td>" for value in row)
            + "</tr>"
        )
    return (
        f"<table><thead><tr>{cells}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 system-ui, -apple-system, Segoe UI, sans-serif;
       margin: 0 auto; max-width: 70rem; padding: 2rem 1rem; }
h1, h2 { line-height: 1.2; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid currentColor; padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; font-size: .92em; margin: 1rem 0; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #8884; }
th { font-weight: 600; }
td:nth-child(n+4) { font-variant-numeric: tabular-nums; }
.note { border-left: 3px solid #8888; padding: .4rem 0 .4rem 1rem; margin: 1rem 0; }
.warn { border-left-color: #c33; }
footer { margin-top: 3rem; font-size: .88em; opacity: .8; }
code { font-size: .92em; }
"""


def html_report(
    catalog: Catalog,
    *,
    version: str,
    generated_at: str,
    stats: dict,
    scorecards: list[dict],
    plan=None,
    limit: int = 200,
) -> str:
    """One self-contained page summarising the archive.

    Tables are capped: this is the overview, and the CSVs beside it hold
    everything. A browser asked to lay out half a million rows stops
    responding, which makes the bundle look broken rather than large.
    """
    from drivefusion.core.analysis import redundancy_summary

    redundancy = redundancy_summary(catalog)
    sections: list[str] = []

    sections.append(
        "<h1>Drive Fusion archive report</h1>"
        f"<p>Generated {html.escape(generated_at)} by Drive Fusion "
        f"{html.escape(version)}.</p>"
        '<div class="note"><strong>Read-only.</strong> Nothing in the archive '
        "described here was modified, moved, renamed or deleted to produce "
        "this report. Drive Fusion has no code that can do any of those "
        "things, and it has no executor: any plan below is a checklist for a "
        "person to follow.</div>"
        '<div class="note">This is not a compliance artifact and not a Data '
        "Management and Sharing Plan. It records what was found and what this "
        "tool's own versioned rubrics make of it.</div>"
    )

    sections.append(
        "<h2>The archive</h2>"
        + _table(
            ["", "count"],
            [
                ["files catalogued", f"{stats.get('files', 0):,}"],
                ["bytes catalogued", f"{stats.get('bytes', 0):,}"],
                ["volumes", f"{stats.get('volumes', 0):,}"],
                ["drives", f"{stats.get('drives', 0):,}"],
                ["confirmed collections", f"{stats.get('collections', 0):,}"],
            ],
        )
    )

    if redundancy:
        sections.append(
            "<h2>Redundancy</h2>"
            + _table(
                ["", "value"],
                [[key.replace("_", " "), f"{value:,}" if isinstance(value, int) else value]
                 for key, value in redundancy.items()],
            )
            + '<div class="note">Copies are counted over distinct physical '
            "drives. Two copies on one disk are one copy against that disk "
            "failing, and hardlinks to the same data are one copy however "
            "many names they have.</div>"
        )

    header, rows = drive_summary(catalog)
    drive_rows = list(rows)[:limit]
    if drive_rows:
        sections.append(
            "<h2>Drives, by what their failure would cost</h2>"
            + _table(header, drive_rows)
            + '<div class="note">A probability labelled <code>estimated</code> '
            "comes from age and media class alone, which is common over USB "
            "bridges that pass no SMART through. A drive that reported nothing "
            "scores worse than one verified clean, because silence and a clean "
            "bill of health are not the same state.</div>"
        )

    header, rows = collection_summary(catalog)
    collection_rows = list(rows)[:limit]
    if collection_rows:
        sections.append(
            "<h2>Collections</h2>" + _table(header, collection_rows)
        )

    if scorecards:
        card_rows = [
            [
                card.get("title"),
                card.get("summary"),
                card.get("ndsa") or "",
                "yes" if card.get("blocking") else "no",
            ]
            for card in scorecards
        ]
        sections.append(
            "<h2>FAIR and preservation scorecards</h2>"
            + _table(["collection", "FAIR", "NDSA", "blocking gap"], card_rows)
            + '<div class="note">Scored against this tool\'s operationalisation '
            "of the published principles, at the rubric versions named in the "
            "bundle metadata — not against the published text itself. "
            "Criteria this tool cannot assess are excluded from the "
            "denominator rather than counted as failures.</div>"
        )

    if plan is not None and plan.steps:
        step_rows = [
            [step.seq, step.kind, step.rationale] for step in plan.steps[:limit]
        ]
        sections.append(
            "<h2>Consolidation plan</h2>"
            + f"<p>Policy: {html.escape(plan.policy.describe())}</p>"
            + _table(["#", "step", "why"], step_rows)
            + '<div class="note"><strong>A checklist, not an operation.</strong> '
            "Every step is something you do, with your own tools. No step "
            "removes anything: when no drive can take another copy, the plan "
            "asks for more storage.</div>"
        )
        if plan.reclamation_appendix:
            appendix = [
                [
                    f"{entry['reclaimable_bytes']:,}",
                    entry["physical_copies"],
                    "; ".join(entry["locations"][:2]),
                ]
                for entry in plan.reclamation_appendix[:limit]
            ]
            sections.append(
                "<h2>Appendix: space duplication is using</h2>"
                + '<div class="note warn">Evidence only. Drive Fusion does not '
                "delete anything and no step above acts on this list. It is "
                "here so you can see where the space went.</div>"
                + _table(["reclaimable bytes", "copies on one drive", "where"], appendix)
            )

    body = "".join(sections)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Drive Fusion archive report</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n{body}\n"
        "<footer>Produced by Drive Fusion, which reads and never writes. "
        "This file is self-contained: no network access is needed to read it, "
        "now or in ten years.</footer>\n</body>\n</html>\n"
    )
