#!/usr/bin/env python3
"""Turn a Backblaze Drive Stats snapshot into a pinned AFR table (ROADMAP M5.4, D6).

Backblaze publishes one CSV row per drive per day, with ``failure`` = 1 on the
day a drive failed (https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data).
A model's annualised failure rate is then

    AFR = failures / (drive_days / 365)

Download a quarter's (or year's) ZIP, then:

    python tools/import_backblaze.py --version 2025Q2 data_Q2_2025.zip

writes ``drivefusion/standards/afr/rates-backblaze-2025Q2.json``, recording the
SHA-256 of every input so the table can be traced to the files it came from.
Models with too little exposure (``--min-drive-days``) are left out, since a
handful of drive-days makes a rate of noise. The fleet is hard drives, so the
whole-fleet rate becomes the ``hdd`` class rate.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "drivefusion" / "standards" / "afr"
SOURCE_URL = "https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data"


def _csv_streams(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if name.lower().endswith(".csv") and "__MACOSX" not in name:
                    with archive.open(name) as raw:
                        yield io.TextIOWrapper(raw, encoding="utf-8", newline="")
    else:
        with open(path, encoding="utf-8", newline="") as handle:
            yield handle


def tally(paths: list[Path]) -> tuple[dict[str, int], dict[str, int]]:
    """drive-days and failures per model."""
    days: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    for path in paths:
        for stream in _csv_streams(path):
            for row in csv.DictReader(stream):
                model = (row.get("model") or "").strip()
                if not model:
                    continue
                days[model] += 1
                if (row.get("failure") or "0").strip() == "1":
                    failures[model] += 1
    return dict(days), dict(failures)


def build(days: dict[str, int], failures: dict[str, int], *, version: str,
          min_drive_days: int, inputs: dict[str, str], retrieved: str) -> dict:
    models = []
    for name in sorted(days):
        if days[name] >= min_drive_days:
            models.append({
                "model": name, "drive_days": days[name], "failures": failures.get(name, 0),
                "afr": round(failures.get(name, 0) / (days[name] / 365), 6),
            })
    total_days, total_failures = sum(days.values()), sum(failures.values())
    if not total_days:
        raise SystemExit("no drive-days found: are these Backblaze Drive Stats CSVs?")
    fleet = round(total_failures / (total_days / 365), 6)
    return {
        "kind": "afr-rates", "source": "backblaze", "version": version, "basis": "fleet",
        "title": f"Backblaze Drive Stats {version}", "url": SOURCE_URL, "retrieved": retrieved,
        "note": (f"AFR = failures / (drive_days / 365) per model, models with at least "
                 f"{min_drive_days} drive-days. The hdd class rate is the whole fleet's."),
        "inputs": inputs,
        "classes": {"hdd": fleet},
        "models": models,
    }


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", type=Path, help="Drive Stats ZIPs or CSVs")
    parser.add_argument("--version", required=True, help="e.g. 2025Q2")
    parser.add_argument("--min-drive-days", type=int, default=36_500,
                        help="leave out models with less exposure (default: 100 drive-years)")
    parser.add_argument("--retrieved", default=dt.date.today().isoformat())
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    days, failures = tally(args.inputs)
    table = build(days, failures, version=args.version, min_drive_days=args.min_drive_days,
                  inputs={p.name: _sha256(p) for p in args.inputs}, retrieved=args.retrieved)
    target = args.out / f"rates-backblaze-{args.version}.json"
    target.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"{target}: {len(table['models'])} models, fleet AFR {table['classes']['hdd']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
