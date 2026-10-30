#!/usr/bin/env python3
"""Turn a Backblaze Drive Stats download into an AFR table file (ROADMAP M5.4).

    python tools/import_backblaze.py --version 2025Q2 data_Q2_2025.zip

writes ``rates-backblaze-2025Q2.json`` (default: into
``drivefusion/standards/afr/``). The arithmetic is
``drivefusion/core/health/backblaze.py``, which the app's own import uses too;
the build runs this through ``tools/fetch_backblaze.py`` on a pinned snapshot.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drivefusion.core.health import backblaze  # noqa: E402

OUT_DIR = ROOT / "drivefusion" / "standards" / "afr"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", type=Path, help="Drive Stats ZIPs or CSVs")
    parser.add_argument("--version", help="e.g. 2025Q2 (default: from the file name)")
    parser.add_argument("--min-drive-days", type=int, default=backblaze.MIN_DRIVE_DAYS,
                        help="leave out models with less exposure (default: 100 drive-years)")
    parser.add_argument("--retrieved", default=dt.date.today().isoformat())
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    version = args.version or backblaze.version_from_name(args.inputs[0].name)
    if not version:
        parser.error("can't tell the quarter from the file name; pass --version")
    table = backblaze.import_files(args.inputs, version=version, retrieved=args.retrieved,
                                   min_drive_days=args.min_drive_days,
                                   on_file=lambda name: print(f"  {name}", file=sys.stderr))
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"rates-backblaze-{version}.json"
    target.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"{target}: {len(table['models'])} models, fleet AFR {table['classes']['hdd']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
