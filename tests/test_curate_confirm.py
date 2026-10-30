"""Accepting, rejecting, splitting and describing collections (ROADMAP M6.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.curate import confirm, infer
from drivefusion.core.store import audit, curation
from drivefusion.core.store.curation import CurationError
from tests.conftest import _write_dated


def _queue(catalog) -> dict[str, dict]:
    infer.infer(catalog)
    return {Path(json.loads(r["evidence_json"])["path"]).name: r
            for r in curation.candidates(catalog)}


def test_accepting_makes_a_rule_based_collection_titled_after_its_folder(archive) -> None:
    catalog, _ = archive
    june = _queue(catalog)["2021-06 Lake Survey"]
    coll = confirm.accept(catalog, june["id"])
    row = curation.collection(catalog, coll)
    assert row["title"] == "2021-06 Lake Survey"
    assert [r["dir_id"] for r in curation.rules(catalog, coll)] == [june["dir_id"]]
    assert curation.member_stats(catalog, coll)["files"] == 8
    assert curation.candidate(catalog, june["id"])["status"] == "accepted"
    actions = [e["action"] for e in audit.entries(catalog.conn, limit=3)]
    assert actions[0] == "collection.candidate.accepted"


def test_files_added_later_join_and_a_new_sibling_returns_to_the_queue(archive, tmp_path) -> None:
    catalog, path = archive
    queue = _queue(catalog)
    coll = confirm.accept(catalog, queue["2021-06 Lake Survey"]["id"], title="June")
    surveys = tmp_path / "archive" / "Projects" / "Surveys"
    import datetime as dt
    _write_dated(surveys / "2021-06 Lake Survey" / "late.csv", 4321, dt.datetime(2021, 6, 30))
    for n in range(8):
        _write_dated(surveys / "2021-12 Lake Survey" / f"s{n}.csv", 3000 + n,
                     dt.datetime(2021, 12, 1 + n))
    assert main(["--catalog", str(path), "scan"]) == 0
    assert curation.member_stats(catalog, coll)["files"] == 9, "joined by rule"
    again = {Path(p.evidence["path"]).name for p in infer.infer(catalog)}
    assert "2021-12 Lake Survey" in again, "a new subtree is proposed, not absorbed"
    assert "2021-06 Lake Survey" not in again


def test_accepting_into_a_collection_merges_proposals(archive) -> None:
    catalog, _ = archive
    queue = _queue(catalog)
    coll = confirm.accept(catalog, queue["2021-03 Lake Survey"]["id"], title="Lake surveys")
    assert confirm.accept(catalog, queue["2021-06 Lake Survey"]["id"], into=coll) == coll
    assert len(curation.rules(catalog, coll)) == 2
    assert curation.member_stats(catalog, coll)["files"] == 16
    assert len(curation.collections(catalog)) == 1
    with pytest.raises(CurationError, match="keeps its title"):
        confirm.accept(catalog, queue["2021-09 Lake Survey"]["id"], into=coll, title="x")


def test_a_decision_is_made_once(archive) -> None:
    catalog, _ = archive
    march = _queue(catalog)["2021-03 Lake Survey"]
    confirm.reject(catalog, march["id"])
    for act in (lambda: confirm.reject(catalog, march["id"]),
                lambda: confirm.accept(catalog, march["id"]),
                lambda: confirm.split(catalog, march["id"])):
        with pytest.raises(CurationError, match="already rejected"):
            act()
    with pytest.raises(CurationError, match="no proposal"):
        confirm.accept(catalog, 999)


def test_a_failed_accept_leaves_nothing_behind(archive) -> None:
    catalog, _ = archive
    queue = _queue(catalog)
    with pytest.raises(CurationError):
        confirm.accept(catalog, queue["2021-03 Lake Survey"]["id"], title="   ")
    assert curation.collections(catalog) == []
    assert curation.candidate(catalog, queue["2021-03 Lake Survey"]["id"])["status"] == "proposed"


def test_splitting_proposes_each_subfolder_with_files(archive) -> None:
    catalog, _ = archive
    surveys = catalog.conn.execute("SELECT id FROM dir WHERE name = 'Surveys'").fetchone()["id"]
    parent = curation.propose(catalog, surveys, 0.7, {"path": "Surveys"})
    for row in curation.candidates(catalog):
        if row["id"] != parent:
            catalog.conn.execute("DELETE FROM collection_candidate WHERE id = ?", (row["id"],))
    made = confirm.split(catalog, parent)
    names = sorted(Path(json.loads(curation.candidate(catalog, i)["evidence_json"])["path"]).name
                   for i in made)
    assert names == ["2021-03 Lake Survey", "2021-06 Lake Survey", "2021-09 Lake Survey"]
    assert curation.candidate(catalog, parent)["status"] == "rejected"
    leaf = made[0]
    with pytest.raises(CurationError, match="no subfolders"):
        confirm.split(catalog, leaf)


def test_accept_all_above_a_score(archive) -> None:
    catalog, _ = archive
    queue = _queue(catalog)
    top = max(r["score"] for r in queue.values())
    assert len(confirm.accept_all(catalog, min_score=top + 0.01)) == 0
    assert len(confirm.accept_all(catalog, min_score=0.0)) == 3
    assert curation.candidates(catalog) == []


def test_completeness_names_what_is_missing(archive) -> None:
    catalog, _ = archive
    coll = curation.create_collection(catalog, "X", license="CC-BY-4.0")
    filled, total, missing = confirm.completeness(curation.collection(catalog, coll))
    assert (filled, total) == (2, 7) and "license" not in missing and "creators" in missing


def test_the_cli_round_trip(archive, capsys) -> None:
    catalog, path = archive
    cli = lambda *a: main(["--catalog", str(path), *a])  # noqa: E731
    queue = _queue(catalog)
    ids = sorted(r["id"] for r in queue.values())
    assert cli("collections", "accept", str(ids[0]), "--title", "Lake surveys") == 0
    out = capsys.readouterr().out
    assert "accepted: collection 1 (Lake surveys, dfc-" in out
    assert cli("collections", "accept", str(ids[1]), "--into", "1") == 0
    assert cli("collections", "reject", str(ids[2])) == 0
    assert cli("collections", "reject", str(ids[2])) == 1
    assert "already rejected" in capsys.readouterr().err
    assert cli("collections", "edit", "1", "--creators", "A. Smith, B. Jones",
               "--license", "CC-BY-4.0", "--ndsa-target", "2") == 0
    capsys.readouterr()
    assert cli("collections", "show", "1") == 0
    out = capsys.readouterr().out
    assert "creators:      A. Smith, B. Jones" in out and "16 files" in out
    assert out.count("folder:") == 2 and "described:     3/7" in out
    assert cli("collections", "list") == 0
    assert "[1] Lake surveys  16 files" in capsys.readouterr().out
    assert cli("collections", "edit", "1") == 2
    with pytest.raises(SystemExit, match="no collection nope"):
        cli("collections", "show", "nope")
