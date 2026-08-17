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
    "score": ("FAIR and NDSA scorecards", "M6"),
    "plan": ("build a consolidation plan", "M7"),
    "export": ("write reports, manifests, and bundles to an export folder", "M8"),
}


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
        choices=("summary", "duplicates", "redundancy", "integrity"),
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

        return 0


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
    ("status", None): cmd_status,
    ("volumes", None): cmd_volumes,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
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

    if args.command == "scope" and not getattr(args, "scope_command", None):
        parser.parse_args(["scope", "--help"])
        return 0

    handler = DISPATCH.get((args.command, getattr(args, "scope_command", None)))
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
