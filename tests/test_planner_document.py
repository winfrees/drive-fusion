"""The plan document and its simulation (ROADMAP M7.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.planner import document
from drivefusion.core.store import curation, plans
from drivefusion.core.store.catalog import Catalog
from tests.test_planner_units import GiB, Fleet


@pytest.fixture
def fleet(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        f = Fleet(catalog)
        f.a = f.drive("A", purchase_date="2019-01-01", nickname="ARCHIVE-1")
        f.b = f.drive("B", purchase_date="2024-01-01", nickname="ARCHIVE-2",
                      cluster=131072, disaster_zone="office")
        f.tiny = f.drive("C", free=GiB, capacity=4 * GiB, nickname="TINY")   # all reserve
        f.photos = f.mkdir("A", "Photos")
        f.file("A", f.photos, 5 << 20)
        f.file("A", f.photos, 3 << 20)
        f.project = f.mkdir("A", "Project")
        f.file("A", f.project, 7 << 20)
        f.coll = curation.create_collection(catalog, "Project", dir_ids=[f.project])
        yield f


def test_a_plan_is_copies_then_their_verification(fleet) -> None:
    doc = document.make(fleet.c, today=None, use_cpsat=False)
    kinds = [s.kind for s in doc.steps]
    assert kinds.count("copy") == 2 and kinds.count("verify") == 2
    for seq, step in enumerate(doc.steps, start=1):
        if step.kind == "verify":
            dep = doc.steps[step.depends_on[0] - 1]
            assert dep.kind == "copy" and dep.unit == step.unit and dep.dst_path == step.dst_path
        if step.kind == "copy":
            assert step.dst_drive_id == fleet.b and step.src_drive_id == fleet.a
            assert step.dst_path.startswith("Drive Fusion copies/ARCHIVE-1/")
            assert step.risk_delta < 0 and step.rationale
            assert step.footprint_bytes % 131072 == 0
    assert {"remove", "delete", "move"}.isdisjoint(kinds)


def test_the_simulation_balances(fleet) -> None:
    doc = document.make(fleet.c, use_cpsat=False)
    s = doc.summary
    copies = [st for st in doc.steps if st.kind == "copy"]
    assert s["copies"] == len(copies)
    assert s["bytes_to_copy"] == sum(st.bytes for st in copies) == 15 << 20
    drives = {d["label"]: d for d in s["drives"]}
    b = drives["ARCHIVE-2"]
    assert b["free_before"] - b["free_after"] == s["footprint_to_copy"]
    assert drives["ARCHIVE-1"]["free_after"] == drives["ARCHIVE-1"]["free_before"]
    assert s["drives_to_connect"] == ["ARCHIVE-2"] and s["source_drives"] == ["ARCHIVE-1"]
    assert s["expected_loss_after"] < s["expected_loss_before"]
    assert s["expected_loss_before"] + sum(st.risk_delta for st in copies) == \
        pytest.approx(s["expected_loss_after"])
    assert s["units_at_target_after"] == s["units"] and s["short_copies"] == 0


def test_describe_and_review_steps(fleet) -> None:
    curation.propose(fleet.c, fleet.photos, 0.7, {})
    doc = document.make(fleet.c, use_cpsat=False)
    describe = [s for s in doc.steps if s.kind == "describe"]
    assert len(describe) == 1 and describe[0].collection_id == fleet.coll
    assert "fill" in describe[0].rationale
    assert [s.kind for s in doc.steps].count("review-candidate") == 1


def test_a_shortfall_is_an_acquire_step_last(fleet) -> None:
    fleet.c.conn.execute("UPDATE drive SET retired_at = '2026-01-01' WHERE id = ?", (fleet.b,))
    doc = document.make(fleet.c, use_cpsat=False)
    assert doc.steps[-1].kind == "acquire"
    assert doc.steps[-1].bytes == 15 << 20
    assert doc.summary["short_copies"] == 2 and doc.summary["copies"] == 0
    assert "Add at least 1 drive" in doc.steps[-1].rationale


def test_ndsa_storage_is_projected(fleet) -> None:
    doc = document.make(fleet.c, use_cpsat=False)
    [proj] = doc.standards["ndsa"]
    assert proj["title"] == "Project"
    assert proj["checks"]["storage-1a"] == {"before": "fail", "after": "pass"}
    assert proj["checks"]["storage-4b"]["after"] == "pass"
    assert proj["checks"]["storage-2a"]["after"] != "pass", "two copies is not three"


def test_saved_plans_render_as_json_and_text(fleet) -> None:
    doc = document.make(fleet.c, name="First", use_cpsat=False)
    plan_id = document.save(fleet.c, doc)
    data = document.to_json(fleet.c, plan_id)
    assert data["name"] == "First" and data["status"] == "draft"
    assert len(data["steps"]) == len(doc.steps)
    assert data["steps"][1]["depends_on"] == [1]
    assert data["steps"][0]["src_path"].endswith(("Photos", "Project"))
    json.dumps(data)                                      # serialisable as it stands
    text = document.to_text(fleet.c, plan_id)
    assert "Connect, in order: ARCHIVE-2" in text and "why:" in text
    assert "nothing; every existing copy stays" in text
    assert plans.current(fleet.c)["id"] == plan_id
