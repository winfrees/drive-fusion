"""The FAIR and NDSA rubric files (ROADMAP M6.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.fair import rubrics

ROOT = Path(__file__).parent.parent / "drivefusion" / "standards"


def test_fair_has_all_fifteen_principles_in_their_four_groups() -> None:
    fair = rubrics.current("fair")
    assert [c.id for c in fair.criteria] == [
        "F1", "F2", "F3", "F4", "A1", "A1.1", "A1.2", "A2", "I1", "I2", "I3",
        "R1", "R1.1", "R1.2", "R1.3"]
    assert fair.groups == {"F": "Findable", "A": "Accessible", "I": "Interoperable",
                           "R": "Reusable"}
    assert all(c.group == c.id[0] for c in fair.criteria)
    assert "license" in fair.criterion("R1.1").text


def test_ndsa_covers_five_areas_at_four_levels() -> None:
    ndsa = rubrics.current("ndsa")
    assert ndsa.version == "2.0" and set(ndsa.levels) == {1, 2, 3, 4}
    assert set(ndsa.groups) == {"storage", "integrity", "control", "metadata", "content"}
    for group in ndsa.groups:
        assert {c.level for c in ndsa.criteria if c.group == group} == {1, 2, 3, 4}, group
    ids = [c.id for c in ndsa.criteria]
    assert len(ids) == len(set(ids))
    assert all(c.id.startswith(f"{c.group}-{c.level}") for c in ndsa.criteria)


@pytest.mark.parametrize("rubric_id", rubrics.RUBRICS)
def test_every_rubric_is_citable_and_says_how_it_was_obtained(rubric_id) -> None:
    r = rubrics.current(rubric_id)
    assert r.citation and r.url.startswith("https://")
    assert r.retrieval in rubrics.RETRIEVALS
    if r.retrieval in ("confirmed", "reviewed"):
        assert r.retrieved, "a checked or reviewed rubric records when"
    else:
        assert r.retrieved is None, "an unconfirmed rubric claims no retrieval date"
    assert all(c.section and c.text.endswith(".") for c in r.criteria)


def test_file_names_match_their_id_and_version() -> None:
    for path in ROOT.glob("*/rubric-*.json"):
        data = json.loads(path.read_text())
        assert path.name == f"rubric-{data['id']}-{data['version']}.json"
        assert path.parent.name == data["id"]


def test_standards_list_names_the_rubrics_and_their_state(tmp_path, capsys) -> None:
    from drivefusion.cli.main import main

    assert main(["--catalog", str(tmp_path / "c.db"), "standards", "list"]) == 0
    out = capsys.readouterr().out
    assert "fair-2016: FAIR Guiding Principles 2016, 15 criteria" in out
    assert "ndsa-2.0:" in out and "wording reviewed 2026-10-31" in out


def test_provenance_says_how_the_wording_was_checked() -> None:
    import dataclasses

    r = rubrics.current("fair")
    assert dataclasses.replace(r, retrieval="confirmed").provenance().startswith(
        "confirmed against the source")
    assert dataclasses.replace(r, retrieval="reviewed").provenance() == f"wording reviewed {r.retrieved}"
    unconfirmed = dataclasses.replace(r, retrieval="unconfirmed", retrieved=None)
    assert not unconfirmed.accepted and "not yet confirmed" in unconfirmed.provenance()
