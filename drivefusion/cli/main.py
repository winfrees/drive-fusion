"""Command-line entry point.

Every verb is read-only or produces a document. There is deliberately no
``apply``, ``move``, ``dedupe``, or ``delete`` command, and there never will
be: consolidation plans are documents you execute with your own tools
(docs/PLAN.md §3.3).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from drivefusion import __version__
from drivefusion.core import discovery
from drivefusion.core.analysis import (
    content_paths,
    duplicate_groups,
    integrity_incidents,
    reclamation_candidates,
    redundancy_summary,
    under_protected,
)
from drivefusion.core.enum.backend import describe_age, method_summary, plan_enumeration
from drivefusion.core.identity import HASH_ALGO, hash_pass, verify_pass
from drivefusion.core.scan import preview_root, scan_root
from drivefusion.core.scope import ExclusionSet, ScopeError, validate_new_root
from drivefusion.core.store.catalog import Catalog

BANNER = "Drive Fusion — read-only cataloging and consolidation planning"

DEFAULT_CATALOG = Path(
    os.environ.get("DRIVEFUSION_CATALOG", Path.home() / ".drivefusion" / "catalog.db")
)

#: Verbs not yet implemented, and the milestone that lands each.
PLANNED_VERBS = {
    "plan": ("build a consolidation plan", "M7"),
    "export": ("write reports, manifests, and bundles to an export folder", "M8"),
}


#: Name of the windowed executable built by packaging/drivefusion.spec.
WINDOWED_EXECUTABLE_STEM = "drivefusion"


def wants_window(argv0: str, *, frozen: bool, has_command: bool) -> bool:
    """True when this invocation should open the GUI instead of printing help.

    The frozen build ships two front ends from one entry point: a windowed
    ``DriveFusion.exe`` and a console ``drivefusion.exe``. Double-clicking the
    windowed one passes no arguments, and printing help at a process with no
    console writes to nowhere at all — the user sees the program start and vanish.
    So that one case opens the window.

    Distinguished by the executable's name, capitalised in the windowed build,
    and only ever when frozen: from a source checkout ``python -m drivefusion``
    should keep printing help.

    The basename is taken separator-agnostically rather than with ``pathlib``,
    which on POSIX does not split a Windows path at all and would hand back the
    whole string as the stem. The answer must not depend on which platform is
    reading the argument.
    """
    if has_command or not frozen:
        return False
    basename = argv0.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename[:-4] if basename.lower().endswith(".exe") else basename
    # Case-sensitive on purpose: "DriveFusion" is the window, "drivefusion" the
    # console tool, and on Windows only the spelling tells them apart.
    return stem != WINDOWED_EXECUTABLE_STEM and stem.lower() == WINDOWED_EXECUTABLE_STEM


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024
    return f"{size:,.1f} PB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drivefusion", description=BANNER)
    parser.add_argument(
        "--version", action="version", version=f"drivefusion {__version__}"
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"catalog database path (default: {DEFAULT_CATALOG})",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    scope = subparsers.add_parser(
        "scope", help="manage which drives and roots are catalogued"
    )
    scope_sub = scope.add_subparsers(dest="scope_command", metavar="ACTION")

    scope_add = scope_sub.add_parser("add", help="add a root to the scan scope")
    scope_add.add_argument("path")
    scope_add.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="extra exclusion glob; repeatable",
    )
    scope_sub.add_parser("list", help="list scope roots")
    scope_remove = scope_sub.add_parser("remove", help="remove a scope root")
    scope_remove.add_argument("root_id", type=int)
    scope_preview = scope_sub.add_parser(
        "preview", help="count what a scan would cover, writing nothing"
    )
    scope_preview.add_argument("root_id", type=int)

    scan = subparsers.add_parser("scan", help="enumerate scope roots into the catalog")
    scan.add_argument(
        "root_id", type=int, nargs="?", help="scope root to scan (default: all)"
    )

    find = subparsers.add_parser("find", help="search the catalog by filename")
    find.add_argument("pattern", help="LIKE pattern; %% matches any sequence")
    find.add_argument("--limit", type=int, default=50)
    find.add_argument(
        "--include-vanished", action="store_true",
        help="include files last seen in an earlier scan",
    )

    hashing = subparsers.add_parser(
        "hash", help="establish content identity for catalogued files"
    )
    hashing.add_argument(
        "--volume", type=int, default=None, metavar="ID",
        help="restrict to one volume",
    )
    hashing.add_argument(
        "--limit", type=int, default=None, help="stop after this many candidates"
    )

    report = subparsers.add_parser(
        "report", help="duplicate, redundancy, and integrity reports"
    )
    report.add_argument(
        "kind", nargs="?", default="summary",
        choices=("summary", "duplicates", "redundancy", "integrity", "risk"),
    )
    report.add_argument("--top", type=int, default=20, help="rows to show")
    report.add_argument(
        "--min-copies", type=int, default=2, metavar="N",
        help="copies on distinct drives a file should have (default: 2)",
    )

    verify = subparsers.add_parser(
        "verify", help="re-hash catalogued content and report fixity failures"
    )
    verify.add_argument("--volume", type=int, default=None, metavar="ID")
    verify.add_argument("--limit", type=int, default=None)

    collections_parser = subparsers.add_parser(
        "collections", help="propose, confirm, and describe collections"
    )
    collections_sub = collections_parser.add_subparsers(
        dest="collections_command", metavar="ACTION"
    )

    propose_parser = collections_sub.add_parser(
        "propose", help="score subtrees and fill the review queue (confirms nothing)"
    )
    propose_parser.add_argument("--limit", type=int, default=25)
    propose_parser.add_argument(
        "--record", action="store_true",
        help="store the proposals in the review queue",
    )

    collections_sub.add_parser("queue", help="proposals awaiting your decision")
    collections_sub.add_parser("list", help="confirmed collections")

    accept_parser = collections_sub.add_parser(
        "accept", help="confirm a proposal as a collection"
    )
    accept_parser.add_argument("candidate_id", type=int)
    accept_parser.add_argument("--title")
    accept_parser.add_argument("--license")
    accept_parser.add_argument("--data-type")

    reject_parser = collections_sub.add_parser(
        "reject", help="decline a proposal; it will not be offered again"
    )
    reject_parser.add_argument("candidate_id", type=int)

    merge_parser = collections_sub.add_parser(
        "merge", help="add a proposal's subtree to an existing collection"
    )
    merge_parser.add_argument("candidate_id", type=int)
    merge_parser.add_argument("collection_id", type=int)

    describe_parser = collections_sub.add_parser(
        "describe", help="fill in the metadata the scorecard asks for"
    )
    describe_parser.add_argument("collection_id", type=int)
    for field in (
        "title", "description", "contact", "license", "data-type", "project",
        "retention-note", "external-pid",
    ):
        describe_parser.add_argument(f"--{field}")
    describe_parser.add_argument(
        "--creators", help="comma-separated list of creator names"
    )
    describe_parser.add_argument("--ndsa-target", type=int, choices=(1, 2, 3, 4))
    describe_parser.add_argument("--min-copies", type=int)

    collections_sub.add_parser("vocab", help="the controlled terms available")
    collections_sub.add_parser(
        "coverage", help="how much of the archive is filed, and how much is not"
    )

    score_parser = subparsers.add_parser(
        "score", help="FAIR and NDSA scorecards"
    )
    score_parser.add_argument(
        "collection_id", type=int, nargs="?",
        help="one collection, or all of them plus the unfiled bucket",
    )
    score_parser.add_argument(
        "--formats", action="store_true",
        help="list at-risk formats and open alternatives (a recommendation only)",
    )

    health = subparsers.add_parser(
        "health", help="read drive health and estimate failure risk"
    )
    health_sub = health.add_subparsers(dest="health_command", metavar="ACTION")
    health_collect = health_sub.add_parser(
        "collect", help="read SMART for every known drive and record it"
    )
    health_collect.add_argument(
        "--dry-run", action="store_true",
        help="show what would be read without recording anything",
    )
    health_sub.add_parser("show", help="current health and risk per drive")
    health_set = health_sub.add_parser(
        "set", help="record what only you know: purchase date, location, role"
    )
    health_set.add_argument("drive_id", type=int)
    for field in (
        "nickname", "purchase-date", "vendor", "warranty-end", "batch-key",
        "role", "location", "disaster-zone", "smart-device", "media", "bus",
    ):
        health_set.add_argument(f"--{field}")
    health_set.add_argument("--purchase-price-cents", type=int)

    subparsers.add_parser("gui", help="open the desktop window")
    subparsers.add_parser("status", help="catalog summary")
    subparsers.add_parser("volumes", help="volumes currently attached")

    for name, (help_text, milestone) in PLANNED_VERBS.items():
        planned = subparsers.add_parser(name, help=f"{help_text} [{milestone}]")
        planned.set_defaults(milestone=milestone)

    return parser


# -- commands ----------------------------------------------------------------

def cmd_scope_add(args) -> int:
    with Catalog(args.catalog) as catalog:
        existing = [root.path for root in catalog.scope_roots()]
        try:
            root_path = validate_new_root(args.path, existing)
        except ScopeError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1

        try:
            volume = discovery.identify_volume(root_path)
        except discovery.DiscoveryError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1

        drive_id = None
        if volume.drive and volume.drive.serial:
            drive_id = catalog.upsert_drive(
                serial=volume.drive.serial,
                model=volume.drive.model,
                manufacturer=volume.drive.manufacturer,
                bus=volume.drive.bus,
                media=volume.drive.media,
            )
        fields = volume.as_volume_fields()
        if drive_id is not None:
            fields["drive_id"] = drive_id
        volume_id = catalog.upsert_volume(volume_guid=volume.volume_guid, **fields)

        root_id = catalog.add_scope_root(volume_id, root_path, args.exclude)
        print(f"added scope root {root_id}: {root_path}")
        print(
            f"  volume {volume.label or volume.volume_guid} "
            f"({volume.fs_type or 'unknown'}, rescan: {volume.rescan_cost})"
        )
        if volume.rescan_cost == "full-walk":
            print(
                "  note: this filesystem has no change journal, so every "
                "rescan is a full re-walk"
            )
        return 0


def cmd_scope_list(args) -> int:
    with Catalog(args.catalog) as catalog:
        roots = catalog.scope_roots()
        if not roots:
            print("no scope roots; add one with: drivefusion scope add <path>")
            return 0
        volumes = {row["id"]: row for row in catalog.volumes()}
        for root in roots:
            volume = volumes.get(root.volume_id)
            label = (volume["label"] or volume["volume_guid"]) if volume else "?"
            fs_type = volume["fs_type"] if volume else "?"
            cost = volume["rescan_cost"] if volume else "?"
            age = describe_age(volume["last_seen_at"] if volume else None)
            print(f"[{root.id}] {root.path}")
            print(f"      volume: {label} ({fs_type}, rescan: {cost})")
            print(f"      last scanned: {age}")
            if root.excludes:
                print(f"      excludes: {', '.join(root.excludes)}")
        return 0


def cmd_scope_remove(args) -> int:
    with Catalog(args.catalog) as catalog:
        if catalog.remove_scope_root(args.root_id):
            print(f"removed scope root {args.root_id}")
            print("catalogued entries are kept; nothing on disk was touched")
            return 0
        print(f"drivefusion: no scope root {args.root_id}", file=sys.stderr)
        return 1


def cmd_scope_preview(args) -> int:
    with Catalog(args.catalog) as catalog:
        root = catalog.scope_root(args.root_id)
        if root is None:
            print(f"drivefusion: no scope root {args.root_id}", file=sys.stderr)
            return 1
    counters = preview_root(root.path, ExclusionSet.for_root(root.excludes))
    print(f"preview of {root.path} (nothing was written)")
    print(f"  directories: {counters.dirs:,}")
    print(f"  files:       {counters.files:,}")
    print(f"  bytes:       {_human_bytes(counters.bytes)}")
    if counters.excluded:
        print(f"  excluded:    {counters.excluded:,}")
    if counters.unreadable:
        print(f"  unreadable:  {counters.unreadable:,}")
    return 0


def cmd_scan(args) -> int:
    with Catalog(args.catalog) as catalog:
        roots = catalog.scope_roots(enabled_only=True)
        if args.root_id is not None:
            roots = [r for r in roots if r.id == args.root_id]
            if not roots:
                print(f"drivefusion: no scope root {args.root_id}", file=sys.stderr)
                return 1
        if not roots:
            print("no scope roots; add one with: drivefusion scope add <path>")
            return 1

        volumes = {row["id"]: row for row in catalog.volumes()}
        for root in roots:
            volume = volumes.get(root.volume_id)
            decision = plan_enumeration(
                supports_usn=bool(volume["supports_usn"]) if volume else False,
                elevated=False,
                journal_id=volume["usn_journal_id"] if volume else None,
                next_usn=volume["usn_next"] if volume else None,
            )
            print(f"scanning [{root.id}] {root.path} ...")
            print(
                f"  method: {method_summary(decision, volume['supports_usn'] if volume else False)}"
            )
            result = scan_root(
                catalog,
                root_path=root.path,
                root_id=root.id,
                volume_id=root.volume_id,
                excludes=ExclusionSet.for_root(root.excludes),
                method=decision.method.value,
                supports_file_ids=(
                    bool(volume["supports_file_ids"]) if volume else None
                ),
            )
            counters = result["counters"]
            merged = result["merged"]
            print(
                f"  {counters.dirs:,} directories, {counters.files:,} files, "
                f"{_human_bytes(counters.bytes)}"
            )
            print(f"  coverage: {result['coverage']}")
            if merged["vanished"]:
                print(
                    f"  {merged['vanished']:,} entries no longer present "
                    "(recorded, not removed)"
                )
            if counters.unreadable_paths:
                print("  first unreadable paths:")
                for path in counters.unreadable_paths[:5]:
                    print(f"    {path}")
        return 0


def cmd_find(args) -> int:
    pattern = args.pattern if "%" in args.pattern else f"%{args.pattern}%"
    with Catalog(args.catalog) as catalog:
        rows = list(
            catalog.find(
                pattern,
                limit=args.limit,
                include_vanished=args.include_vanished,
            )
        )
        if not rows:
            print("no matches")
            return 1
        for row in rows:
            marker = " (vanished)" if row["vanished_at"] else ""
            print(
                f"{_human_bytes(row['size_bytes']):>12}  "
                f"{row['path']}{os.sep}{row['name']}{marker}"
            )
        print(f"\n{len(rows)} match(es)")
        return 0


def cmd_hash(args) -> int:
    """Tier 1 and tier 2 hashing.

    This is the only verb that reads file *contents*; it still opens every file
    read-only through the gateway and writes nothing back to disk.
    """
    with Catalog(args.catalog) as catalog:
        def report(counters) -> None:
            print(f"  {counters.summary()}", flush=True)

        print(f"hashing with {HASH_ALGO} (files are opened read-only) ...")
        counters = hash_pass(
            catalog, volume_id=args.volume, limit=args.limit, progress=report
        )
        if not counters.candidates:
            print(
                "nothing to hash: every catalogued file already has an identity, "
                "or has a size shared with no other file"
            )
            return 0
        print(f"  {counters.summary()}")
        print(f"  read {_human_bytes(counters.bytes_read)} of file contents")
        if counters.unreadable_paths:
            print("  first unreadable paths:")
            for line in counters.unreadable_paths[:5]:
                print(f"    {line}")
        return 0


def _print_group(catalog, group, *, show_paths: int = 4) -> None:
    line = (
        f"{_human_bytes(group.size_bytes):>12}  "
        f"{group.paths} path(s) on {group.drives} drive(s)"
    )
    if group.physical_copies != group.paths:
        # Hardlinks: fewer real copies than paths, and the difference matters.
        line += f", {group.physical_copies} physical copies"
    if group.reclaimable_bytes:
        line += f" — {_human_bytes(group.reclaimable_bytes)} duplicated on one drive"
    print(line)
    for path in content_paths(catalog, group.content_id, limit=show_paths):
        print(f"              {path}")
    if group.paths > show_paths:
        print(f"              ... and {group.paths - show_paths} more")


def cmd_report(args) -> int:
    with Catalog(args.catalog) as catalog:
        summary = redundancy_summary(catalog, min_copies=args.min_copies)

        if summary["unhashed_files"] and args.kind != "integrity":
            # Stated, never implied: a report over a partly-hashed catalog
            # describes only the part that was hashed.
            print(
                f"note: {summary['unhashed_files']:,} catalogued files have no "
                "content identity yet.\n"
                "      Files with a size shared by no other file are never "
                "opened and never will be;\n"
                "      run `drivefusion hash` if you expect more coverage than "
                "this.\n"
            )

        if args.kind in ("summary", "redundancy"):
            print("catalog")
            print(f"  distinct content:  {summary['distinct_content']:,}")
            print(f"  catalogued paths:  {summary['catalogued_paths']:,}")
            print(f"  bytes over paths:  {_human_bytes(summary['path_bytes'])}")
            print(f"  distinct bytes:    {_human_bytes(summary['unique_bytes'])}")
            print(
                "  duplicate weight:  "
                f"{_human_bytes(summary['duplicate_overhead_bytes'])}"
            )
            print(
                f"\ndurability (target: {args.min_copies} copies on distinct drives)"
            )
            print(f"  under-protected:   {summary['under_protected_items']:,} items")
            print(
                "  at risk:           "
                f"{_human_bytes(summary['under_protected_bytes'])}"
            )
            print(
                "  same-drive waste:  "
                f"{_human_bytes(summary['reclaimable_bytes'])}"
            )

        if args.kind in ("summary", "redundancy"):
            exposed = under_protected(
                catalog, min_copies=args.min_copies, limit=args.top
            )
            if exposed:
                print(
                    f"\nheld on fewer than {args.min_copies} drives "
                    f"(largest {len(exposed)}):"
                )
                for group in exposed:
                    _print_group(catalog, group)

        if args.kind == "duplicates":
            groups = duplicate_groups(catalog, limit=args.top)
            if not groups:
                print("no content found at more than one path")
                return 0
            print(f"content at more than one path (largest {len(groups)}):")
            for group in groups:
                _print_group(catalog, group)

        if args.kind == "summary":
            waste = reclamation_candidates(catalog, limit=args.top)
            if waste:
                print(
                    f"\nduplicated within a single drive — space without "
                    f"durability (largest {len(waste)}):"
                )
                for group in waste:
                    _print_group(catalog, group)
                print(
                    "\nThese are observations, not instructions. Drive Fusion "
                    "never deletes\nand never emits a command that would."
                )

        if args.kind in ("summary", "integrity"):
            incidents = integrity_incidents(catalog, limit=args.top)
            if incidents:
                print(f"\nfixity failures ({len(incidents)}):")
                for incident in incidents:
                    print(
                        f"  {incident['result']:10} {incident['path']}"
                        f"{os.sep}{incident['name']}"
                    )
                    print(
                        f"             checked {incident['checked_at']}; this "
                        f"content is on {incident['other_copies']} drive(s)"
                    )
            elif args.kind == "integrity":
                print("no fixity failures recorded; run `drivefusion verify` to check")

        if args.kind in ("summary", "risk"):
            _print_risk(catalog, args)

        return 0


def _print_risk(catalog, args) -> None:
    """Expected annual loss, and which drive to act on first."""
    from drivefusion.core.analysis.exposure import (
        expected_bytes_lost_per_year,
        riskiest_drives,
    )

    exposure = expected_bytes_lost_per_year(catalog)
    if not exposure["content_items"]:
        if args.kind == "risk":
            print(
                "No content has a hash yet, so there is nothing to put a risk "
                "figure on.\nRun `drivefusion hash`, then "
                "`drivefusion health collect`."
            )
        return

    print("\nrisk")
    print(
        "  expected loss:     "
        f"{_human_bytes(int(exposure['expected_bytes_lost_per_year']))}/year"
    )
    print(f"  content assessed:  {exposure['content_items']:,} items")
    print(f"  rates:             {exposure['table']}")

    measured = exposure["by_confidence"].get("measured", 0)
    total = exposure["content_items"]
    if measured < total:
        # Stated, not implied: most of this figure may rest on age alone.
        print(
            f"  evidence:          {measured:,} of {total:,} items rest on "
            "drive counters; the rest are age-based estimates"
        )
    if exposure["generic_rates"]:
        print(
            "  caveat:            failure rates are generic defaults, not "
            "published fleet data (standards/README.md)"
        )
    if exposure["unhashed_files"]:
        print(
            f"  not included:      {exposure['unhashed_files']:,} files have no "
            "content identity, so they are absent from the figure above"
        )

    drives = riskiest_drives(catalog, limit=args.top)
    if drives:
        print("\n  drives ranked by what their failure would cost:")
        for entry in drives:
            print(
                f"    {entry['name']}: "
                f"{_human_bytes(int(entry['expected_bytes_lost_per_year']))}/yr "
                f"at risk — {_human_bytes(entry['sole_copy_bytes'])} exists "
                f"nowhere else ({entry['annual_probability'] * 100:.2f}%/yr, "
                f"{entry['confidence']})"
            )
            for reason in entry["reasons"][:3]:
                print(f"        · {reason}")


def cmd_verify(args) -> int:
    with Catalog(args.catalog) as catalog:
        print("verifying content against recorded hashes ...")
        result = verify_pass(catalog, volume_id=args.volume, limit=args.limit)
        print(f"  checked:    {result['checked']:,}")
        print(f"  mismatched: {result['mismatched']:,}")
        print(f"  unreadable: {result['unreadable']:,}")
        if not result["checked"] and not result["unreadable"]:
            print(
                "\nNothing in this catalog has a hash to verify against yet. "
                "Run `drivefusion hash`\nfirst; note that files whose size is "
                "shared with no other file are never opened,\nso they carry no "
                "hash and cannot be checked for corruption."
            )
            return 0
        for incident in result["incidents"][:20]:
            print(f"  {incident['result']:10} {incident['path']}")
        # A mismatch is a finding, not a crash, but it should not look like success.
        return 1 if result["mismatched"] else 0


# -- collections --------------------------------------------------------------

def cmd_collections_propose(args) -> int:
    from drivefusion.core.curate import candidates

    with Catalog(args.catalog) as catalog:
        proposals = candidates.propose(catalog, limit=args.limit)
        if not proposals:
            print(
                "No candidates. Either everything is already filed, or no "
                "subtree looks enough like a collection to propose — both are "
                "fine. You can also create one by hand."
            )
            return 0

        print(f"{len(proposals)} proposal(s). Nothing is confirmed until you say so.\n")
        for proposal in proposals:
            print(f"  {proposal.proposed_title}")
            print(f"    path:     {proposal.path}")
            print(
                f"    size:     {proposal.files:,} files, "
                f"{_human_bytes(proposal.bytes)}"
            )
            print(f"    evidence: {proposal.why()}")
            if proposal.sample_paths:
                print(f"    samples:  {', '.join(proposal.sample_paths[:3])}")
            print(f"    score:    {proposal.confidence:.2f}")
            print()

        if args.record:
            stored = candidates.record(catalog, proposals)
            print(f"{stored} proposal(s) in the review queue.")
            print("Confirm with: drivefusion collections accept <id>")
        else:
            print("Nothing was stored. Pass --record to queue these for review.")
        return 0


def cmd_collections_queue(args) -> int:
    import json as _json

    from drivefusion.core.curate import candidates

    with Catalog(args.catalog) as catalog:
        queue = candidates.pending(catalog)
        if not queue:
            print("Nothing awaiting review. Run `collections propose --record`.")
            return 0

        for row in queue:
            evidence = _json.loads(row["evidence_json"])
            print(f"[{row['id']}] {row['proposed_title']}  ({row['confidence']:.2f})")
            print(f"      {evidence.get('path', '')}")
            print(
                f"      {row['files']:,} files, {_human_bytes(row['bytes'])}"
            )
            for signal in evidence.get("signals", []):
                if signal["score"] > 0:
                    print(f"      · {signal['detail']}")
            print()
        print("accept <id> · reject <id> · merge <id> <collection_id>")
        return 0


def cmd_collections_list(args) -> int:
    from drivefusion.core.curate import collections as collections_module

    with Catalog(args.catalog) as catalog:
        items = collections_module.all_collections(catalog)
        if not items:
            print("No confirmed collections yet.")
            return 0

        for collection in items:
            stats = collections_module.stats(catalog, collection.id)
            print(f"[{collection.id}] {collection.title}")
            print(f"      pid:     {collection.local_pid}")
            print(
                f"      content: {stats['files']:,} files, "
                f"{_human_bytes(stats['bytes'])}"
            )
            # A missing licence is the finding the scorecard blocks on, so it is
            # visible in the listing rather than only in a report.
            print(f"      licence: {collection.license or 'NOT SET — blocks FAIR R1.1'}")
            print(f"      type:    {collection.data_type or '—'}")
        return 0


def cmd_collections_accept(args) -> int:
    from drivefusion.core.curate import candidates, collections as collections_module

    fields = {}
    if args.license:
        fields["license"] = args.license
    if args.data_type:
        fields["data_type"] = args.data_type

    with Catalog(args.catalog) as catalog:
        try:
            collection = candidates.accept(
                catalog, args.candidate_id, title=args.title, **fields
            )
        except collections_module.CollectionError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1

        print(f"confirmed [{collection.id}] {collection.title}")
        print(f"  pid: {collection.local_pid}")
        print(
            "  membership is a rule over that subtree, so files added there "
            "later join automatically"
        )
        if not collection.license:
            print(
                "\n  No licence set. FAIR R1.1 treats that as a blocking "
                "finding — unlicensed\n  data is not reusable by anyone else. "
                "Set one with `collections describe`."
            )
        return 0


def cmd_collections_reject(args) -> int:
    from drivefusion.core.curate import candidates

    with Catalog(args.catalog) as catalog:
        if candidates.reject(catalog, args.candidate_id):
            print(
                f"rejected candidate {args.candidate_id}; it will not be "
                "proposed again"
            )
            return 0
        print(
            f"drivefusion: no pending candidate {args.candidate_id}",
            file=sys.stderr,
        )
        return 1


def cmd_collections_merge(args) -> int:
    from drivefusion.core.curate import candidates, collections as collections_module

    with Catalog(args.catalog) as catalog:
        try:
            collection = candidates.merge_into(
                catalog, args.candidate_id, args.collection_id
            )
        except collections_module.CollectionError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1
        print(f"merged into [{collection.id}] {collection.title}")
        return 0


def cmd_collections_describe(args) -> int:
    import json as _json

    from drivefusion.core.curate import collections as collections_module

    fields = {}
    for name in (
        "title", "description", "contact", "license", "data_type", "project",
        "retention_note", "external_pid", "ndsa_target", "min_copies",
    ):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value
    if args.creators:
        fields["creators_json"] = _json.dumps(
            [part.strip() for part in args.creators.split(",") if part.strip()]
        )

    if not fields:
        print("nothing to set; pass at least one field", file=sys.stderr)
        return 2

    with Catalog(args.catalog) as catalog:
        try:
            collections_module.update(catalog, args.collection_id, **fields)
        except collections_module.CollectionError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1
        print(f"updated collection {args.collection_id}: {', '.join(sorted(fields))}")
        return 0


def cmd_collections_vocab(args) -> int:
    from drivefusion.core import standards

    vocab = standards.vocabularies()
    print(f"{vocab.citation}\n")
    print("licences (FAIR R1.1):")
    for entry in vocab["licenses"]:
        marker = "" if entry.get("reusable") else "  [not reusable by others]"
        print(f"  {entry['id']:22} {entry['label']}{marker}")
    print("\ndata types (FAIR I2):")
    for entry in vocab["data_types"]:
        print(f"  {entry['id']:22} {entry['label']}")
    print(
        "\nA value outside these lists is recorded and reported as "
        "uncontrolled, never refused."
    )
    return 0


def cmd_collections_coverage(args) -> int:
    from drivefusion.core.curate import collections as collections_module

    with Catalog(args.catalog) as catalog:
        report = collections_module.coverage(catalog)
        print("coverage")
        print(f"  catalogued:  {_human_bytes(report['total_bytes'])} "
              f"({report['total_files']:,} files)")
        print(f"  in a collection: {_human_bytes(report['filed_bytes'])} "
              f"({report['byte_share']:.0%} of bytes)")
        print(f"  unfiled:     {_human_bytes(report['unfiled_bytes'])} "
              f"({report['unfiled_files']:,} files)")
        if report["unfiled_bytes"]:
            print(
                "\nUnfiled is a legitimate permanent state for content you "
                "never intend to curate.\nIt is reported because a scorecard "
                "over the filed share, presented as a scorecard\nfor the "
                "archive, would be a lie."
            )
        if report["overlap_warning"]:
            print(
                "\nWarning: some collections claim overlapping directories, so "
                "the filed total\ncounts those bytes once rather than twice."
            )
        return 0


# -- scorecards ---------------------------------------------------------------

def cmd_score(args) -> int:
    from drivefusion.core.curate import collections as collections_module
    from drivefusion.core.fair import ndsa, scorecard

    with Catalog(args.catalog) as catalog:
        if args.collection_id is not None:
            targets = [collections_module.get(catalog, args.collection_id)]
        else:
            targets = collections_module.all_collections(catalog)

        if not targets:
            print(
                "No collections to score. Run `collections propose --record` "
                "and confirm one."
            )
            return 1

        for collection in targets:
            card = scorecard.score(catalog, collection.id)
            print(f"[{collection.id}] {collection.title}  ({collection.local_pid})")
            print(f"  FAIR: {card.summary()}")
            print(f"  rubric: {card.rubric}")

            for result in card.results:
                if result.status == "pass":
                    print(f"    pass  {result.id:5} {result.title}")
            for result in card.failures:
                flag = "BLOCKING" if result.blocking else "gap"
                print(f"    {flag:8} {result.id:5} {result.title}")
                print(f"              {result.detail}")
                print(f"              {result.gap}")
                print(f"      remedy: {result.remedy}")
            for result in card.not_assessed:
                print(f"    not scored  {result.id:5} {result.detail}")

            levels = ndsa.score(catalog, collection.id)
            print(f"\n  {levels.summary()}")
            for area in levels.areas:
                ceiling = (
                    f", most this tool can assess is {area.ceiling}"
                    if area.ceiling is not None and area.ceiling < 4
                    else ""
                )
                print(f"    {area.title}: level {area.reached}{ceiling}")
                for level in area.levels:
                    if level.status == "fail":
                        print(f"      next: {level.statement} — {level.detail}")
                        break

            if args.formats:
                recommendations = scorecard.format_recommendations(
                    catalog, collection.id
                )
                if recommendations:
                    print("\n  at-risk formats (a recommendation only — Drive "
                          "Fusion never converts anything):")
                    for entry in recommendations:
                        print(
                            f"    .{entry['extension']}: {entry['files']:,} files, "
                            f"{_human_bytes(entry['bytes'])}"
                        )
                        print(f"      {entry['why']}")
                        if entry["alternative"]:
                            print(f"      consider: {entry['alternative']}")
            print()

        if args.collection_id is None:
            unfiled = ndsa.score_unfiled(catalog)
            coverage = collections_module.coverage(catalog)
            print(f"Unfiled content — {unfiled.summary()}")
            print(
                f"  {_human_bytes(coverage['unfiled_bytes'])} in no collection "
                f"({1 - coverage['byte_share']:.0%} of catalogued bytes)."
            )
            print(
                "  Scored separately on purpose: the grades above describe the "
                "filed share only."
            )
        return 0


def _confidence_note(confidence: str) -> str:
    return {
        "measured": "from the drive's own counters",
        "estimated": "estimated from age and media — no SMART counters available",
        "unknown": "nothing known about this drive; a placeholder, not a finding",
    }.get(confidence, confidence)


def cmd_health_collect(args) -> int:
    from drivefusion.core.health import afr
    from drivefusion.core.health.collect import collect_health
    from drivefusion.core.health.smart import available as smart_available

    with Catalog(args.catalog) as catalog:
        drives = catalog.drives()
        if not drives:
            print("no drives known yet; add a scope root and scan first")
            return 1

        table = afr.load()
        if args.dry_run:
            print(f"would read health for {len(drives):,} drive(s)")
            print(f"  smartctl: {'found' if smart_available() else 'not installed'}")
            print(f"  rates:    {table.citation}")
            print("nothing was read and nothing was recorded")
            return 0

        print(f"reading health for {len(drives):,} drive(s) ...")
        result = collect_health(catalog, table=table)
        print(f"  {result.summary()}")
        for note in result.notes:
            print(f"  note: {note}")
        if table.is_generic:
            print(
                "\nFailure rates come from generic defaults, not published "
                "fleet data.\nSee standards/README.md to load real per-model "
                "figures."
            )
        return 0


def cmd_health_show(args) -> int:
    from drivefusion.core.analysis.exposure import RiskIndex
    from drivefusion.core.health import afr

    with Catalog(args.catalog) as catalog:
        drives = catalog.drives()
        if not drives:
            print("no drives known yet; add a scope root and scan first")
            return 1

        table = afr.load()
        index = RiskIndex(catalog, table=table)
        print(f"rates: {table.citation}\n")

        for row in drives:
            drive = dict(row)
            risk = index.risk_for(int(drive["id"]))
            name = (
                drive.get("nickname")
                or drive.get("model")
                or drive.get("serial")
                or f"drive {drive['id']}"
            )
            print(f"[{drive['id']}] {name}")
            print(
                f"      {risk.annual_probability * 100:.2f}%/yr  "
                f"({_confidence_note(risk.confidence)})"
            )
            if drive.get("purchase_date"):
                print(f"      purchased: {drive['purchase_date']}")
            if drive.get("location") or drive.get("disaster_zone"):
                print(
                    f"      location:  {drive.get('location') or '-'}"
                    f" / zone {drive.get('disaster_zone') or '-'}"
                )

            health = catalog.latest_health(int(drive["id"]))
            if health is not None:
                counters = [
                    f"{label} {health[column]}"
                    for label, column in (
                        ("pending", "pending"),
                        ("reallocated", "reallocated"),
                        ("uncorrectable", "uncorrectable"),
                        ("CRC", "crc_errors"),
                        ("hours", "power_on_hours"),
                    )
                    if health[column] is not None
                ]
                print(f"      counters:  {', '.join(counters) or 'none reported'}")
                print(f"      observed:  {describe_age(health['observed_at'])}")
            for reason in risk.reasons:
                print(f"      · {reason}")
            print()
        return 0


def cmd_health_set(args) -> int:
    fields = {}
    for name in (
        "nickname", "purchase_date", "vendor", "warranty_end", "batch_key",
        "role", "location", "disaster_zone", "smart_device", "media", "bus",
        "purchase_price_cents",
    ):
        value = getattr(args, name, None)
        if value is not None:
            fields[name] = value

    if not fields:
        print("nothing to set; pass at least one field", file=sys.stderr)
        return 2

    with Catalog(args.catalog) as catalog:
        if catalog.drive(args.drive_id) is None:
            print(f"drivefusion: no drive {args.drive_id}", file=sys.stderr)
            return 1
        catalog.update_drive(args.drive_id, **fields)
        print(f"updated drive {args.drive_id}: {', '.join(sorted(fields))}")
        if "purchase_date" in fields or "batch_key" in fields:
            print(
                "Purchase date and batch key feed the durability model: two "
                "drives from one\norder are not two independent copies, and the "
                "risk figures now say so."
            )
        return 0


def cmd_gui(args) -> int:
    """Open the window.

    The GUI is an optional extra, so a missing PySide6 is an instruction rather
    than a traceback — the CLI does everything the window does.
    """
    from drivefusion.gui.app import main as gui_main

    return gui_main(catalog=args.catalog)


def cmd_status(args) -> int:
    with Catalog(args.catalog) as catalog:
        counts = catalog.counts()
        print(f"catalog: {args.catalog}")
        size = args.catalog.stat().st_size if args.catalog.exists() else 0
        print(f"  size:        {_human_bytes(size)}")
        print(f"  volumes:     {counts['volumes']:,}")
        print(f"  scope roots: {counts['roots']:,}")
        print(f"  directories: {counts['dirs']:,}")
        print(f"  files:       {counts['files']:,}")
        print(f"  bytes:       {_human_bytes(counts['bytes'])}")
        print(
            f"  identified:  {counts['identified']:,} files "
            f"({counts['content']:,} distinct content items)"
        )
        if counts["vanished"]:
            print(f"  vanished:    {counts['vanished']:,} (metadata retained)")
        if counts["files"]:
            print(f"  catalog cost: {size / counts['files']:.0f} bytes/file")

        volumes = catalog.volumes()
        if volumes:
            print("\nvolumes:")
            for volume in volumes:
                label = volume["label"] or volume["volume_guid"]
                print(
                    f"  {label} ({volume['fs_type'] or '?'}) "
                    f"rescan: {volume['rescan_cost'] or '?'}, "
                    f"last scanned {describe_age(volume['last_seen_at'])}"
                )

        scans = catalog.scans(limit=5)
        if scans:
            print("\nrecent scans:")
            for scan in scans:
                print(
                    f"  [{scan['id']}] {scan['started_at']} {scan['status']:8} "
                    f"{scan['method'] or '-':9} {scan['files_seen']:,} files"
                )
        return 0


def cmd_volumes(args) -> int:
    volumes = discovery.list_volumes()
    if not volumes:
        print("no volumes discovered")
        return 1
    for volume in volumes:
        print(f"{volume.last_letter or volume.volume_guid}")
        print(f"  guid:    {volume.volume_guid}")
        print(f"  label:   {volume.label or '-'}")
        print(f"  fs:      {volume.fs_type or '-'}")
        if volume.capacity_bytes:
            print(
                f"  size:    {_human_bytes(volume.capacity_bytes)} "
                f"({_human_bytes(volume.free_bytes or 0)} free)"
            )
        if volume.cluster_bytes:
            print(f"  cluster: {_human_bytes(volume.cluster_bytes)}")
        print(f"  rescan:  {volume.rescan_cost}")
        if volume.drive and volume.drive.serial:
            drive = volume.drive
            print(
                f"  drive:   {drive.model or '?'} ({drive.serial}) "
                f"{drive.bus or '?'}/{drive.media or '?'}"
            )
    return 0


#: Verbs that require an action, and print their own help without one.
SUBCOMMAND_VERBS = ("scope", "health", "collections")

DISPATCH = {
    ("scope", "add"): cmd_scope_add,
    ("scope", "list"): cmd_scope_list,
    ("scope", "remove"): cmd_scope_remove,
    ("scope", "preview"): cmd_scope_preview,
    ("scan", None): cmd_scan,
    ("find", None): cmd_find,
    ("hash", None): cmd_hash,
    ("report", None): cmd_report,
    ("verify", None): cmd_verify,
    ("collections", "propose"): cmd_collections_propose,
    ("collections", "queue"): cmd_collections_queue,
    ("collections", "list"): cmd_collections_list,
    ("collections", "accept"): cmd_collections_accept,
    ("collections", "reject"): cmd_collections_reject,
    ("collections", "merge"): cmd_collections_merge,
    ("collections", "describe"): cmd_collections_describe,
    ("collections", "vocab"): cmd_collections_vocab,
    ("collections", "coverage"): cmd_collections_coverage,
    ("score", None): cmd_score,
    ("health", "collect"): cmd_health_collect,
    ("health", "show"): cmd_health_show,
    ("health", "set"): cmd_health_set,
    ("gui", None): cmd_gui,
    ("status", None): cmd_status,
    ("volumes", None): cmd_volumes,
}


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """The subparser registered for a verb, for printing its help."""
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if choices and hasattr(choices, "get") and name in choices:
            return choices[name]
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        if wants_window(
            sys.argv[0] if sys.argv else "",
            frozen=getattr(sys, "frozen", False),
            has_command=False,
        ):
            return cmd_gui(args)
        parser.print_help()
        return 0

    if args.command in PLANNED_VERBS:
        _, milestone = PLANNED_VERBS[args.command]
        print(
            f"drivefusion {args.command}: not implemented yet — scheduled for "
            f"{milestone}. See docs/PLAN.md §15.",
            file=sys.stderr,
        )
        return 2

    # Verbs with their own subcommands store it under `<verb>_command`. Derived
    # from the verb rather than hardcoded: `health show` silently fell through
    # to the top-level help when only `scope_command` was consulted.
    action = getattr(args, f"{args.command}_command", None)

    if args.command in SUBCOMMAND_VERBS and not action:
        # print_help() on the subparser rather than parse_args([verb, "--help"]),
        # which raises SystemExit and made the return below unreachable.
        _subparser(parser, args.command).print_help()
        return 0

    handler = DISPATCH.get((args.command, action))
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler(args)
    except BrokenPipeError:
        # Piping into `head` closes stdout early; that is a normal way to use a
        # CLI, not an error worth a traceback.
        try:
            sys.stdout.close()
        finally:
            return 0
    except KeyboardInterrupt:
        print("\ninterrupted; nothing on disk was modified", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
