"""Campaigns: N drives, chosen suites, repeated runs, one report.

A campaign repeats generate → run → evaluate a number of times, each time on
a fresh, differently seeded dataset over the same drives, judging only the
chosen suites (vv/suites.py), and collects every run under one folder:

    <campaign>/
      campaign.json     what was asked: drives, suites, iterations, versions
      summary.json      every run's verdicts, by requirement and by suite
      dashboard.html    the same on one self-contained page
      junit.xml         every run's tests, one <testsuite> per run
      runs/001/         report.md, results.json, observed.json, junit.xml,
                        dataset.json (seed and drives) and log.txt
      runs/002/ …

Drives are simulated (folders under the campaign, registered as separate
drives) or real: one folder per physical device. On a real drive each run's
dataset goes in ``<folder>/DriveFusion-VV/<campaign>/<run>`` and is removed
after the run unless ``keep`` is set; removal goes through ``fsops.wipe``,
which refuses to delete anything the bench did not create.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import platform
import shutil
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from vv import __version__, fsops, runner
from vv import suites as suites_mod

FILES = ("report.md", "results.json", "observed.json", "junit.xml")


@dataclass
class Config:
    out: str                                   # a new or empty folder
    suites: list[str] = field(default_factory=lambda: ["all"])
    iterations: int = 1
    drives: int = 3                            # simulated drives, when no paths
    drive_paths: list[str] = field(default_factory=list)   # real drives: one folder each
    drive_size: str | None = None              # simulated only, e.g. "4GB"
    files_per_drive: int = 120
    large_files: int = 3
    first_seed: int | None = None              # seeds are first_seed, +1, +2 … when set
    min_copies: int = 2
    keep: bool = False                         # keep each run's dataset afterwards
    drivefusion_src: str | None = None

    @property
    def mode(self) -> str:
        return "physical" if self.drive_paths else "simulated"


class Cancelled(Exception):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _generate(config: Config, dataset: Path, index: int, campaign_id: str) -> Path:
    from vv.__main__ import cmd_generate

    args = argparse.Namespace(
        out=str(dataset), drives=config.drives, drive_path=[], files_per_drive=config.files_per_drive,
        large_files=config.large_files, drive_size=[config.drive_size] if config.drive_size else [],
        seed=None if config.first_seed is None else config.first_seed + index - 1)
    if config.drive_paths:
        args.drive_path = [f"D{i:02d}={os.path.join(p, 'DriveFusion-VV', campaign_id, f'{index:03d}')}"
                           for i, p in enumerate(config.drive_paths, start=1)]
    return cmd_generate(args)


def _cleanup(dataset: Path, config: Config) -> None:
    manifest = runner.load_manifest(dataset)
    fsops.wipe(manifest)
    for d in manifest["drives"]:
        # The empty DriveFusion-VV/<campaign> folders above a real drive's root.
        parent = Path(d["root"]).parent
        for folder in (parent, parent.parent):
            with contextlib.suppress(OSError):
                folder.rmdir()
    shutil.rmtree(dataset, ignore_errors=True)


def run(config: Config, *, log: Callable[[str], None] = print,
        progress: Callable[[int, int, str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        after_run: Callable[[Path], None] | None = None) -> Path:
    """Run the campaign; returns its folder. Each run is judged on its own."""
    from vv import dashboard
    from vv import evaluate as evaluate_mod
    from vv import report

    chosen = suites_mod.resolve(config.suites)
    if config.drive_paths:
        # Before anything is written: two folders on one device are one copy.
        from vv.__main__ import _check_distinct_devices

        _check_distinct_devices({f"D{i:02d}": os.path.abspath(p)
                                 for i, p in enumerate(config.drive_paths, start=1)})
    out = Path(config.out).resolve()
    fsops.ensure_empty_target(str(out))
    out.mkdir(parents=True, exist_ok=True)
    (out / "runs").mkdir()
    (out / "datasets").mkdir()
    campaign_id = out.name
    meta = {
        "format": "drivefusion-vv-campaign/1", "bench": __version__, "id": campaign_id,
        "started_at": _now(), "host": platform.node(), "platform": platform.platform(),
        "config": {**asdict(config), "suites": chosen, "mode": config.mode},
        "phases": sorted(suites_mod.phases(chosen)), "runs": [],
    }

    def save() -> None:
        (out / "campaign.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        dashboard.write(out)

    save()
    for index in range(1, config.iterations + 1):
        if cancelled():
            log("cancelled before run %d" % index)
            meta["cancelled"] = True
            break
        run_dir = out / "runs" / f"{index:03d}"
        run_dir.mkdir()
        entry = {"run": index, "started_at": _now()}
        buffer = io.StringIO()

        def say(message: str) -> None:
            buffer.write(message + "\n")
            log(f"[{index}/{config.iterations}] {message}")

        if progress:
            progress(index, config.iterations, "generating")
        dataset = None
        try:
            with contextlib.redirect_stdout(io.StringIO()) as generated:
                dataset = _generate(config, out / "datasets" / f"{index:03d}", index, campaign_id)
            for line in generated.getvalue().splitlines():
                say(line)
            manifest = runner.load_manifest(dataset)
            entry["seed"] = manifest["seed"]
            entry["drives"] = [{k: d.get(k) for k in ("id", "fs", "root", "capacity_bytes",
                                                       "device", "identity")}
                               for d in manifest["drives"]]
            (run_dir / "dataset.json").write_text(json.dumps(
                {"seed": manifest["seed"], "mode": manifest["mode"], "params": manifest["params"],
                 "drives": entry["drives"], "scenarios": len(manifest["scenarios"]),
                 "files": len(manifest["files"])}, indent=1), encoding="utf-8")
            if progress:
                progress(index, config.iterations, "running Drive Fusion")
            tool_run = runner.run(dataset, src=config.drivefusion_src, min_copies=config.min_copies,
                                  log=say, phases=suites_mod.phases(chosen))
            observed = json.loads((tool_run / "observed.json").read_text(encoding="utf-8"))
            results = evaluate_mod.evaluate(manifest, observed, chosen)
            report.write(tool_run, manifest, observed, results)
            for name in FILES:
                shutil.copy2(tool_run / name, run_dir / name)
            entry["verdict"] = report.overall(results)
            entry["tool"] = observed.get("tool", {})
            say(f"verdict: {entry['verdict']}")
        except Exception as exc:  # noqa: BLE001 - one bad run must not lose the campaign
            entry["verdict"] = "ERROR"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            say("ERROR: " + traceback.format_exc())
        finally:
            if dataset is not None and not config.keep:
                try:
                    _cleanup(dataset, config)
                except Exception as exc:  # noqa: BLE001
                    say(f"could not remove the dataset: {exc}")
            entry["finished_at"] = _now()
            (run_dir / "log.txt").write_text(buffer.getvalue(), encoding="utf-8")
            meta["runs"].append(entry)
            save()
            if after_run:
                after_run(out)
    meta["finished_at"] = _now()
    save()
    with contextlib.suppress(OSError):
        (out / "datasets").rmdir()
    return out
