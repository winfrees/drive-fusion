"""Plans in the catalog (ROADMAP M7.1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from drivefusion.core.store import audit, plans, schema
from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store.plans import PlanError, Step


@pytest.fixture
def catalog(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as cat:
        yield cat


def _make(catalog, name="Consolidate") -> int:
    return plans.create(catalog, name, [
        Step("acquire", "1.2 TB more is needed to give every unit two copies", bytes=1_200),
        Step("copy", "the only copy is on a 2017 drive", unit="Photos", bytes=500,
             footprint_bytes=512, risk_delta=-40.0, depends_on=[1]),
        Step("verify", "check the new copy against its hashes", depends_on=[2]),
    ], solver="greedy", policy={"k": 2}, standards={"rates": ["provisional-1"]},
        summary={"bytes_to_copy": 500})


def test_the_schema_is_v12(catalog) -> None:
    assert schema.current_version(catalog.conn) == schema.SCHEMA_VERSION


def test_a_plan_is_stored_whole_and_audited(catalog) -> None:
    plan_id = _make(catalog)
    row = plans.plan(catalog, plan_id)
    assert (row["status"], row["solver"], json.loads(row["policy_json"])) == ("draft", "greedy",
                                                                            {"k": 2})
    steps = plans.steps(catalog, plan_id)
    assert [(s["seq"], s["kind"]) for s in steps] == [(1, "acquire"), (2, "copy"), (3, "verify")]
    assert steps[1]["depends_on"] == "1" and steps[1]["risk_delta"] == -40.0
    entry = audit.entries(catalog.conn, limit=1)[0]
    detail = json.loads(entry["detail_json"])
    assert entry["action"] == "plan.create" and detail["kinds"] == {"copy": 1, "verify": 1,
                                                                     "acquire": 1}


def test_no_step_can_remove_or_move_anything(catalog) -> None:
    for kind in ("delete", "move", "remove", "dedupe"):
        with pytest.raises(PlanError, match="only adds placements"):
            plans.create(catalog, "x", [Step(kind, "because")], solver="greedy", policy={},
                         standards={}, summary={})
    plan_id = _make(catalog)
    with pytest.raises(sqlite3.IntegrityError):          # the schema refuses it too
        catalog.conn.execute("INSERT INTO plan_step (plan_id, seq, kind, rationale) "
                             "VALUES (?, 9, 'delete', 'x')", (plan_id,))


def test_every_step_says_why(catalog) -> None:
    with pytest.raises(PlanError, match="says why"):
        plans.create(catalog, "x", [Step("copy", "  ")], solver="greedy", policy={},
                     standards={}, summary={})
    assert plans.plans(catalog) == [], "a refused plan leaves nothing behind"


def test_a_new_plan_supersedes_the_last_draft(catalog) -> None:
    first = _make(catalog, "first")
    second = _make(catalog, "second")
    assert plans.plan(catalog, first)["status"] == "superseded"
    assert plans.current(catalog)["id"] == second
    plans.set_status(catalog, second, "exported")
    assert plans.current(catalog) is None
    assert audit.entries(catalog.conn, limit=1)[0]["action"] == "plan.status"
    with pytest.raises(PlanError):
        plans.set_status(catalog, second, "executed")
