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


def test_a_plan_is_copies_each_verified_in_its_own_step(fleet) -> None:
    """One step per copy, copied then verified: no separate verify step."""
    doc = document.make(fleet.c, today=None, use_cpsat=False)
    kinds = [s.kind for s in doc.steps]
    assert kinds.count("copy") == 2 and "verify" not in kinds
    assert "verify" in document.COPY_AND_VERIFY
    for seq, step in enumerate(doc.steps, start=1):
        if step.kind == "copy":
            assert step.dst_drive_id == fleet.b and step.src_drive_id == fleet.a
            assert step.dst_path.startswith("Drive Fusion copies/ARCHIVE-1/")
            assert step.risk_delta < 0 and step.rationale
            assert len(step.rationale) < 140, step.rationale        # said briefly
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
    # Both copies go from ARCHIVE-1 to ARCHIVE-2, so they can't run together.
    assert [st["round"] for st in data["steps"] if st["kind"] == "copy"] == [1, 2]
    assert data["steps"][0]["src_path"].endswith(("Photos", "Project"))
    json.dumps(data)                                      # serialisable as it stands
    text = document.to_text(fleet.c, plan_id)
    assert "Connect, in order: ARCHIVE-2" in text and "why:" in text
    assert "Round 1" in text and "copy and verify" in text
    assert plans.current(fleet.c)["id"] == plan_id


def test_a_copy_sharing_a_zone_says_so(fleet) -> None:
    fleet.c.conn.execute("UPDATE drive SET disaster_zone = 'office' WHERE id = ?", (fleet.a,))
    doc = document.make(fleet.c, use_cpsat=False)
    copies = [s for s in doc.steps if s.kind == "copy"]
    assert copies and all("shares a recorded zone" in s.rationale for s in copies)
    assert all("apart in batch and zone" not in s.rationale for s in copies)


def test_rounds_never_use_a_drive_twice() -> None:
    pairs = [(1, 2), (3, 4), (1, 3), (2, 4), (5, 6), (None, 6)]
    rounds = document.assign_rounds(pairs)
    assert rounds == [1, 1, 2, 2, 1, 2]
    for r in set(rounds):
        used = [d for pair, n in zip(pairs, rounds) if n == r for d in pair if d is not None]
        assert len(used) == len(set(used))


def test_extra_after_the_plan_is_information_per_drive(fleet) -> None:
    """Content over its target, and duplicates within a drive, are reported per
    drive, as information; no step ever removes anything."""
    f = fleet
    twice = f.content(4 << 20)                          # two paths on ARCHIVE-1
    again = f.mkdir("A", "Again")
    f.file("A", again, 4 << 20, twice)
    f.file("A", again, 4 << 20, twice)
    thrice = f.content(2 << 20)                         # one folder, on all three drives
    for drive in ("A", "B", "C"):
        f.file(drive, f.mkdir(drive, "Everywhere"), 2 << 20, thrice)
    doc = document.make(f.c, use_cpsat=False)
    extra = doc.summary["extra_after"]
    assert extra["ARCHIVE-1"]["same_drive_duplicate_bytes"] == 4 << 20
    over = [x for x in extra["ARCHIVE-2"]["beyond_target"] if x["label"].endswith("Everywhere")]
    assert over and over[0]["copies"] == 3 and over[0]["target"] == 2
    assert {"remove", "delete", "move"}.isdisjoint(s.kind for s in doc.steps)
    text = document.to_text(f.c, document.save(f.c, doc))
    assert "Extra once the copies are verified" in text and "you decide" in text


def test_a_drive_name_becomes_a_valid_folder_name() -> None:
    assert document.folder_name("WD Elements (E:)") == "WD Elements (E)"
    assert document.folder_name('a<b>c:"d/e\\f|g?h*') == "abcdefgh"
    assert document.folder_name("::") == "drive"
