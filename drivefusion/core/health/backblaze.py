"""Backblaze Drive Stats to an AFR table (ROADMAP M5.4, D6, D13).

Backblaze publishes one CSV row per drive per day, with ``failure`` = 1 on the
day a drive failed (https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data).
A model's annualised failure rate is then

    AFR = failures / (drive_days / 365)

Drive Fusion never downloads anything (PLAN §4, network rule). A table comes
either from the build, which pins a snapshot by SHA-256
(``tools/fetch_backblaze.py``), or from a ZIP the user downloaded and imports
from Settings or ``drivefusion standards import-afr``. The input is only read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Callable

SOURCE_URL = "https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data"

#: Leave out models with less exposure than this: a handful of drive-days
#: makes a rate of noise. 100 drive-years.
MIN_DRIVE_DAYS = 36_500


class DriveStatsError(ValueError):
    """The input isn't Backblaze Drive Stats."""


def version_from_name(name: str) -> str | None:
    """``data_Q2_2025.zip`` → ``2025Q2``; ``data_2024.zip`` → ``2024``."""
    match = re.search(r"Q([1-4])[_-]?((?:19|20)\d\d)", name, re.I)
    if match:
        return f"{match.group(2)}Q{match.group(1)}"
    match = re.search(r"((?:19|20)\d\d)", name)
    return match.group(1) if match else None


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def _csv_members(path: Path):
    """``(name, text stream)`` for each CSV in ``path`` (a ZIP or one CSV)."""
    with open(path, "rb") as raw:
        if zipfile.is_zipfile(raw):
            raw.seek(0)
            with zipfile.ZipFile(raw) as archive:
                names = sorted(n for n in archive.namelist()
                               if n.lower().endswith(".csv") and "__MACOSX" not in n)
                for name in names:
                    with archive.open(name) as member:
                        yield name, io.TextIOWrapper(member, encoding="utf-8", newline="")
        else:
            raw.seek(0)
            yield path.name, io.TextIOWrapper(raw, encoding="utf-8", newline="")


def tally(paths: list[Path], *, on_file: Callable[[str], None] | None = None
          ) -> tuple[dict[str, int], dict[str, int]]:
    """Drive-days and failures per model. ``on_file`` is called before each
    CSV, so a caller can show progress or raise to cancel."""
    days: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    for path in paths:
        for name, stream in _csv_members(path):
            if on_file:
                on_file(name)
            reader = csv.reader(stream)
            header = next(reader, None) or []
            try:
                model_at, failure_at = header.index("model"), header.index("failure")
            except ValueError:
                raise DriveStatsError(f"{name} has no model and failure columns: "
                                   "is it Backblaze Drive Stats?") from None
            for row in reader:
                if len(row) <= max(model_at, failure_at):
                    continue
                model = row[model_at].strip()
                if model:
                    days[model] += 1
                    if row[failure_at].strip() == "1":
                        failures[model] += 1
    return dict(days), dict(failures)


def build(days: dict[str, int], failures: dict[str, int], *, version: str,
          inputs: dict[str, str], retrieved: str,
          min_drive_days: int = MIN_DRIVE_DAYS) -> dict:
    total_days, total_failures = sum(days.values()), sum(failures.values())
    if not total_days:
        raise DriveStatsError("no drive-days found: are these Backblaze Drive Stats?")
    models = [
        {"model": name, "drive_days": days[name], "failures": failures.get(name, 0),
         "afr": round(failures.get(name, 0) / (days[name] / 365), 6)}
        for name in sorted(days) if days[name] >= min_drive_days
    ]
    return {
        "kind": "afr-rates", "source": "backblaze", "version": version, "basis": "fleet",
        "title": f"Backblaze Drive Stats {version}", "url": SOURCE_URL, "retrieved": retrieved,
        "note": (f"AFR = failures / (drive_days / 365) per model, models with at least "
                 f"{min_drive_days} drive-days. The hdd class rate is the whole fleet's."),
        "inputs": inputs,
        "classes": {"hdd": round(total_failures / (total_days / 365), 6)},
        "models": models,
    }


def import_files(paths: list[Path], *, version: str, retrieved: str,
                 min_drive_days: int = MIN_DRIVE_DAYS,
                 on_file: Callable[[str], None] | None = None) -> dict:
    """Read Drive Stats ZIPs or CSVs and return the AFR table."""
    days, failures = tally(paths, on_file=on_file)
    return build(days, failures, version=version, min_drive_days=min_drive_days,
                 inputs={p.name: _sha256(p) for p in paths}, retrieved=retrieved)
