"""Run one Drive Fusion version through a dataset: scan, change, rescan, corrupt, verify.

The sequence, with the no-touch snapshot taken around every tool phase:

1. snapshot → **baseline**: register drives, scan, hash, record → snapshot
2. the bench applies the manifest's mutations (adds, edits, deletes, a move,
   a deleted folder, a new folder)
3. snapshot → **rescan**: scan, hash, record → snapshot
4. the bench silently corrupts the chosen files (bytes flipped, mtime kept)
5. snapshot → **verify**: fixity pass, record → snapshot

A phase that raises is recorded with its traceback and the run continues
where it can, so one crash shows up as ERROR on the tests it affects rather
than as no report at all.

A dataset is single-use: steps 2 and 4 change it. ``state.json`` records that,
and ``python -m vv reset`` rebuilds it from its manifest.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import traceback
from pathlib import Path

from vv import __version__, fsops
from vv.adapter import ADAPTER_NAME, DriveFusionAdapter

OBSERVED_SCHEMA = "drivefusion-vv-observed/1"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_manifest(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))


def save_manifest(dataset: Path, manifest: dict) -> None:
    (dataset / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False),
                                           encoding="utf-8")


def read_state(dataset: Path) -> str:
    try:
        return json.loads((dataset / "state.json").read_text())["state"]
    except FileNotFoundError:
        return "pristine"


def write_state(dataset: Path, state: str) -> None:
    (dataset / "state.json").write_text(json.dumps({"state": state, "at": _now()}))


def apply_drive_overrides(manifest: dict, overrides: dict[str, str]) -> None:
    """Point drives at new paths — a USB drive that came back as another letter."""
    known = {d["id"] for d in manifest["drives"]}
    for drive_id, path in overrides.items():
        if drive_id not in known:
            raise fsops.DatasetError(f"no drive {drive_id} in this dataset ({', '.join(sorted(known))})")
        next(d for d in manifest["drives"] if d["id"] == drive_id)["root"] = os.path.abspath(path)


def check_pristine(manifest: dict) -> None:
    """The bench checks its own instrument before trusting it."""
    missing = []
    for d in manifest["drives"]:
        if not os.path.isdir(d["root"]):
            raise fsops.DatasetError(
                f"{d['id']} root {d['root']} is not available. Connect the drive, or pass "
                f"--drive-path {d['id']}=<new path> if its letter or mount point changed.")
    roots = {d["id"]: d["root"] for d in manifest["drives"]}
    sizes = manifest["contents"]
    for f in manifest["files"]:
        path = fsops.os_path(roots[f["drive"]], f["path"])
        try:
            if os.stat(path).st_size != sizes[f["content"]]["size"]:
                missing.append(f"wrong size: {f['drive']}:{f['path']}")
        except FileNotFoundError:
            missing.append(f"missing: {f['drive']}:{f['path']}")
        if len(missing) >= 5:
            break
    if missing:
        raise fsops.DatasetError("dataset does not match its manifest:\n  " + "\n  ".join(missing)
                                 + "\nRebuild it with `python -m vv reset`.")


def run(dataset: Path, *, src: str | None = None, overrides: dict[str, str] | None = None,
        min_copies: int = 2, log=print) -> Path:
    dataset = dataset.resolve()
    manifest = load_manifest(dataset)
    if overrides:
        apply_drive_overrides(manifest, overrides)
        save_manifest(dataset, manifest)
    if read_state(dataset) != "pristine":
        raise fsops.DatasetError(
            f"{dataset} has already been run: its files were mutated and corrupted on "
            "purpose. Restore it with `python -m vv reset`, or generate a new dataset.")
    check_pristine(manifest)

    run_dir = dataset / "runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    roots = [d["root"] for d in manifest["drives"]] + [manifest["unscoped_root"]]
    adapter = DriveFusionAdapter(str(run_dir / "catalog.db"), src=src)
    log(f"drive fusion {adapter.version} from {adapter.source}")

    observed: dict = {
        "schema": OBSERVED_SCHEMA, "bench": __version__, "adapter": ADAPTER_NAME,
        "tool": {"name": "drivefusion", "version": adapter.version, "source": adapter.source},
        "dataset": {"seed": manifest["seed"], "mode": manifest["mode"],
                    "drives": len(manifest["drives"])},
        "min_copies": min_copies, "started_at": _now(),
        "phases": {}, "errors": {}, "no_touch": {}, "snapshot_entries": {},
    }
    write_state(dataset, "in-use")
    changed = False

    def tool_phase(name: str, body) -> None:
        before = fsops.snapshot(roots)
        try:
            observed["phases"][name] = body()
        except Exception:  # noqa: BLE001 - recorded, and reported as ERROR
            observed["errors"][name] = traceback.format_exc()
            log(f"  {name}: ERROR\n{observed['errors'][name]}")
        after = fsops.snapshot(roots)
        observed["no_touch"][name] = fsops.snapshot_diff(before, after)
        observed["snapshot_entries"][name] = len(before)

    try:
        def baseline() -> dict:
            adapter.register(manifest)
            scan = adapter.scan()
            hashed = adapter.hash()
            return {**adapter.collect(min_copies), "scan": scan, "hash": hashed}

        log("baseline: scan + hash ...")
        tool_phase("baseline", baseline)

        def rescan() -> dict:
            scan = adapter.scan()
            hashed = adapter.hash()
            return {**adapter.collect(min_copies), "scan": scan, "hash": hashed}

        # A baseline that never ran leaves nothing to compare a rescan with,
        # and stopping here keeps the dataset unspent for the next attempt.
        if "baseline" not in observed["errors"]:
            log("applying between-scan changes ...")
            changed = True
            fsops.apply_mutations(manifest)
            log("rescan: scan + hash ...")
            tool_phase("rescan", rescan)

            log("corrupting files silently ...")
            fsops.apply_corruptions(manifest)
            fsops.apply_late_edits(manifest)
            if not observed["errors"]:
                log("verify: fixity pass ...")
                tool_phase("verify", adapter.verify)
    finally:
        adapter.close()
        untouched = not any(observed["no_touch"].values())
        write_state(dataset, "consumed" if changed or not untouched else "pristine")
        observed["finished_at"] = _now()
        (run_dir / "observed.json").write_text(json.dumps(observed, indent=1, ensure_ascii=False),
                                               encoding="utf-8")
    return run_dir
