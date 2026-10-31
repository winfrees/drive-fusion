"""Plans in the catalog (PLAN §7.3, §9, ROADMAP M7.1).

A plan is a document a person carries out: steps to copy, describe, verify
or acquire, each with its rationale. It is never executed by Drive Fusion,
and no step kind removes or moves anything (the schema's CHECK refuses one).
A plan is created whole, with its steps, and from then on only its status
changes: draft, exported, or superseded by a later plan. Each is audited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from drivefusion.core.store.schema import utcnow

KINDS = ("copy", "organize", "describe", "verify", "review-candidate", "acquire")
STATUSES = ("draft", "exported", "superseded")


class PlanError(ValueError):
    """A plan the rules refuse; the message says why."""


@dataclass
class Step:
    kind: str
    rationale: str
    unit: str = ""
    src_drive_id: int | None = None
    src_dir_id: int | None = None
    dst_drive_id: int | None = None
    dst_path: str | None = None
    collection_id: int | None = None
    bytes: int | None = None
    footprint_bytes: int | None = None
    depends_on: list[int] = field(default_factory=list)
    risk_delta: float | None = None


def create(catalog, name: str, steps: list[Step], *, solver: str, policy: dict,
           standards: dict, summary: dict) -> int:
    """Store a plan and its steps, numbered in order, as one audited act.
    Earlier drafts are marked superseded: there is one current draft."""
    if not name.strip():
        raise PlanError("a plan needs a name")
    for step in steps:
        if step.kind not in KINDS:
            raise PlanError(f"{step.kind!r} is not a plan step; a plan only adds placements, "
                            f"describes, verifies or acquires ({', '.join(KINDS)})")
        if not step.rationale.strip():
            raise PlanError("every step says why")
    conn = catalog.conn
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        superseded = conn.execute(
            "UPDATE plan SET status = 'superseded' WHERE status = 'draft'").rowcount
        plan_id = int(conn.execute(
            "INSERT INTO plan (created_at, name, solver, policy_json, standards_json, "
            "summary_json) VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), name.strip(), solver, json.dumps(policy, sort_keys=True),
             json.dumps(standards, sort_keys=True), json.dumps(summary, sort_keys=True)),
        ).lastrowid)
        conn.executemany(
            "INSERT INTO plan_step (plan_id, seq, kind, rationale, unit, src_drive_id, "
            "src_dir_id, dst_drive_id, dst_path, collection_id, bytes, footprint_bytes, "
            "depends_on, risk_delta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(plan_id, seq, st.kind, st.rationale, st.unit, st.src_drive_id, st.src_dir_id,
              st.dst_drive_id, st.dst_path, st.collection_id, st.bytes, st.footprint_bytes,
              ",".join(map(str, st.depends_on)) or None, st.risk_delta)
             for seq, st in enumerate(steps, start=1)])
        catalog.record("plan.create", {
            "plan_id": plan_id, "name": name.strip(), "solver": solver, "steps": len(steps),
            "kinds": {k: sum(1 for st in steps if st.kind == k) for k in KINDS if
                      any(st.kind == k for st in steps)},
            "superseded": superseded})
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    return plan_id


def plan(catalog, plan_id: int):
    return catalog.conn.execute("SELECT * FROM plan WHERE id = ?", (plan_id,)).fetchone()


def plans(catalog):
    return catalog.conn.execute("SELECT * FROM plan ORDER BY id DESC").fetchall()


def current(catalog):
    """The current draft, if any."""
    return catalog.conn.execute(
        "SELECT * FROM plan WHERE status = 'draft' ORDER BY id DESC LIMIT 1").fetchone()


def steps(catalog, plan_id: int):
    return catalog.conn.execute(
        "SELECT * FROM plan_step WHERE plan_id = ? ORDER BY seq", (plan_id,)).fetchall()


def set_status(catalog, plan_id: int, status: str) -> None:
    if status not in STATUSES:
        raise PlanError(f"a plan is {', '.join(STATUSES)}, not {status!r}")
    row = plan(catalog, plan_id)
    if row is None:
        raise PlanError(f"no plan {plan_id}")
    if row["status"] == status:
        return
    catalog.conn.execute("UPDATE plan SET status = ? WHERE id = ?", (status, plan_id))
    catalog.record("plan.status", {"plan_id": plan_id, "from": row["status"], "to": status})
