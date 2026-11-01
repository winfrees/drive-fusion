"""V&V suites, campaigns, the dashboard and its window."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vv import campaign, dashboard, suites
from vv.__main__ import main as vv_main


def test_every_requirement_is_in_exactly_one_suite() -> None:
    from vv.requirements import REQUIREMENTS

    for req in REQUIREMENTS:
        owners = [s.name for s in suites.SUITES.values() if req.id in s.requirements()]
        assert len(owners) == 1, (req.id, owners)


def test_suites_choose_their_phases_and_results() -> None:
    assert suites.phases(["planner"]) == {"baseline", "rescan"}
    assert suites.phases(["fixity"]) == {"baseline", "rescan", "verify"}
    assert suites.phases(["exports"]) == {"baseline", "rescan", "verify", "export"}
    assert suites.phases(["all"]) == {"baseline", "rescan", "verify", "export"}
    with pytest.raises(ValueError, match="unknown suite"):
        suites.resolve(["nope"])
    results = [{"requirements": ["R-PLAN-01"]}, {"requirements": ["R-FIX-01"]},
               {"requirements": ["R-RO-01"]}, {"requirements": []}]
    kept = suites.select(results, ["planner"])
    assert [r["requirements"] for r in kept] == [["R-PLAN-01"], ["R-RO-01"], []]


@pytest.fixture(scope="module")
def planner_campaign(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("campaign") / "c1"
    config = campaign.Config(out=str(out), suites=["planner", "safety"], iterations=2, drives=3,
                             drive_size="4GB", files_per_drive=15, large_files=1, first_seed=11)
    return campaign.run(config, log=lambda *_: None)


def test_a_campaign_writes_its_report_structure(planner_campaign: Path) -> None:
    c = planner_campaign
    for name in ("campaign.json", "summary.json", "dashboard.html", "junit.xml"):
        assert (c / name).is_file(), name
    for run in ("001", "002"):
        for name in ("report.md", "results.json", "observed.json", "junit.xml",
                     "dataset.json", "log.txt"):
            assert (c / "runs" / run / name).is_file(), (run, name)
    assert not (c / "datasets").exists(), "each run's dataset is cleaned up"
    meta = json.loads((c / "campaign.json").read_text(encoding="utf-8"))
    assert [r["seed"] for r in meta["runs"]] == [11, 12]
    assert meta["phases"] == ["baseline", "rescan"], "no verify or export for these suites"
    observed = json.loads((c / "runs" / "001" / "observed.json").read_text(encoding="utf-8"))
    assert observed["skipped_phases"] == ["export", "verify"]
    assert "verify" not in observed["phases"]


def test_the_summary_judges_only_the_chosen_suites(planner_campaign: Path) -> None:
    summary = json.loads((planner_campaign / "summary.json").read_text(encoding="utf-8"))
    assert set(summary["suites"]) == {"safety", "planner"}
    assert all(r.startswith(("R-PLAN-", "R-RO-", "R-SC-")) for r in summary["requirements"])
    assert summary["verdict"] == "PASS", summary
    assert all(len(v["runs"]) == 2 for v in summary["requirements"].values())
    results = json.loads((planner_campaign / "runs" / "001" / "results.json")
                         .read_text(encoding="utf-8"))["results"]
    assert not any(r["id"].startswith(("VV-FIX", "VV-EXP", "VV-PHASE-VERIFY")) for r in results)
    page = (planner_campaign / "dashboard.html").read_text(encoding="utf-8")
    assert "<script" not in page and "R-PLAN-01" in page and "runs/002/report.md" in page
    junit = (planner_campaign / "junit.xml").read_text(encoding="utf-8")
    assert junit.count("<testsuite ") == 2


def test_a_failing_run_shows_in_the_summary(planner_campaign: Path, tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(planner_campaign, copy)
    path = copy / "runs" / "002" / "results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in data["results"]:
        if "R-PLAN-02" in r["requirements"]:
            r["verdict"], r["details"] = "FAIL", ["planted for the test"]
    path.write_text(json.dumps(data), encoding="utf-8")
    dashboard.write(copy)
    summary = json.loads((copy / "summary.json").read_text(encoding="utf-8"))
    assert summary["requirements"]["R-PLAN-02"]["runs"][1] == "FAIL"
    assert summary["requirements"]["R-PLAN-02"]["overall"] == "FAIL"
    assert summary["suites"]["planner"]["failed"] == 1
    assert "planted for the test" in (copy / "dashboard.html").read_text(encoding="utf-8")


def test_the_cli_runs_a_campaign_and_lists_suites(tmp_path: Path, capsys) -> None:
    assert vv_main(["suites"]) == 0
    assert "exports" in capsys.readouterr().out
    out = tmp_path / "c"
    assert vv_main(["campaign", "--out", str(out), "--suite", "exports", "--iterations", "1",
                    "--drives", "2", "--drive-size", "4GB", "--files-per-drive", "15",
                    "--large-files", "1"]) == 0
    assert "campaign verdict: PASS" in capsys.readouterr().out
    assert vv_main(["campaign", "--out", str(tmp_path / "d"), "--suite", "bogus"]) == 2


@pytest.mark.usefixtures("qt_app")
def test_the_window_runs_a_campaign_and_shows_the_grid(tmp_path: Path) -> None:
    import time

    from PySide6.QtCore import QCoreApplication, QEvent

    from vv import gui

    window = gui.Window()
    for name, box in window.suite_boxes.items():
        box.setChecked(name == "planner")
    window.drive_count.setValue(2)
    window.iterations.setValue(1)
    window.files.setValue(15)
    window.out.setText(str(tmp_path / "w"))
    window.start()
    deadline = time.monotonic() + 120
    while window.busy() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not window.busy()
    assert window.thread is None and window.worker is None   # released by Qt, not left to GC
    assert window.verdict.text().startswith("PASS"), window.verdict.text()
    rows = [window.grid.verticalHeaderItem(i).text() for i in range(window.grid.rowCount())]
    assert "R-PLAN-01" in rows and window.grid.columnCount() == 3
    window.real.setChecked(True)
    with pytest.raises(ValueError, match="real drive"):
        window.config()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_two_folders_on_one_device_are_refused_before_anything_is_written(tmp_path: Path) -> None:
    from vv import fsops

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    config = campaign.Config(out=str(tmp_path / "c"), drive_paths=[str(a), str(b)])
    with pytest.raises(fsops.DatasetError, match="same physical device"):
        campaign.run(config, log=lambda *_: None)
    assert not (tmp_path / "c").exists() and not any(a.iterdir()) and not any(b.iterdir())
