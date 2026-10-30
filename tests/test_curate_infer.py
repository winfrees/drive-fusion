"""Proposing collections from the folder tree (ROADMAP M6.2)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.curate import infer
from drivefusion.core.store import audit, curation
from drivefusion.core.store.catalog import Catalog


def _write(path: Path, size: int, when: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    stamp = when.replace(tzinfo=dt.timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def archive(tmp_path: Path):
    """Projects/Surveys/<three dated survey folders of CSVs>, and Misc/ (mixed, old)."""
    root = tmp_path / "archive"
    for i, month in enumerate((3, 6, 9)):
        folder = root / "Projects" / "Surveys" / f"2021-{month:02d} Lake Survey"
        for n in range(8):
            _write(folder / f"station_{n:02d}.csv", 2000 + 97 * n + i,
                   dt.datetime(2021, month, 1 + n))
    for n, (ext, year) in enumerate([("docx", 2013), ("jpg", 2016), ("pdf", 2019),
                                     ("txt", 2023), ("xlsx", 2015), ("zip", 2021)]):
        _write(root / "Misc" / f"thing{n}.{ext}", 500 + n, dt.datetime(year, 5, 1))
    catalog = tmp_path / "catalog" / "catalog.db"
    assert main(["--catalog", str(catalog), "--accept-disclaimer", "scope", "add",
                 str(root)]) == 0
    assert main(["--catalog", str(catalog), "scan"]) == 0
    with Catalog(catalog) as cat:
        yield cat, catalog


def _paths(catalog, proposals) -> list[str]:
    return sorted(os.path.basename(p.evidence["path"]) for p in proposals)


def test_each_survey_in_a_series_is_proposed_and_nothing_else(archive) -> None:
    catalog, _ = archive
    found = infer.infer(catalog)
    assert _paths(catalog, found) == ["2021-03 Lake Survey", "2021-06 Lake Survey",
                                      "2021-09 Lake Survey"]
    assert curation.collections(catalog) == [], "proposals only"
    entry = audit.entries(catalog.conn, limit=1)[0]
    assert entry["action"] == "collection.infer"
    assert json.loads(entry["detail_json"])["proposed"] == 3


def test_every_proposal_carries_its_evidence(archive) -> None:
    catalog, _ = archive
    survey = next(p for p in infer.infer(catalog) if "2021-06" in p.evidence["path"])
    e = survey.evidence
    assert e["files"] == 8 and e["bytes"] == sum(2000 + 97 * n + 1 for n in range(8))
    assert e["date_span"] == ["2021-06", "2021-06"]
    assert e["types"] == [["csv", 8]]
    assert len(e["samples"]) == infer.SAMPLES and all(s.endswith(".csv") for s in e["samples"])
    assert any("series" in why for why in e["why"])
    assert e["signals"]["homogeneity"] == 1.0 and e["signals"]["time"] == 1.0


def test_proposals_never_nest_and_no_scan_root_is_proposed(archive) -> None:
    catalog, _ = archive
    found = {p.dir_id for p in infer.infer(catalog, threshold=0.0)}
    roots = {r["id"] for r in catalog.conn.execute("SELECT id FROM dir WHERE parent_id IS NULL")}
    assert not found & roots
    parents = {r["id"]: r["parent_id"] for r in catalog.conn.execute("SELECT id, parent_id FROM dir")}
    for dir_id in found:
        up = parents[dir_id]
        while up is not None:
            assert up not in found, "a proposal inside another"
            up = parents[up]


def test_a_rejection_sticks_and_a_collection_is_not_proposed_again(archive) -> None:
    catalog, _ = archive
    first = infer.infer(catalog)
    rows = {json.loads(r["evidence_json"])["path"]: r for r in curation.candidates(catalog)}
    march = next(r for p, r in rows.items() if "2021-03" in p)
    june = next(r for p, r in rows.items() if "2021-06" in p)
    curation.decide(catalog, march["id"], "rejected")
    coll = curation.create_collection(catalog, "June survey", dir_ids=[june["dir_id"]])
    curation.decide(catalog, june["id"], "accepted", collection_id=coll)

    again = infer.infer(catalog)
    assert len(first) == 3
    assert [p.queued for p in again if p.dir_id == march["dir_id"]] == [False]
    assert june["dir_id"] not in {p.dir_id for p in again}, "already a collection"
    assert [r["dir_id"] for r in curation.candidates(catalog)] == [
        p.dir_id for p in again if p.queued]


def test_signals() -> None:
    node = infer._Node(1, 0, "2021-03 Lake Survey", 2, files=10, bytes=5 << 20)
    node.types.update({"csv": 9, "txt": 1})
    node.months.update({24255: 10})
    shape = infer._shape(node.name)                          # "#-# lake survey"
    s = infer.signals(node, infer.Counter({shape: 3}))
    assert s == {"homogeneity": 1.0, "time": 1.0, "series": 1.0, "size": 1.0}
    lone = infer.signals(node, infer.Counter({shape: 1}))
    assert lone["series"] == 0.5, "a date in the name, but no series"
    node.months = infer.Counter({24000: 5, 24200: 5})
    assert infer.signals(node, infer.Counter())["time"] == 0.0


def test_the_cli_proposes_and_lists_the_queue(archive, capsys) -> None:
    _, path = archive
    assert main(["--catalog", str(path), "collections", "propose"]) == 0
    out = capsys.readouterr().out
    assert "3 subtree(s) proposed" in out and "Nothing has joined a collection" in out
    assert "2021-06 Lake Survey" in out and "8 files" in out and "csv 8" in out
    assert main(["--catalog", str(path), "collections", "queue"]) == 0
    assert capsys.readouterr().out.count("(proposed)") == 3
