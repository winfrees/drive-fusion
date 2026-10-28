"""The V&V bench (docs/VV.md), tested as an instrument.

Three things have to hold before a V&V verdict means anything:

* the datasets really are different on every run, and really are reproducible
  from their seed;
* the oracle encodes the plan's counting rules, checked here against a dataset
  small enough to reason about by hand;
* the evaluator can fail. A comparison that passes no matter what it is shown
  is the V&V equivalent of an empty no-touch snapshot, so every class of
  defect is planted in a real observation and must be caught.

Datasets are generated with fresh random seeds, as the bench intends; every
assertion message carries the seed so a failure can be reproduced with
``python -m vv generate --seed``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from vv import evaluate as ev
from vv import oracle, runner
from vv.__main__ import main as vv_main

SMALL = ["--files-per-drive", "15", "--large-files", "1"]


def _generate(out: Path, *extra: str) -> dict:
    assert vv_main(["generate", "--out", str(out), *SMALL, *extra]) == 0
    return runner.load_manifest(out)


def _tree_digest(manifest: dict) -> dict[str, str]:
    out = {}
    for d in manifest["drives"]:
        for path in sorted(Path(d["root"]).rglob("*")):
            if path.is_file() and not path.is_symlink():
                rel = f"{d['id']}/{path.relative_to(d['root']).as_posix()}"
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory):
    """One full generate → run on three simulated drives, shared by the tests below."""
    dataset = tmp_path_factory.mktemp("vv") / "ds"
    manifest = _generate(dataset, "--drives", "3")
    run_dir = runner.run(dataset, log=lambda *_: None)
    observed = json.loads((run_dir / "observed.json").read_text(encoding="utf-8"))
    results = ev.evaluate(manifest, observed)
    return manifest, observed, results


def _verdicts(results, *, test_id=None, phase=None):
    return {(r["id"], r["phase"]): r for r in results
            if (test_id is None or r["id"] == test_id) and (phase is None or r["phase"] == phase)}


# -- the datasets ------------------------------------------------------------

def test_every_dataset_is_different(tmp_path: Path) -> None:
    a = _generate(tmp_path / "a", "--drives", "2")
    b = _generate(tmp_path / "b", "--drives", "2")
    assert a["seed"] != b["seed"]
    assert _tree_digest(a) != _tree_digest(b), "two runs produced the same data"


def test_a_seed_reproduces_its_dataset_exactly(tmp_path: Path) -> None:
    a = _generate(tmp_path / "a", "--drives", "2", "--seed", "4242")
    b = _generate(tmp_path / "b", "--drives", "2", "--seed", "4242")
    assert a["files"] == b["files"] and a["mutations"] == b["mutations"]
    assert _tree_digest(a) == _tree_digest(b)


def test_every_requirement_is_planted(tmp_path: Path) -> None:
    """A dataset that omits a scenario silently stops testing a requirement."""
    manifest = _generate(tmp_path / "ds", "--drives", "3")
    planted = {req for s in manifest["scenarios"] for req in s["requirements"]}
    # Capability-dependent ones may be absent on this filesystem or as root.
    optional = {"R-CAT-06", "R-RED-02", "R-SC-03"}
    from vv.requirements import REQUIREMENTS

    missing = {r.id for r in REQUIREMENTS} - planted - optional - {"R-RO-01", "R-CAT-07", "R-RED-05"}
    assert not missing, f"seed {manifest['seed']}: not planted: {sorted(missing)}"


def test_generator_refuses_a_folder_that_holds_anything(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "someone's thesis.docx").write_bytes(b"irreplaceable")
    assert vv_main(["generate", "--out", str(target), *SMALL]) == 2
    assert (target / "someone's thesis.docx").read_bytes() == b"irreplaceable"


def test_physical_mode_refuses_two_drives_on_one_device(tmp_path: Path) -> None:
    code = vv_main(["generate", "--out", str(tmp_path / "ds"), *SMALL,
                    "--drive-path", str(tmp_path / "a"), "--drive-path", str(tmp_path / "b")])
    assert code == 2
    assert not (tmp_path / "a").exists(), "refused, but only after writing"


def test_reset_will_not_delete_what_it_did_not_create(tmp_path: Path) -> None:
    manifest = _generate(tmp_path / "ds", "--drives", "1")
    stranger = Path(manifest["drives"][0]["root"]) / "not-from-the-bench.txt"
    stranger.write_bytes(b"keep me")
    assert vv_main(["reset", str(tmp_path / "ds")]) == 2
    assert stranger.read_bytes() == b"keep me"


# -- the oracle --------------------------------------------------------------

def test_oracle_counts_drives_not_paths_and_links_once() -> None:
    """The §8.1 rules on a dataset small enough to check by hand."""
    def f(drive, path, content, link=None):
        return {"drive": drive, "path": path, "content": content, "mtime_ns": 0,
                "scenario": "S1", **({"link_group": link} if link else {})}

    manifest = {
        "contents": {"a": {"size": 100}, "h": {"size": 300}, "u": {"size": 777},
                     "e": {"size": 0}},
        "files": [f("D1", "a1", "a"), f("D1", "x/a2", "a"), f("D2", "a3", "a"),
                  f("D1", "h1", "h", "L"), f("D1", "h2", "h", "L"),
                  f("D2", "u", "u"), f("D1", "e1", "e"), f("D2", "e2", "e")],
        "dirs": [{"drive": "D1", "path": "x"}],
        "drives": [{"id": "D1"}, {"id": "D2"}],
        "excluded": {"roots": [], "dirs": [], "files": []},
        "links": [], "mutations": [], "corruptions": [],
    }
    base = oracle.expectations(manifest)["baseline"]
    a, h = base["groups"]["a"], base["groups"]["h"]
    assert (a["drives"], a["physical"], a["reclaimable"]) == (2, 3, 100)
    assert (h["drives"], h["physical"], h["reclaimable"]) == (1, 1, 0), "hardlink is one copy"
    assert base["under_protected"] == {"h"}
    assert ("D2", "u") not in base["identified"], "a unique size is never hashed"
    assert not {("D1", "e1"), ("D2", "e2")} & base["identified"], "empty files hold no content"
    assert base["summary"]["unhashed_files"] == 3


# -- the evaluator -----------------------------------------------------------

def test_a_full_run_has_no_errors_and_a_clean_baseline(completed_run) -> None:
    manifest, _observed, results = completed_run
    seed = manifest["seed"]
    errors = [r["id"] for r in results if r["verdict"] == ev.ERROR]
    assert not errors, f"seed {seed}: phase errors {errors}"
    for (test_id, phase), r in _verdicts(results).items():
        if phase in ("baseline", "verify"):
            assert r["verdict"] in (ev.PASS, ev.NA), f"seed {seed}: {test_id}@{phase}: {r['details']}"


def test_a_full_run_passes_the_rescan(completed_run) -> None:
    """Failed on every dataset before the identity fix of docs/VV.md §7."""
    manifest, _observed, results = completed_run
    failing = [f"{r['id']}: {r['details'][:1]}" for r in results
               if r["phase"] == "rescan" and r["verdict"] not in (ev.PASS, ev.NA)]
    assert not failing, f"seed {manifest['seed']}: {failing}"


def _rejudge(manifest, observed, mutate) -> dict:
    tampered = copy.deepcopy(observed)
    mutate(tampered)
    return _verdicts(ev.evaluate(manifest, tampered))


def test_evaluator_catches_a_merged_identity(completed_run) -> None:
    manifest, observed, _ = completed_run
    exp = oracle.expectations(manifest)["baseline"]

    def merge(obs):
        files = obs["phases"]["baseline"]["files"]
        by_content = {}
        for row in files:
            key = (row[0], row[1])
            if row[3] is not None and key in exp["files"]:
                by_content.setdefault(exp["files"][key]["content"], row)
        a, b = list(by_content.values())[:2]
        b[3] = a[3]

    assert _rejudge(manifest, observed, merge)[("VV-ID-02", "baseline")]["verdict"] == ev.FAIL


def test_evaluator_catches_a_missing_file(completed_run) -> None:
    manifest, observed, _ = completed_run
    v = _rejudge(manifest, observed, lambda o: o["phases"]["baseline"]["files"].pop())
    assert v[("VV-CAT-01", "baseline")]["verdict"] == ev.FAIL


def test_evaluator_catches_a_wrong_headline_number(completed_run) -> None:
    manifest, observed, _ = completed_run

    def skew(obs):
        obs["phases"]["baseline"]["summary"]["under_protected_items"] += 1

    assert _rejudge(manifest, observed, skew)[("VV-RED-05", "baseline")]["verdict"] == ev.FAIL


def test_evaluator_catches_a_touched_tree(completed_run) -> None:
    manifest, observed, _ = completed_run

    def touch(obs):
        obs["no_touch"]["baseline"] = ["modified (mtime_ns): D01/somewhere.bin"]

    assert _rejudge(manifest, observed, touch)[("VV-RO-01", "baseline")]["verdict"] == ev.FAIL


def test_evaluator_catches_missed_corruption(completed_run) -> None:
    manifest, observed, _ = completed_run
    if not manifest["corruptions"]:
        pytest.skip(f"seed {manifest['seed']}: nothing was corrupted")
    v = _rejudge(manifest, observed, lambda o: o["phases"]["verify"]["incidents"].clear())
    assert v[("VV-FIX-01", "verify")]["verdict"] == ev.FAIL


def test_a_test_that_checked_nothing_is_not_a_pass() -> None:
    assert ev.Result("X", "t", [], "p").finish().verdict == ev.NA


def test_the_product_never_imports_the_bench() -> None:
    """The bench writes; the product must not be able to reach it."""
    root = Path(__file__).resolve().parent.parent / "drivefusion"
    offenders = [p for p in root.rglob("*.py")
                 if "import vv" in p.read_text(encoding="utf-8")
                 or "from vv" in p.read_text(encoding="utf-8")]
    assert not offenders
