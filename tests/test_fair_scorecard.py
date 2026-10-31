"""FAIR and NDSA scorecards per collection, with golden results (ROADMAP M6.7)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.fair import rubrics, scorecard
from drivefusion.core.store import curation
from drivefusion.core.store.catalog import Catalog

TODAY = dt.date(2026, 10, 31)


class Archive:
    """Two drives, each with a root folder; a collection's folder on drive A."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        conn = catalog.conn
        self.drives, self.dirs = {}, {}
        for name, zone in (("A", "home"), ("B", "office")):
            drive = catalog.upsert_drive(serial=name, media="hdd", model=f"M{name}",
                                         manufacturer="Maker")
            conn.execute("UPDATE drive SET disaster_zone = ?, purchase_date = '2022-01' "
                         "WHERE id = ?", (zone, drive))
            volume = conn.execute("INSERT INTO volume (volume_guid, drive_id) VALUES (?, ?)",
                                  (name, drive)).lastrowid
            root = conn.execute("INSERT INTO dir (volume_id, name, depth) VALUES (?, ?, 0)",
                                (volume, f"/{name}")).lastrowid
            self.drives[name] = (drive, volume, root)
        a_volume, a_root = self.drives["A"][1], self.drives["A"][2]
        self.project = conn.execute("INSERT INTO dir (volume_id, parent_id, name, depth) "
                                    "VALUES (?, ?, 'Project', 1)", (a_volume, a_root)).lastrowid
        self.n = 0

    def file(self, where: str, content: int | None, size: int = 100) -> int:
        drive, volume, root = self.drives[where]
        directory = self.project if where == "A" else root
        self.n += 1
        return self.catalog.conn.execute(
            "INSERT INTO file (volume_id, dir_id, name, size_bytes, content_id) "
            "VALUES (?, ?, ?, ?, ?)", (volume, directory, f"f{self.n}", size, content)).lastrowid

    def content(self, size: int = 100) -> int:
        return self.catalog.conn.execute("INSERT INTO content (size_bytes) VALUES (?)",
                                         (size,)).lastrowid


@pytest.fixture
def archive(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        a = Archive(catalog)
        shared = a.content()
        a.file("A", shared)
        a.file("B", shared)                                  # a second copy, another zone
        a.file("A", None, size=777)                          # a single copy, never hashed
        a.coll = curation.create_collection(catalog, "Project", dir_ids=[a.project])
        a.shared = shared
        yield a


def statuses(card) -> dict[str, str]:
    return {r.criterion: r.status for r in card.results}


@pytest.mark.parametrize("rubric_id", rubrics.RUBRICS)
def test_every_criterion_gets_exactly_one_result(archive, rubric_id) -> None:
    card = scorecard.score(archive.catalog, archive.coll, rubric_id, today=TODAY)
    assert [r.criterion for r in card.results] == [c.id for c in rubrics.current(rubric_id).criteria]
    assert all(r.remedy or r.status in ("pass", "judgement") for r in card.results), \
        "every gap names its remedy"


def test_fair_golden_for_an_undescribed_collection(archive) -> None:
    card = scorecard.fair(archive.catalog, archive.coll, today=TODAY)
    assert statuses(card) == {
        "F1": "pass", "F2": "fail", "F3": "planned", "F4": "partial",
        "A1": "pass", "A1.1": "judgement", "A1.2": "judgement", "A2": "pass",
        "I1": "planned", "I2": "planned", "I3": "fail",
        "R1": "fail", "R1.1": "fail", "R1.2": "partial", "R1.3": "planned",
    }
    assert card.score == pytest.approx((1 + 0 + 0.5 + 1 + 1 + 0 + 0 + 0 + 0.5) / 9)
    assert "no license recorded" in card.result("R1.1").detail
    assert card.rubric == "fair-2016" and card.rubric_accepted is True


def test_describing_licensing_relating_and_verifying_raise_the_fair_score(archive) -> None:
    c = archive.catalog
    curation.edit_collection(c, archive.coll, {
        "description": "Lake surveys", "creators_json": '["A. Smith"]', "contact": "a@x",
        "license": "CC-BY-4.0", "data_type": "tabular", "project": "Lakes"})
    c.conn.execute("INSERT INTO collection_relation (from_collection, to_ref, relation_type) "
                   "VALUES (?, 'dfc-0', 'IsPartOf')", (archive.coll,))
    c.conn.execute("INSERT INTO fixity_check (content_id, checked_at, result) "
                   "VALUES (?, '2026-10-01', 'ok')", (archive.shared,))
    s = statuses(scorecard.fair(c, archive.coll, today=TODAY))
    assert (s["F2"], s["R1"], s["R1.1"], s["I3"]) == ("pass", "pass", "pass", "pass")
    assert s["R1.2"] == "partial", "the unhashed single copy can't be verified"


def test_ndsa_golden_and_levels(archive) -> None:
    card = scorecard.ndsa(archive.catalog, archive.coll, today=TODAY)
    s = statuses(card)
    assert s["storage-1a"] == "partial", "one of two items has a single copy"
    assert (s["storage-1b"], s["storage-3b"], s["storage-3a"]) == ("pass", "pass", "partial")
    assert s["storage-2a"] == "fail" and s["storage-4a"] == "fail"
    assert (s["integrity-2b"], s["integrity-2c"], s["integrity-3b"]) == ("pass", "pass", "fail")
    assert s["integrity-1b"] == "partial" and s["integrity-3a"] == "fail"
    assert all(s[f"control-{n}a"] == "judgement" for n in range(1, 5))
    assert (s["metadata-1a"], s["metadata-2a"]) == ("pass", "fail")
    assert s["content-1a"] == "planned"
    assert card.levels == {"storage": 0, "integrity": 0, "control": None,
                           "metadata": 1, "content": None}


def test_a_well_kept_collection_reaches_higher_ndsa_levels(archive) -> None:
    c = archive.catalog
    c.conn.execute("DELETE FROM file WHERE content_id IS NULL")       # no single copies
    c.conn.execute("INSERT INTO fixity_check (content_id, checked_at, result) "
                   "VALUES (?, '2026-10-01', 'ok')", (archive.shared,))
    curation.edit_collection(c, archive.coll, {
        "description": "d", "creators_json": '["x"]', "contact": "c", "license": "CC0-1.0",
        "data_type": "t", "project": "p"})
    card = scorecard.ndsa(c, archive.coll, today=TODAY)
    assert card.levels["storage"] == 1, "two zones, but not three copies"
    assert card.levels["integrity"] == 4 and card.levels["metadata"] == 4


def test_an_old_verification_no_longer_counts(archive) -> None:
    c = archive.catalog
    c.conn.execute("DELETE FROM file WHERE content_id IS NULL")
    c.conn.execute("INSERT INTO fixity_check (content_id, checked_at, result) "
                   "VALUES (?, '2024-01-01', 'ok')", (archive.shared,))
    s = statuses(scorecard.ndsa(c, archive.coll, today=TODAY))
    assert s["integrity-3a"] == "fail" and s["integrity-3b"] == "pass"


def test_the_cli_scores_one_collection_in_full_and_all_in_summary(archive, tmp_path, capsys) -> None:
    path = str(tmp_path / "catalog.db")
    assert main(["--catalog", path, "score"]) == 0
    out = capsys.readouterr().out
    assert "scored over the" in out and "collection 1: Project" in out
    assert "fair-2016: consistent with" in out and "not yet confirmed" not in out
    assert "consistent with levels storage 0" in out
    assert main(["--catalog", path, "score", "1", "--rubric", "fair"]) == 0
    out = capsys.readouterr().out
    assert "✗ R1.1" in out and "→ `drivefusion collections edit ID --license" in out
    assert main(["--catalog", path, "score", "1", "--json"]) == 0
    assert '"rubric": "ndsa-2.0"' in capsys.readouterr().out


def test_a_collection_on_unverified_drives_only_still_gets_remedies(archive) -> None:
    """Found by the V&V bench (VV-FAIR-01): with no holding drive named,
    storage-3b scored 0 of 0 as a fail and named no remedy."""
    archive.catalog.conn.execute("UPDATE volume SET drive_id = NULL")
    card = scorecard.ndsa(archive.catalog, archive.coll, today=TODAY)
    for r in card.results:
        if r.status not in (scorecard.PASS, scorecard.JUDGEMENT):
            assert r.remedy, f"{r.criterion}: {r.status} without a remedy"
    assert "confirm the unverified" in card.result("storage-3b").remedy
