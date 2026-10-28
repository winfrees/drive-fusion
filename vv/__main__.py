"""``python -m vv`` — generate datasets, run Drive Fusion against them, judge the result.

Exit status: 0 when every applicable test passes, 1 when any fails or errors,
2 when the bench itself could not do what was asked.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from pathlib import Path

from vv import evaluate as evaluate_mod
from vv import fsops, generator, oracle, report, runner


def _drive_paths(values: list[str]) -> dict[str, str]:
    """``D01=E:\\VV`` or bare ``E:\\VV`` (numbered in order)."""
    out: dict[str, str] = {}
    for i, value in enumerate(values, start=1):
        drive_id, sep, path = value.partition("=")
        if sep and drive_id.upper().startswith("D") and drive_id[1:].isdigit():
            out[f"D{int(drive_id[1:]):02d}"] = path
        else:
            out[f"D{i:02d}"] = value
    return out


def _identify(path: str):
    """Drive Fusion's own view of a folder's volume, or None if unavailable."""
    try:
        from drivefusion.core.discovery import identify_volume

        return identify_volume(path)
    except Exception:  # noqa: BLE001 - best effort; the run checks again
        return None


def _check_distinct_devices(roots: dict[str, str]) -> None:
    """Refuse before writing anything, not after, if two drives share a device.

    Two partitions of one disk, or two folders on one thumb drive, are one
    copy as far as durability is concerned, and Drive Fusion is right to say
    so — which would make every cross-drive expectation wrong.
    """
    seen: dict[str, str] = {}
    for drive_id, root in roots.items():
        probe = root
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        volume = _identify(probe)
        if volume is None:
            return
        device = volume.drive.serial if volume.drive and volume.drive.serial else volume.volume_guid
        if device in seen:
            raise fsops.DatasetError(
                f"{drive_id} ({root}) is on the same physical device as {seen[device]} "
                f"({device}). Give each drive a folder on a different device, or leave "
                "out --drive-path to simulate drives on one disk.")
        seen[device] = drive_id


def cmd_generate(args) -> Path:
    import random

    out = Path(args.out).resolve()
    fsops.ensure_empty_target(str(out))
    seed = args.seed if args.seed is not None else secrets.randbits(48)
    rng = random.Random(f"{seed}/drives")

    if args.drive_path:
        mode = "physical"
        paths = _drive_paths(args.drive_path)
        roots = {d: os.path.abspath(p) for d, p in sorted(paths.items())}
        _check_distinct_devices(roots)
    else:
        mode = "simulated"
        if args.drives < 1:
            raise fsops.DatasetError("--drives must be at least 1")
        roots = {f"D{i:02d}": str(out / "drives" / f"D{i:02d}") for i in range(1, args.drives + 1)}

    for root in roots.values():
        fsops.ensure_empty_target(root)
    if len(set(map(os.path.normcase, roots.values()))) != len(roots):
        raise fsops.DatasetError("two drives were given the same path")

    out.mkdir(parents=True, exist_ok=True)
    for root in roots.values():
        os.makedirs(root, exist_ok=True)

    fs_mix = ["NTFS", "exFAT"] * len(roots)
    rng.shuffle(fs_mix)
    drives = []
    for index, (drive_id, root) in enumerate(roots.items(), start=1):
        caps = fsops.probe_capabilities(root)
        if mode == "physical":
            volume = _identify(root)
            fs = (volume.fs_type if volume else None) or "unknown"
        else:
            fs = fs_mix[index - 1]
        if mode == "simulated" and fs != "NTFS":
            # exFAT has neither; keep the simulation as honest as a real drive.
            caps["hardlinks"] = caps["symlinks"] = False
        identity = generator.mock_drive_identity(rng, index, fs)
        drives.append({**identity, "id": drive_id, "root": root, "caps": caps})

    params = {"files_per_drive": args.files_per_drive, "large_files": args.large_files}
    manifest = generator.build(seed, drives, files_per_drive=args.files_per_drive,
                               large_files=args.large_files,
                               unscoped_root=str(out / "unscoped"), mode=mode, params=params)
    runner.save_manifest(out, manifest)
    print(f"seed {seed}: {len(manifest['files']):,} files, {len(manifest['dirs']):,} folders, "
          f"{len(manifest['scenarios'])} scenario instances across {len(drives)} {mode} drive(s)")
    fsops.materialize(manifest)
    exp = oracle.expectations(manifest)
    (out / "expected.json").write_text(json.dumps(oracle.summary_json(exp), indent=1))
    runner.write_state(out, "pristine")
    for d in drives:
        missing = [k for k, v in d["caps"].items() if not v]
        note = f" (cannot hold: {', '.join(missing)})" if missing else ""
        print(f"  {d['id']} {d['fs']:6} {d['root']}{note}")
    print(f"dataset: {out}")
    return out


def _evaluate_run(dataset: Path, run_dir: Path, observed_path: Path | None = None) -> int:
    manifest = runner.load_manifest(dataset)
    observed = json.loads((observed_path or run_dir / "observed.json").read_text(encoding="utf-8"))
    results = evaluate_mod.evaluate(manifest, observed)
    path = report.write(run_dir, manifest, observed, results)
    verdict = report.overall(results)
    for r in results:
        if r["verdict"] in (evaluate_mod.FAIL, evaluate_mod.ERROR):
            print(f"  {r['verdict']:5} {r['id']}@{r['phase']}: {r['title']}")
            for d in r["details"][:3]:
                print(f"        {d}")
    counts = {v: sum(r["verdict"] == v for r in results)
              for v in ("PASS", "FAIL", "ERROR", "N/A", "DEFERRED")}
    print(f"verdict: {verdict} — " + ", ".join(f"{n} {v}" for v, n in counts.items() if n))
    print(f"report: {path}")
    return 0 if verdict == "PASS" else 1


def cmd_run(args, dataset: Path | None = None) -> int:
    dataset = dataset or Path(args.dataset)
    run_dir = runner.run(dataset, src=args.drivefusion_src,
                         overrides=_drive_paths(args.drive_path) if args.drive_path else None,
                         min_copies=args.min_copies)
    return _evaluate_run(dataset.resolve(), run_dir)


def cmd_evaluate(args) -> int:
    dataset = Path(args.dataset).resolve()
    if args.observed:
        observed = Path(args.observed).resolve()
        return _evaluate_run(dataset, observed.parent, observed)
    runs = sorted((dataset / "runs").glob("*/observed.json"))
    if not runs:
        raise fsops.DatasetError(f"no runs under {dataset / 'runs'}")
    return _evaluate_run(dataset, runs[-1].parent)


def cmd_reset(args) -> int:
    dataset = Path(args.dataset).resolve()
    manifest = runner.load_manifest(dataset)
    if args.drive_path:
        runner.apply_drive_overrides(manifest, _drive_paths(args.drive_path))
        runner.save_manifest(dataset, manifest)
    fsops.wipe(manifest)
    for d in manifest["drives"]:
        os.makedirs(d["root"], exist_ok=True)
    fsops.materialize(manifest)
    runner.write_state(dataset, "pristine")
    print(f"restored {dataset} to its generated state (seed {manifest['seed']})")
    return 0


def cmd_clean(args) -> int:
    dataset = Path(args.dataset).resolve()
    manifest = runner.load_manifest(dataset)
    fsops.wipe(manifest)
    shutil.rmtree(dataset)
    print(f"removed {dataset} and its drive folders")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vv", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def gen_args(sp):
        sp.add_argument("--out", required=True, help="new or empty folder for the dataset")
        sp.add_argument("--drives", type=int, default=4,
                        help="number of simulated drives (default 4)")
        sp.add_argument("--drive-path", action="append", default=[], metavar="[Dnn=]PATH",
                        help="physical mode: an empty folder on a real device; repeat once per "
                             "drive, e.g. --drive-path D01=E:\\DriveFusionVV")
        sp.add_argument("--seed", type=int, default=None,
                        help="reproduce a dataset; default is a fresh random seed")
        sp.add_argument("--files-per-drive", type=int, default=120,
                        help="filler files per drive, ±20%% (default 120)")
        sp.add_argument("--large-files", type=int, default=3,
                        help="multi-megabyte contents to copy across drives (default 3)")

    def run_args(sp, with_dataset=True):
        if with_dataset:
            sp.add_argument("dataset")
            sp.add_argument("--drive-path", action="append", default=[], metavar="Dnn=PATH",
                            help="override a drive's location, e.g. after a letter change")
        sp.add_argument("--drivefusion-src", metavar="DIR",
                        help="checkout of the Drive Fusion version to test (default: installed)")
        sp.add_argument("--min-copies", type=int, default=2,
                        help="copy policy for the under-protected tests (default 2)")

    gen_args(sub.add_parser("generate", help="build a randomized dataset"))
    run_args(sub.add_parser("run", help="run Drive Fusion against a dataset and judge it"))
    all_p = sub.add_parser("all", help="generate, then run")
    gen_args(all_p)
    run_args(all_p, with_dataset=False)
    ev = sub.add_parser("evaluate", help="re-judge the latest run, or an observed.json")
    ev.add_argument("dataset")
    ev.add_argument("--observed", help="observed.json from any adapter")
    rs = sub.add_parser("reset", help="restore a used dataset to its generated state")
    rs.add_argument("dataset")
    rs.add_argument("--drive-path", action="append", default=[], metavar="Dnn=PATH")
    cl = sub.add_parser("clean", help="delete a dataset and its drive folders")
    cl.add_argument("dataset")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            cmd_generate(args)
            return 0
        if args.command == "all":
            return cmd_run(args, cmd_generate(args))
        return {"run": cmd_run, "evaluate": cmd_evaluate, "reset": cmd_reset,
                "clean": cmd_clean}[args.command](args)
    except (fsops.DatasetError, OSError) as exc:
        print(f"vv: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
