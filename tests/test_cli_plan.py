"""`drivefusion plan` (ROADMAP M7.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.store.catalog import Catalog
from tests.test_planner_units import Fleet


def build(path: Path) -> None:
    with Catalog(path) as catalog:
        f = Fleet(catalog)
        f.drive("A", purchase_date="2019-01-01", nickname="ARCHIVE-1")
        f.drive("B", purchase_date="2024-01-01", nickname="ARCHIVE-2")
        photos = f.mkdir("A", "Photos")
        f.file("A", photos, 5 << 20)


def test_make_show_and_list(tmp_path: Path, capsys) -> None:
    db = tmp_path / "catalog.db"
    build(db)
    run = lambda *a: main(["--catalog", str(db), *a])  # noqa: E731

    assert run("plan", "show") == 1
    assert "no current plan" in capsys.readouterr().err

    assert run("plan", "make", "--greedy", "--name", "First") == 0
    out = capsys.readouterr().out
    assert "First — plan 1" in out and "[copy]" in out and "ARCHIVE-2" in out

    assert run("plan", "make", "--greedy", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == 2 and data["solver"] == "greedy"
    assert [s["kind"] for s in data["steps"]] == ["copy", "verify"]

    assert run("plan", "list") == 0
    listed = capsys.readouterr().out.splitlines()
    assert "draft" in listed[0] and "superseded" in listed[1]

    assert run("plan", "show", "1", "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "superseded"
    assert run("plan", "show", "9") == 1
    with pytest.raises(SystemExit):                         # bare `plan` prints help
        run("plan")
