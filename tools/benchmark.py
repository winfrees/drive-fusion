#!/usr/bin/env python3
"""The M1 benchmark gate: measure the catalog against docs/PLAN.md §13.

The plan commits to specific numbers — ≤250 bytes per file, under 2 GB of RSS
at any catalog size, reports over 50M rows in under five minutes — and says
they are acceptance criteria rather than aspirations. This measures them, and
decides the single-database versus sharded question with data instead of
expectation.

    python tools/benchmark.py --files 1000000

Rows are generated directly through the loader rather than from a real
filesystem, so the measurement isolates catalog cost from disk enumeration.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivefusion.core.store.catalog import Catalog  # noqa: E402

#: docs/PLAN.md §13.
BUDGET_BYTES_PER_FILE = 250
BUDGET_PEAK_RSS_BYTES = 2 * 1024**3
BUDGET_REPORT_SECONDS_AT_50M = 300

#: Shape of a realistic archive: directories are a small fraction of entries,
#: which is what makes path interning worth its complexity.
FILES_PER_DIR = 20
DIRS_PER_LEVEL = 12


def peak_rss_bytes() -> int | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return int(counters.PeakWorkingSetSize)
        return None

    try:
        with open("/proc/self/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def human(value: float, unit: str = "B") -> str:
    if unit == "B":
        size = float(value)
        for suffix in ("B", "KB", "MB", "GB", "TB"):
            if abs(size) < 1024 or suffix == "TB":
                return f"{size:,.1f} {suffix}"
            size /= 1024
    return f"{value:,.1f} {unit}"


def build_tree(catalog: Catalog, volume_id: int, root_id: int, scan_id: int,
               file_count: int) -> tuple[int, int]:
    """Intern enough directories to hold ``file_count`` files."""
    root = catalog.intern_dir(
        volume_id=volume_id, root_id=root_id, parent_id=None,
        name="/bench", depth=0, scan_id=scan_id,
    )
    needed = max(1, file_count // FILES_PER_DIR)
    dirs = [root]
    frontier = [root]
    depth = 1
    while len(dirs) < needed:
        next_frontier = []
        for parent in frontier:
            for index in range(DIRS_PER_LEVEL):
                if len(dirs) >= needed:
                    break
                dir_id = catalog.intern_dir(
                    volume_id=volume_id, root_id=root_id, parent_id=parent,
                    name=f"d{depth:02d}-{index:03d}", depth=depth,
                    scan_id=scan_id,
                )
                dirs.append(dir_id)
                next_frontier.append(dir_id)
            if len(dirs) >= needed:
                break
        frontier = next_frontier or [root]
        depth += 1
    return dirs, depth


def generate_rows(scan_id: int, volume_id: int, dirs: list[int], file_count: int):
    per_dir = max(1, file_count // len(dirs))
    emitted = 0
    for dir_id in dirs:
        for index in range(per_dir):
            if emitted >= file_count:
                return
            emitted += 1
            yield (
                scan_id, volume_id, dir_id,
                f"file-{emitted:09d}.dat", "dat",
                (emitted * 7919) % 5_000_000, None,
                1_700_000_000_000_000_000 + emitted, None,
                emitted, 1, 0, "ok",
            )
    while emitted < file_count:
        emitted += 1
        yield (
            scan_id, volume_id, dirs[-1], f"extra-{emitted:09d}.dat", "dat",
            emitted, None, 1_700_000_000_000_000_000 + emitted, None,
            emitted, 1, 0, "ok",
        )


def run(file_count: int, catalog_dir: Path | None) -> dict:
    workdir = catalog_dir or Path(tempfile.mkdtemp(prefix="df-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    catalog_path = workdir / "benchmark.db"
    for stale in workdir.glob("benchmark.db*"):
        os.unlink(stale)

    timings: dict[str, float] = {}
    with Catalog(catalog_path) as catalog:
        volume_id = catalog.upsert_volume(volume_guid="bench:1", fs_type="ntfs")
        root_id = catalog.add_scope_root(volume_id, "/bench")
        scan_id = catalog.begin_scan(volume_id, root_id, "bench", False)

        start = time.perf_counter()
        catalog.conn.execute("BEGIN")
        dirs, depth = build_tree(catalog, volume_id, root_id, scan_id, file_count)
        catalog.conn.execute("COMMIT")
        timings["intern_dirs"] = time.perf_counter() - start

        start = time.perf_counter()
        catalog.conn.execute("BEGIN")
        catalog.stage_files(generate_rows(scan_id, volume_id, dirs, file_count))
        catalog.conn.execute("COMMIT")
        timings["stage"] = time.perf_counter() - start

        start = time.perf_counter()
        catalog.merge_scan(scan_id, volume_id, root_id)
        timings["merge"] = time.perf_counter() - start

        start = time.perf_counter()
        catalog.rebuild_rollups(volume_id)
        timings["rollups"] = time.perf_counter() - start

        start = time.perf_counter()
        catalog.analyze()
        timings["analyze"] = time.perf_counter() - start

        # Queries a user actually waits on.
        start = time.perf_counter()
        list(catalog.find("%file-000000123%", limit=50))
        timings["find_by_name"] = time.perf_counter() - start

        start = time.perf_counter()
        list(catalog.find("%", limit=200))
        timings["first_page"] = time.perf_counter() - start

        start = time.perf_counter()
        counts = catalog.counts()
        timings["counts"] = time.perf_counter() - start

        # The shape M3's duplicate detection depends on: group by size.
        start = time.perf_counter()
        catalog.conn.execute(
            "SELECT size_bytes, COUNT(*) AS n FROM file "
            "WHERE vanished_at IS NULL GROUP BY size_bytes HAVING n > 1 "
            "ORDER BY n DESC LIMIT 20"
        ).fetchall()
        timings["duplicate_scan"] = time.perf_counter() - start

    catalog_bytes = sum(
        p.stat().st_size for p in workdir.glob("benchmark.db*") if p.is_file()
    )
    return {
        "files": counts["files"],
        "dirs": counts["dirs"],
        "depth": depth,
        "timings": timings,
        "catalog_bytes": catalog_bytes,
        "bytes_per_file": catalog_bytes / max(1, counts["files"]),
        "peak_rss": peak_rss_bytes(),
        "path": catalog_path,
    }


def report(result: dict, file_count: int) -> int:
    print(f"\nCatalog benchmark — {result['files']:,} files, "
          f"{result['dirs']:,} directories, depth {result['depth']}")
    print("-" * 68)
    for name, seconds in result["timings"].items():
        rate = ""
        if name in {"stage", "merge"} and seconds > 0:
            rate = f"  ({result['files'] / seconds:,.0f} rows/s)"
        print(f"  {name:<16} {seconds:8.3f}s{rate}")

    print("-" * 68)
    print(f"  catalog size     {human(result['catalog_bytes'])}")
    print(f"  bytes per file   {result['bytes_per_file']:.1f}")
    if result["peak_rss"]:
        print(f"  peak RSS         {human(result['peak_rss'])}")

    projected = result["bytes_per_file"] * 50_000_000
    print(f"\n  projected at 50M files: {human(projected)}")

    print("\nBudgets (docs/PLAN.md §13)")
    print("-" * 68)
    failures = 0

    ok = result["bytes_per_file"] <= BUDGET_BYTES_PER_FILE
    failures += not ok
    print(
        f"  [{'PASS' if ok else 'FAIL'}] bytes/file "
        f"{result['bytes_per_file']:.1f} <= {BUDGET_BYTES_PER_FILE}"
    )

    if result["peak_rss"]:
        ok = result["peak_rss"] < BUDGET_PEAK_RSS_BYTES
        failures += not ok
        print(
            f"  [{'PASS' if ok else 'FAIL'}] peak RSS "
            f"{human(result['peak_rss'])} < {human(BUDGET_PEAK_RSS_BYTES)}"
        )

    scaled = result["timings"]["duplicate_scan"] * (50_000_000 / max(1, file_count))
    ok = scaled < BUDGET_REPORT_SECONDS_AT_50M
    failures += not ok
    print(
        f"  [{'PASS' if ok else 'FAIL'}] duplicate scan projected to 50M "
        f"{scaled:.1f}s < {BUDGET_REPORT_SECONDS_AT_50M}s"
    )

    print(
        "\nNote: projections assume linear scaling, which index depth makes "
        "mildly optimistic.\nRun with --files 50000000 before treating the "
        "single-database decision as settled."
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=200_000)
    parser.add_argument("--catalog-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    result = run(args.files, args.catalog_dir)
    status = report(result, args.files)
    print(f"\ntotal wall clock: {time.perf_counter() - started:.1f}s")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
