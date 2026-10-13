"""Assembling a bundle: what goes in it, and in what order.

One entry point, :func:`build`. It lays out a BagIt bag whose payload is the
description of an archive — metadata, manifests, scorecards, reports, and
optionally a plan — and finalises it with checksums over exactly what it wrote.

The layout is chosen so that someone opening the folder in three years, with no
tooling, finds the human-readable things first:

    export-20260413T101500Z/
      README.txt                  the disclaimer, and what everything here is
      bagit.txt                   BagIt declaration
      bag-info.txt                what is described, and that the files are elsewhere
      manifest-sha256.txt         checksums over data/
      tagmanifest-sha256.txt      checksums over the tag files
      data/
        report.html               the whole archive, one self-contained page
        ro-crate-metadata.json    machine-readable description
        premis-events.json        every fixity check, as provenance
        inventory/*.csv           the index, the collections, the drives
        collections/<pid>/        per collection: DataCite, scorecards
        drives/<name>/            per drive: the two manifests to copy across
        plan/                     the checklist, as JSON and Markdown

Order matters in exactly one place: the manifest is written last, because it is
a statement about files that already exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from drivefusion import __version__
from drivefusion.core.export import bagit, manifests, metadata, naming, reports, writer
from drivefusion.core.store.catalog import Catalog


@dataclass
class Bundle:
    """What was written, for the caller to report and the tests to assert on."""

    root: Path
    files: list[str] = field(default_factory=list)
    bag: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    export_id: int | None = None

    def add(self, path: Path) -> None:
        self.files.append(Path(path).relative_to(self.root).as_posix())


def _stats(catalog: Catalog) -> dict:
    from drivefusion.core.curate import collections as collections_module

    counts = catalog.counts()
    drives = manifests.drives(catalog)
    return {
        "files": int(counts.get("files", 0)),
        "bytes": int(counts.get("bytes", 0) or 0),
        "volumes": len({entry["volume_id"] for entry in drives}),
        "drives": len({entry["drive_key"] for entry in drives}),
        "collections": len(collections_module.all_collections(catalog)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # An opaque, stable identifier rather than the catalog's path. The path
        # is usually C:\Users\<someone>\..., and a bundle is a thing people
        # share: putting a username in it would make the export carry personal
        # information the tool has no business distributing. The digest still
        # identifies successive exports of the same catalog as related.
        "catalog_id": "catalog-"
        + hashlib.blake2b(
            str(catalog.path.resolve()).encode("utf-8"), digest_size=8
        ).hexdigest(),
    }


def _enriched_collections(catalog: Catalog) -> list:
    """Collections with their measures and relations attached.

    The metadata writers need both, and loading them there would mean each
    format querying the catalog again and the formats disagreeing if one of
    them drifted.
    """
    from drivefusion.core.curate import collections as collections_module

    out = []
    for collection in collections_module.all_collections(catalog):
        stats = collections_module.stats(catalog, collection.id)
        where, params = collections_module.member_file_filter(catalog, collection.id)
        drives = catalog.conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(v.drive_id, -v.id)) AS drives "
            "FROM file f JOIN volume v ON v.id = f.volume_id "
            f"WHERE f.vanished_at IS NULL AND {where}",
            params,
        ).fetchone()

        relations = [
            dict(row)
            for row in catalog.conn.execute(
                "SELECT id, to_ref, relation_type, added_at "
                "FROM collection_relation WHERE from_collection = ? ORDER BY id",
                (collection.id,),
            )
        ]

        collection.row["_measures"] = {
            "files": stats.get("files", 0),
            "bytes": stats.get("bytes", 0),
            "hashed": stats.get("identified", 0),
            "drives": int(dict(drives).get("drives", 0)) if drives else 0,
            "copies": int(dict(drives).get("drives", 0)) if drives else 0,
        }
        collection.row["_relations"] = relations
        out.append(collection)
    return out


def _scorecards(catalog: Catalog, collections: list) -> list[dict]:
    from drivefusion.core.fair import ndsa, scorecard

    cards = []
    for collection in collections:
        card = scorecard.score(catalog, collection.id)
        levels = ndsa.score(catalog, collection.id)
        cards.append(
            {
                "local_pid": collection.local_pid,
                "title": collection.title,
                "summary": card.summary(),
                "rubric": card.rubric,
                "ndsa": levels.summary(),
                "blocking": bool(
                    [result for result in card.failures if result.blocking]
                ),
                "failures": [
                    {
                        "id": result.id,
                        "title": result.title,
                        "blocking": result.blocking,
                        "gap": result.gap,
                        "remedy": result.remedy,
                    }
                    for result in card.failures
                ],
                "not_assessed": [
                    {"id": result.id, "detail": result.detail}
                    for result in card.not_assessed
                ],
            }
        )
    return cards


def build(
    catalog: Catalog,
    destination: str | Path,
    *,
    plan=None,
    rename_template: str | None = None,
    inventory_limit: int = 500_000,
) -> Bundle:
    """Write a complete bundle under *destination* and return what it contains.

    *destination* is the directory the user chose; the bundle itself goes into
    a new timestamped folder inside it, so an earlier export is never touched.
    """
    root = writer.prepare_root(destination, catalog=catalog)
    bundle = Bundle(root=root)
    stats = _stats(catalog)
    bundle.stats = stats

    collections = _enriched_collections(catalog)
    scorecards = _scorecards(catalog, collections)

    def put(relative: str, content) -> None:
        bundle.add(writer.write(root, relative, content))

    # The README first, so the folder explains itself before anything else in
    # it needs explaining.
    put("README.txt", _readme(stats, plan is not None))

    payload = bagit.payload_path
    put(
        payload("report.html"),
        reports.html_report(
            catalog,
            version=__version__,
            generated_at=stats["generated_at"],
            stats=stats,
            scorecards=scorecards,
            plan=plan,
        ),
    )
    put(
        payload("ro-crate-metadata.json"),
        metadata.ro_crate(
            catalog,
            version=__version__,
            collections=collections,
            stats=stats,
            generated_at=stats["generated_at"],
        ),
    )
    put(payload("premis-events.json"), metadata.premis_events(catalog, version=__version__))

    header, rows = reports.content_inventory(catalog, limit=inventory_limit)
    put(payload("inventory/files.csv"), reports.to_csv(header, rows))
    header, rows = reports.collection_summary(catalog)
    put(payload("inventory/collections.csv"), reports.to_csv(header, rows))
    header, rows = reports.drive_summary(catalog)
    put(payload("inventory/drives.csv"), reports.to_csv(header, rows))

    for collection, card in zip(collections, scorecards):
        folder = f"collections/{naming.token(collection.local_pid)}"
        put(payload(f"{folder}/datacite.json"), metadata.datacite(collection))
        put(payload(f"{folder}/scorecard.json"), json.dumps(card, indent=2))

        if rename_template:
            try:
                mapping = naming.build(
                    catalog,
                    collection_id=collection.id,
                    template=rename_template,
                )
                mapping.assert_reversible()
            except naming.NamingError as error:
                # Refused, and said so in the bundle: a rename plan that cannot
                # be undone must not ship, and silently omitting it would leave
                # the user wondering which collections it covered.
                bundle.warnings.append(f"{collection.local_pid}: {error}")
                put(
                    payload(f"{folder}/rename-REFUSED.txt"),
                    f"No rename plan was written for {collection.title}.\n\n"
                    f"{error}\n\n"
                    "Drive Fusion writes a rename plan only when it can also "
                    "write the table that undoes it. Once files are renamed, "
                    "the old names exist nowhere else.\n",
                )
            else:
                put(
                    payload(f"{folder}/rename-plan.csv"),
                    reports.to_csv(
                        ["file_id", "current_path", "proposed_relative_path", "why"],
                        mapping.forward_rows(),
                    ),
                )
                put(
                    payload(f"{folder}/rename-undo.csv"),
                    reports.to_csv(
                        ["file_id", "renamed_relative_path", "restore_to", "why"],
                        mapping.inverse_rows(),
                    ),
                )

    drive_rows = manifests.drives(catalog)
    for drive in drive_rows:
        folder = f"drives/{drive['dir_name']}"
        put(
            payload(f"{folder}/{manifests.MACHINE_MANIFEST}"),
            manifests.machine_manifest(catalog, drive, version=__version__),
        )
        put(
            payload(f"{folder}/{manifests.HUMAN_MANIFEST}"),
            manifests.human_manifest(catalog, drive, version=__version__),
        )
    if drive_rows:
        put(payload("drives/HOW-TO-USE.txt"), _drive_instructions(len(drive_rows)))

    if plan is not None:
        put(payload("plan/plan.json"), plan.to_json())
        put(payload("plan/checklist.md"), _plan_markdown(plan))

    bundle.bag = bagit.finalise(
        root, version=__version__, described=stats, source_organization=None
    )
    for name in ("bagit.txt", "bag-info.txt", bagit.MANIFEST, bagit.TAG_MANIFEST):
        bundle.files.append(name)

    # Recorded last, and only once the bag is finalised: an export row is the
    # evidence FAIR F3 and I1 are scored on, and claiming one for a bundle
    # whose manifest failed to write would make the scorecard flattering.
    bundle.export_id = record(catalog, bundle, collections=collections, plan=plan)
    return bundle


def record(catalog: Catalog, bundle: Bundle, *, collections: list, plan=None) -> int:
    """Note in the catalog that this export exists.

    Two FAIR criteria ask whether a machine-readable description exists at all,
    and before M8 the honest answer was "not assessable". The answer is now a
    fact about this catalog, so it is stored as one.
    """
    plan_id = None
    if plan is not None:
        plan_id = getattr(plan, "plan_id", None)

    cursor = catalog.conn.execute(
        "INSERT INTO export (created_at, root_path, tool_version, plan_id, "
        "files_written, payload_oxum, manifest_algo, bundle_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle.stats.get("generated_at"),
            str(bundle.root),
            __version__,
            plan_id,
            len(bundle.files),
            bundle.bag.get("payload_oxum"),
            bundle.bag.get("algorithm"),
            json.dumps(
                {
                    "files": sorted(bundle.files),
                    "warnings": bundle.warnings,
                    "stats": bundle.stats,
                }
            ),
        ),
    )
    export_id = int(cursor.lastrowid)

    for collection in collections:
        folder = f"data/collections/{naming.token(collection.local_pid)}"
        written = set(bundle.files)
        catalog.conn.execute(
            "INSERT INTO export_collection (export_id, collection_id, "
            "has_manifest, has_ro_crate, has_datacite) VALUES (?, ?, ?, ?, ?)",
            (
                export_id,
                collection.id,
                int("data/inventory/files.csv" in written),
                int("data/ro-crate-metadata.json" in written),
                int(f"{folder}/datacite.json" in written),
            ),
        )

    if plan_id is not None:
        # §3.3: a plan's terminal state is "exported", never "applied".
        catalog.conn.execute(
            "UPDATE plan SET status = 'exported' WHERE id = ? AND status = 'draft'",
            (plan_id,),
        )

    catalog.conn.commit()
    return export_id


def exports_covering(catalog: Catalog, collection_id: int) -> list[dict]:
    """Exports that described this collection, most recent first."""
    return [
        dict(row)
        for row in catalog.conn.execute(
            "SELECT e.id, e.created_at, e.root_path, x.has_manifest, "
            "       x.has_ro_crate, x.has_datacite "
            "FROM export_collection x JOIN export e ON e.id = x.export_id "
            "WHERE x.collection_id = ? ORDER BY e.created_at DESC, e.id DESC",
            (collection_id,),
        )
    ]


def _readme(stats: dict, has_plan: bool) -> str:
    lines = [
        "Drive Fusion export",
        "===================",
        "",
        writer.DISCLAIMER,
        "",
        "What is in this folder",
        "----------------------",
        "",
        "  README.txt              this file",
        "  data/report.html        the whole archive on one page — start here",
        "  data/inventory/*.csv    the index: every file, collection and drive",
        "  data/collections/       per collection: DataCite metadata, scorecards",
        "  data/drives/            per drive: a manifest to copy onto that drive",
        "  data/ro-crate-metadata.json   machine-readable description (RO-Crate)",
        "  data/premis-events.json       every fixity check, as provenance",
    ]
    if has_plan:
        lines.append("  data/plan/              the consolidation checklist")
    lines += [
        "  bagit.txt, bag-info.txt, manifest-sha256.txt",
        "                          BagIt packaging, so this folder can be",
        "                          verified intact years from now",
        "",
        "What this folder is not",
        "-----------------------",
        "",
        "It is not a copy of your data. The files described here are on your",
        "drives; this folder describes them. The checksums in the inventory are",
        "the archive's; the checksums in manifest-sha256.txt are this folder's.",
        "",
        "The archive, as catalogued",
        "--------------------------",
        "",
        f"  files            {stats.get('files', 0):,}",
        f"  bytes            {stats.get('bytes', 0):,}",
        f"  volumes          {stats.get('volumes', 0):,}",
        f"  drives           {stats.get('drives', 0):,}",
        f"  collections      {stats.get('collections', 0):,}",
        f"  generated        {stats.get('generated_at', '')}",
        "",
    ]
    return "\n".join(lines)


def _drive_instructions(count: int) -> str:
    return (
        "Per-drive manifests\n"
        "===================\n"
        "\n"
        f"There is a folder here for each of the {count} drive(s) in the "
        "catalog, holding two files:\n"
        "\n"
        f"  {manifests.MACHINE_MANIFEST}   for a program\n"
        f"  {manifests.HUMAN_MANIFEST}      for a person with a text editor\n"
        "\n"
        "Copy both onto the matching drive, at its top level. A drive carrying "
        "them can answer, on its own, what is on it, how many other copies "
        "exist and where they are — with no catalog and no software.\n"
        "\n"
        "Drive Fusion does not put them there for you. Writing to your drives "
        "is the one thing it does not do, and it is not going to make an "
        "exception for its own files.\n"
    )


def _plan_markdown(plan) -> str:
    lines = [
        f"# {plan.name}",
        "",
        f"Policy: {plan.policy.describe()}",
        "",
        "A checklist you carry out yourself, with your own tools. Drive Fusion",
        "has no executor. No step removes anything: where no drive can take",
        "another copy, the plan asks for more storage instead.",
        "",
        "## Steps",
        "",
    ]
    for step in plan.steps:
        depends = (
            f" _(after step {', '.join(str(d) for d in step.depends_on)})_"
            if step.depends_on
            else ""
        )
        lines.append(f"{step.seq}. **{step.kind}**{depends}")
        lines.append(f"   {step.rationale}")
        lines.append("")

    simulation = plan.simulation or {}
    if simulation:
        lines += [
            "## If you follow this plan",
            "",
            f"- Data to copy: {simulation.get('bytes_to_copy', 0):,} bytes",
            f"- Estimated time: {simulation.get('estimated_hours', 0):.1f} hours",
            f"  ({simulation.get('throughput_assumption', '')})",
            f"- Expected loss: {simulation.get('expected_loss_before', 0):,.0f}"
            f" → {simulation.get('expected_loss_after', 0):,.0f} bytes/year",
            "",
        ]

    if plan.reclamation_appendix:
        lines += [
            "## Appendix: space duplication is using",
            "",
            "Evidence only. Drive Fusion does not delete anything, and no step",
            "above acts on this list.",
            "",
        ]
        for entry in plan.reclamation_appendix:
            lines.append(
                f"- {entry['reclaimable_bytes']:,} bytes across "
                f"{entry['physical_copies']} copies on one drive"
            )
        lines.append("")
    return "\n".join(lines)
