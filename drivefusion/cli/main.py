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
import textwrap
from pathlib import Path

from drivefusion import __version__
from drivefusion.core import discovery
from drivefusion.core.analysis import (
    drive_inventory,
    duplicate_groups,
    group_paths,
    integrity_incidents,
    reclamation_candidates,
    redundancy_summary,
    under_protected,
)
from drivefusion.core.enum.backend import describe_age, method_summary, plan_enumeration
from drivefusion.core.errors import CatalogLocationError
from drivefusion.core.identity import HASH_ALGO, hash_pass, verify_pass
from drivefusion.core.scan import preview_root, scan_root
from drivefusion.core.scope import (
    ExclusionSet,
    ScopeError,
    bind_roots,
    validate_new_root,
)
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
    parser.add_argument(
        "--accept-disclaimer",
        action="store_true",
        help="accept the scope disclaimer without a prompt, for scripts (recorded "
             "in the audit log); DRIVEFUSION_ACCEPT_DISCLAIMER=1 does the same",
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

    drives = subparsers.add_parser(
        "drives", help="which physical drive each volume is on"
    )
    drives_sub = drives.add_subparsers(dest="drives_command", metavar="ACTION")
    drives_sub.add_parser("list", help="volumes, their drives, and how each is identified")
    drives_confirm = drives_sub.add_parser(
        "confirm", help="record that a volume with an unverified drive is a drive of its own"
    )
    drives_confirm.add_argument("volume_id", type=int)
    drives_confirm.add_argument("--nickname", default=None)
    drives_assign = drives_sub.add_parser(
        "assign", help="record that a volume with an unverified drive is on a known drive"
    )
    drives_assign.add_argument("volume_id", type=int)
    drives_assign.add_argument("drive_id", type=int)

    audit_cmd = subparsers.add_parser(
        "audit", help="the catalog's append-only, hash-chained record of what was done"
    )
    audit_sub = audit_cmd.add_subparsers(dest="audit_command", metavar="ACTION")
    audit_list = audit_sub.add_parser("list", help="the most recent entries, newest first")
    audit_list.add_argument("--limit", type=int, default=20)
    audit_sub.add_parser("verify", help="check that no entry was changed, removed or reordered")

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

    subparsers.add_parser("gui", help="open the desktop window")
    subparsers.add_parser("status", help="catalog summary")
    subparsers.add_parser("volumes", help="volumes currently attached")

    for name, (help_text, milestone) in PLANNED_VERBS.items():
        planned = subparsers.add_parser(name, help=f"{help_text} [{milestone}]")
        planned.set_defaults(milestone=milestone)

    return parser


# -- the first-run disclaimer (docs/PLAN.md §1) ---------------------------------

#: Commands that read the user's drives. Nothing is read before the scope
#: disclaimer has been accepted (REVIEW SR-25).
READS_DRIVES = {
    ("scope", "add"), ("scope", "preview"), ("scan", None), ("hash", None),
    ("verify", None),
}


def _disclaimer_gate(args) -> bool:
    """True when the disclaimer is, or has just been, accepted."""
    from drivefusion.core import disclaimer

    with Catalog(args.catalog) as catalog:
        if disclaimer.accepted(catalog):
            return True
        if args.accept_disclaimer:
            disclaimer.accept(catalog, "flag")
            return True
        if disclaimer.accepted_by_environment():
            disclaimer.accept(catalog, "env")
            return True
        if sys.stdin.isatty():
            print("Before Drive Fusion reads any drive, please read and accept:\n")
            print(textwrap.fill(disclaimer.TEXT, 78) + "\n")
            try:
                answer = input("Type 'accept' to continue: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() == "accept":
                disclaimer.accept(catalog, "prompt")
                print("accepted; recorded in the audit log\n")
                return True
            print("not accepted; nothing was read", file=sys.stderr)
            return False
    print(
        "drivefusion: the scope disclaimer has not been accepted for this catalog, "
        "so no drive will be read:\n\n" + textwrap.fill(disclaimer.TEXT, 78) + "\n\n"
        "Run once in a terminal to accept it, or pass --accept-disclaimer (or set "
        f"{disclaimer.ENVIRONMENT_VARIABLE}=1) for scripted use. Either way, "
        "acceptance is recorded in the audit log.",
        file=sys.stderr,
    )
    return False


# -- commands ----------------------------------------------------------------

def cmd_scope_add(args) -> int:
    with Catalog(args.catalog) as catalog:
        existing = [root.path for root in catalog.scope_roots()]
        try:
            root_path = validate_new_root(args.path, existing)
            catalog.check_root_location(root_path)
        except (ScopeError, CatalogLocationError) as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1

        try:
            volume = discovery.identify_volume(root_path)
        except discovery.DiscoveryError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1
        try:
            catalog.check_root_location(root_path, volume.volume_guid)
        except CatalogLocationError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1

        volume_id = catalog.record_discovered_volume(volume)

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
        scanned = catalog.root_last_scanned()
        for root in roots:
            volume = volumes.get(root.volume_id)
            label = (volume["label"] or volume["volume_guid"]) if volume else "?"
            fs_type = volume["fs_type"] if volume else "?"
            cost = volume["rescan_cost"] if volume else "?"
            age = describe_age(scanned.get(root.id))
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
    if counters.unreadable_dirs:
        print(f"  unlistable folders: {counters.unreadable_dirs:,} (contents unknown)")
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

        # A root is a path, and a path names whatever is mounted there today.
        # Only roots whose volume is the catalogued one are scanned (F-01).
        binding = bind_roots(catalog, roots)
        for root, reason in binding.refused:
            print(
                f"drivefusion: not scanning [{root.id}] {root.path}: {reason}",
                file=sys.stderr,
            )

        failed = bool(binding.refused)
        volumes = {row["id"]: row for row in catalog.volumes()}
        for root in binding.bound:
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
            if not result["root_listed"]:
                failed = True
                print(f"drivefusion: {result['coverage']}", file=sys.stderr)
                continue
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
        return 1 if failed else 0


def _bound_root_ids(catalog, volume_id: int | None, verb: str):
    """Roots a content-reading pass may read, after reporting the ones it may not.

    Returns ``(root_ids, refused)``. ``root_ids`` is ``None`` when roots exist
    but none may be read, so the caller can stop.
    """
    roots = catalog.scope_roots()
    if volume_id is not None:
        roots = [r for r in roots if r.volume_id == volume_id]
    binding = bind_roots(catalog, roots)
    for root, reason in binding.refused:
        print(
            f"drivefusion: skipping files under [{root.id}] {root.path}: {reason}",
            file=sys.stderr,
        )
    if roots and not binding.bound:
        print(
            f"drivefusion: nothing to {verb}: no scope root "
            "is on its catalogued volume right now",
            file=sys.stderr,
        )
        return None, True
    return binding.root_ids, bool(binding.refused)


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
        root_ids, refused = _bound_root_ids(catalog, args.volume, "hash")
        if root_ids is None:
            return 1

        def report(counters) -> None:
            print(f"  {counters.summary()}", flush=True)

        print(f"hashing with {HASH_ALGO} (files are opened read-only) ...")
        counters = hash_pass(
            catalog,
            volume_id=args.volume,
            limit=args.limit,
            progress=report,
            root_ids=root_ids,
        )
        if not counters.candidates:
            print(
                "nothing to hash: every catalogued file already has an identity, "
                "or has a size shared with no other file"
            )
            return 1 if refused else 0
        print(f"  {counters.summary()}")
        print(f"  read {_human_bytes(counters.bytes_read)} of file contents")
        if counters.unreadable_paths:
            print("  first unreadable paths:")
            for line in counters.unreadable_paths[:5]:
                print(f"    {line}")
        return 1 if refused else 0


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
    if group.identity_uncertain:
        line += (
            f" (up to {group.drives_possible}: a copy is on a volume whose drive "
            "is unverified)"
        )
    print(line)
    for path in group_paths(catalog, group, limit=show_paths):
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
                f"note: {summary['unhashed_files']:,} catalogued files share a "
                "size with another file but have\n"
                "      no content identity yet, so they are left out of the "
                "figures below;\n"
                "      run `drivefusion hash` to identify them. Files whose size "
                "no other file\n"
                "      shares are counted as single copies without being "
                "opened.\n"
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
            if summary["unverified_drive_items"]:
                print(
                    f"    of which {summary['unverified_drive_items']:,} have a copy on "
                    "a volume whose drive is unverified;\n"
                    "    confirm it with `drivefusion drives` and they may be safe"
                )
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
        root_ids, refused = _bound_root_ids(catalog, args.volume, "verify")
        if root_ids is None:
            return 1
        print("verifying content against recorded hashes ...")
        result = verify_pass(
            catalog, volume_id=args.volume, limit=args.limit, root_ids=root_ids
        )
        print(f"  checked:    {result['checked']:,}")
        print(f"  mismatched: {result['mismatched']:,}")
        print(f"  unreadable: {result['unreadable']:,}")
        if result["modified"]:
            print(
                f"  modified:   {result['modified']:,} edited since the last scan "
                "(date changed);\n"
                "              not checked, and not corruption. Run `drivefusion "
                "scan` to catalogue the edits."
            )
        if not result["checked"] and not result["unreadable"] and not result["modified"]:
            print(
                "\nNothing in this catalog has a hash to verify against yet. "
                "Run `drivefusion hash`\nfirst; note that files whose size is "
                "shared with no other file are never opened,\nso they carry no "
                "hash and cannot be checked for corruption."
            )
            return 1 if refused else 0
        for incident in result["incidents"][:20]:
            print(f"  {incident['result']:10} {incident['path']}")
        # A mismatch is a finding, not a crash, but it should not look like
        # success; nor should a pass that had to skip a root.
        return 1 if result["mismatched"] or refused else 0


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
                    f"last scanned {describe_age(volume['last_scanned_at'])}"
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


#: How each drive identity is described to a user.
IDENTITY_WORDS = {
    "serial": "identified by serial number",
    "disk-layout": "identified by partition table",
    "user": "confirmed by you",
}


def cmd_drives_list(args) -> int:
    with Catalog(args.catalog) as catalog:
        rows = drive_inventory(catalog)
        if not rows:
            print("no volumes in the catalog yet; add one with `drivefusion scope add`")
            return 0
        unverified = 0
        for row in rows:
            label = row["label"] or row["volume_guid"]
            if row["drive_id"] is None:
                unverified += 1
                drive = "drive UNVERIFIED"
            else:
                name = row["nickname"] or row["model"] or row["serial"] or row["layout_id"]
                how = IDENTITY_WORDS.get(row["identity"], row["identity"] or "?")
                drive = f"drive {row['drive_id']} {name or ''} ({how})".replace("  ", " ")
            print(f"volume {row['volume_id']}: {label} ({row['fs_type'] or '?'}) — {drive}")
        if unverified:
            print(
                f"\n{unverified} volume(s) have no identified drive. Copies on them count "
                "as at most one\nproven copy until you say which drive each is on:\n"
                "  drivefusion drives confirm VOLUME          its own physical drive\n"
                "  drivefusion drives assign VOLUME DRIVE     on a drive listed above"
            )
        return 0


def cmd_drives_confirm(args) -> int:
    with Catalog(args.catalog) as catalog:
        try:
            drive_id = catalog.confirm_volume_drive(args.volume_id, nickname=args.nickname)
        except ValueError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1
        print(f"volume {args.volume_id} recorded as on a drive of its own (drive {drive_id})")
        return 0


def cmd_drives_assign(args) -> int:
    with Catalog(args.catalog) as catalog:
        try:
            catalog.assign_volume_drive(args.volume_id, args.drive_id)
        except ValueError as exc:
            print(f"drivefusion: {exc}", file=sys.stderr)
            return 1
        print(f"volume {args.volume_id} recorded as on drive {args.drive_id}")
        return 0


def cmd_audit_list(args) -> int:
    from drivefusion.core.store import audit

    with Catalog(args.catalog) as catalog:
        rows = audit.entries(catalog.conn, limit=args.limit)
        if not rows:
            print("the audit log is empty")
            return 0
        for row in rows:
            print(f"[{row['id']}] {row['at']}  {row['action']:18} {row['actor']}")
            if row["detail_json"] and row["detail_json"] != "{}":
                print(f"      {row['detail_json']}")
        return 0


def cmd_audit_verify(args) -> int:
    from drivefusion.core.store import audit

    with Catalog(args.catalog) as catalog:
        result = audit.verify(catalog.conn)
    if result.ok:
        print(f"audit log intact: {result.entries:,} entries")
        if result.head:
            # The chain can't vouch for entries cut off the end; a head hash
            # noted elsewhere can.
            print(f"  head: {result.head.hex()}")
        return 0
    print(f"audit log BROKEN ({result.entries:,} entries):", file=sys.stderr)
    for problem in result.problems[:50]:
        print(f"  {problem}", file=sys.stderr)
    return 1


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
    ("drives", "list"): cmd_drives_list,
    ("drives", "confirm"): cmd_drives_confirm,
    ("drives", "assign"): cmd_drives_assign,
    ("audit", "list"): cmd_audit_list,
    ("audit", "verify"): cmd_audit_verify,
    ("scan", None): cmd_scan,
    ("find", None): cmd_find,
    ("hash", None): cmd_hash,
    ("report", None): cmd_report,
    ("verify", None): cmd_verify,
    ("gui", None): cmd_gui,
    ("status", None): cmd_status,
    ("volumes", None): cmd_volumes,
}


def main(argv: list[str] | None = None) -> int:
    from drivefusion.core.store import audit

    parser = build_parser()
    args = parser.parse_args(argv)
    audit.set_front_end("cli")

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

    action = (getattr(args, "scope_command", None) or getattr(args, "drives_command", None)
              or getattr(args, "audit_command", None))
    if args.command in ("scope", "drives", "audit") and not action:
        parser.parse_args([args.command, "--help"])
        return 0

    handler = DISPATCH.get((args.command, action))
    if handler is None:
        parser.print_help()
        return 2

    try:
        if (args.command, action) in READS_DRIVES and not _disclaimer_gate(args):
            return 2
        return handler(args)
    except CatalogLocationError as exc:
        print(f"drivefusion: {exc}", file=sys.stderr)
        return 1
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
