"""Command-line entry point.

Every verb is read-only or produces a document. There is deliberately no
``apply``, ``move``, ``dedupe``, or ``delete`` command, and there never will
be: consolidation plans are documents you execute with your own tools
(docs/PLAN.md §3.3).
"""

from __future__ import annotations

import argparse
import sys

from drivefusion import __version__

BANNER = "Drive Fusion — read-only cataloging and consolidation planning"

#: Verb -> (help text, milestone that implements it).
VERBS = {
    "scope": ("manage which drives and roots are catalogued", "M1"),
    "scan": ("enumerate a volume in scope and update the catalog", "M1"),
    "find": ("search the catalog", "M1"),
    "report": ("duplicate, redundancy, and integrity reports", "M3"),
    "score": ("FAIR and NDSA scorecards", "M6"),
    "plan": ("build a consolidation plan", "M7"),
    "export": ("write reports, manifests, and bundles to an export folder", "M8"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drivefusion", description=BANNER)
    parser.add_argument(
        "--version", action="version", version=f"drivefusion {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (help_text, milestone) in VERBS.items():
        sub = subparsers.add_parser(name, help=f"{help_text} [{milestone}]")
        sub.set_defaults(milestone=milestone)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    print(
        f"drivefusion {args.command}: not implemented yet — scheduled for "
        f"{args.milestone}. See docs/PLAN.md §15.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
