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
from drivefusion.core.scan import preview_root, scan_root
from drivefusion.core.scope import ExclusionSet, ScopeError, validate_new_root
from drivefusion.core.store.catalog import Catalog

BANNER = "Drive Fusion — read-only cataloging and consolidation planning"

DEFAULT_CATALOG = Path(
    os.environ.get("DRIVEFUSION_CATALOG", Path.home() / ".drivefusion" / "catalog.db")
)

#: Verbs not yet implemented, and the milestone that lands each.
PLANNED_VERBS = {
    "report": ("duplicate, redundancy, and integrity reports", "M3"),
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
            print(f"[{root.id}] {root.path}")
            print(f"      volume: {label} ({fs_type}, rescan: {cost})")
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

        for root in roots:
            print(f"scanning [{root.id}] {root.path} ...")
            result = scan_root(
                catalog,
                root_path=root.path,
                root_id=root.id,
                volume_id=root.volume_id,
                excludes=ExclusionSet.for_root(root.excludes),
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
        if counts["vanished"]:
            print(f"  vanished:    {counts['vanished']:,} (metadata retained)")
        if counts["files"]:
            print(f"  catalog cost: {size / counts['files']:.0f} bytes/file")

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
