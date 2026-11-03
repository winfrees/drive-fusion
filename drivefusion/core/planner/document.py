"""The plan document (PLAN §9, ROADMAP M7.6).

``make`` runs the planner over the catalog and turns its solution into a
checklist: ``copy`` steps, each one copied *and then verified* (scan the
destination, run ``drivefusion verify``), in rounds: the copies of one round
use no drive twice, so they can all run at the same time; ``describe`` steps
for collections short of their descriptive fields; a ``review-candidate``
step for proposed collections waiting on a person; and, last, the ``acquire``
step when the fleet can't hold every target. Each step says briefly why, and
a copy's ``risk_delta`` is the change it brings to the bytes expected lost a
year (negative is better).

The summary also says, per drive, what will be *extra* once the plan's copies
are verified: content held beyond its copy target, and duplicates within one
drive. That is information for a person; a plan never removes anything.

The summary is a before/after simulation: free space per drive, bytes to
copy, the drives to connect in order, expected bytes lost a year, units at
target, and the NDSA storage checks each collection would pass once the
copies are made. It is a projection from the catalog; nothing is copied.

``save`` stores it (``core/store/plans.py``); ``to_json`` and ``to_text``
render a stored plan, so a plan diffs against a later one and archives as
provenance.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass, field

from drivefusion.core.curate import confirm
from drivefusion.core.fair import scorecard
from drivefusion.core.planner import capacity, cpsat, greedy, units as units_mod
from drivefusion.core.store import curation, plans
from drivefusion.core.store.plans import Step

#: Where copies land on a destination, under the source drive's name.
COPY_ROOT = "Drive Fusion copies"
#: What every copy step means, said once rather than in each step.
COPY_AND_VERIFY = ("Copy the folder to the destination, then scan that drive and run "
                   "verify, so the new copy is proven before it is counted.")
#: The NDSA storage checks a plan can change, and what passing takes per unit.
PROJECTED = {
    "storage-1a": lambda copies, zones, batches: copies >= 2,
    "storage-2a": lambda copies, zones, batches: copies >= 3 and zones >= 2,
    "storage-3a": lambda copies, zones, batches: zones >= 2,
    "storage-4a": lambda copies, zones, batches: zones >= 3,
    "storage-4b": lambda copies, zones, batches: batches >= 2,
}


@dataclass
class Document:
    name: str
    solver: str
    steps: list[Step] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    standards: dict = field(default_factory=dict)
    #: The units planned over, and each one's shortfall (copies, bytes): kept
    #: with the document for a caller to inspect, not stored with the plan.
    units: list = field(default_factory=list)
    short: dict = field(default_factory=dict)


def _relative(catalog, dir_id: int) -> str:
    """A folder's path below its volume root, '/'-separated."""
    path = catalog.dir_path(dir_id, "/").replace("\\", "/")
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[1:])


def folder_name(label: str) -> str:
    """A drive name as a folder name Windows accepts: "WD Elements (E:)" becomes
    "WD Elements (E)"."""
    safe = "".join(c for c in label if c not in '<>:"/\\|?*' and ord(c) >= 32)
    return safe.strip(" .") or "drive"


def _destination_path(catalog, unit, source_label: str) -> str:
    rel = _relative(catalog, unit.dir_id)
    return "/".join(p for p in (COPY_ROOT, folder_name(source_label), rel) if p)


def make(catalog, *, name: str | None = None, today=None, use_cpsat: bool = True,
         time_limit: float = cpsat.TIME_LIMIT) -> Document:
    dests = capacity.destinations(catalog)
    clusters = tuple(sorted({d.cluster_bytes for d in dests} | {capacity.DEFAULT_CLUSTER}))
    found = units_mod.build(catalog, clusters=clusters, today=today)
    p_of = greedy.risk_function(catalog, today=today)
    if use_cpsat:
        solution, solver = cpsat.solve(found, dests, p_of, time_limit=time_limit)
    else:
        solution, solver = greedy.solve(found, dests, p_of), "greedy"

    by_id = {d.drive_id: d for d in dests}
    labels = {d.drive_id: d.label for d in dests}
    volume_drive = {r["id"]: r["drive_id"] for r in
                    catalog.conn.execute("SELECT id, drive_id FROM volume")}
    unit_by_key = {u.key: u for u in found}

    # Each added copy's own change to expected loss: copies of one unit are
    # credited in the order they were chosen.
    held = {u.key: set(u.holders) for u in found}
    deltas = []
    for added in solution.added:
        u = unit_by_key[added.unit]
        before = p_of(frozenset(held[u.key])) if held[u.key] else u.p_loss
        held[u.key].add(added.drive_id)
        deltas.append(u.bytes * (p_of(frozenset(held[u.key])) - before))

    by_dest: dict[int, list[int]] = defaultdict(list)
    for i, added in enumerate(solution.added):
        by_dest[added.drive_id].append(i)
    order = sorted(by_dest, key=lambda d: -sum(solution.added[i].footprint for i in by_dest[d]))

    copies_out: list[tuple[int, int, Step]] = []
    for drive in order:
        for i in sorted(by_dest[drive], key=lambda i: deltas[i]):
            added = solution.added[i]
            u = unit_by_key[added.unit]
            sources = sorted(u.holders)
            src = sources[0] if sources else volume_drive.get(u.volume_id)
            src_label = labels.get(src) or (f"drive {src}" if src else "unverified volume")
            copies = len(u.holders) or 1
            reason = (f"{copies} cop{'y' if copies == 1 else 'ies'} now, target {u.target}; "
                      f"{labels[drive]} has room")
            shared = [{"batch": "a purchase batch", "zone": "a recorded zone"}[k]
                      for k in added.crowds[::2]]
            if shared:
                reason += (f"; it shares {' and '.join(shared)} with another copy (allowed: "
                           "the safest drive with room)")
            else:
                reason += ", apart in batch and zone"
            if not sources:
                reason += "; confirm the source drive first"
            copies_out.append((src, drive, Step(
                "copy", reason, unit=u.key, src_drive_id=src, src_dir_id=u.dir_id,
                dst_drive_id=drive, dst_path=_destination_path(catalog, u, src_label),
                collection_id=u.collection_id, bytes=u.bytes, footprint_bytes=added.footprint,
                risk_delta=deltas[i])))
    # Round by round: within a round no drive is read or written twice, so a
    # person can run every copy of a round at once.
    rounds = assign_rounds([(a, b) for a, b, _ in copies_out])
    steps: list[Step] = [st for _, (_, _, st) in sorted(
        enumerate(copies_out), key=lambda item: (rounds[item[0]], item[0]))]

    for coll in curation.collections(catalog):
        filled, total, missing = confirm.completeness(coll)
        if missing:
            steps.append(Step(
                "describe", f"collection “{coll['title']}” has {filled} of {total} descriptive "
                f"fields; fill {', '.join(missing)} so it can be found and reused",
                collection_id=coll["id"]))
    pending = curation.candidates(catalog)
    if pending:
        steps.append(Step(
            "review-candidate", f"{len(pending)} proposed collection(s) wait for a decision; "
            "confirmed collections get their own copy targets"))

    short_copies = sum(n for n, _ in solution.short.values())
    if solution.short:
        need_drives = max(n for n, _ in solution.short.values())
        largest = max(b // n for n, b in solution.short.values())
        total_short = sum(b for _, b in solution.short.values())
        steps.append(Step(
            "acquire", f"{len(solution.short)} unit(s) are {short_copies} cop"
            f"{'y' if short_copies == 1 else 'ies'} short of target: {total_short:,} bytes "
            f"that no connected drive can take within its reserve, batch and zone limits. "
            f"Add at least {need_drives} drive(s), from a different batch, able to hold "
            f"{largest:,} bytes (the largest unit) and {total_short:,} in all, plus a "
            f"{int(capacity.RESERVE_SHARE * 100)} % reserve",
            bytes=total_short))

    summary = _summary(catalog, found, dests, solution, labels, order, by_id)
    summary["short_copies"] = short_copies
    summary["rounds"] = max(rounds, default=0)
    summary["extra_after"] = _extra_after(catalog, found, solution, labels)
    return Document(
        name=name or f"Plan {today or dt.date.today()}", solver=solver, steps=steps, summary=summary,
        policy={"default_target": units_mod.DEFAULT_TARGET,
                "reserve_share": capacity.RESERVE_SHARE,
                "reserve_floor": capacity.RESERVE_FLOOR,
                "batch_and_zone_limit": "target - 1"},
        standards={"ndsa": _projection(catalog, found, solution, by_id, today)},
        units=found, short=dict(solution.short))


def assign_rounds(pairs) -> list[int]:
    """A round (from 1) for each (source drive, destination drive) copy, in
    order: the first round in which neither drive is busy yet."""
    busy: list[set] = []
    out = []
    for pair in pairs:
        drives = {d for d in pair if d is not None}
        for number, used in enumerate(busy, start=1):
            if not drives & used:
                used |= drives
                out.append(number)
                break
        else:
            busy.append(set(drives))
            out.append(len(busy))
    return out


def _extra_after(catalog, found, solution, labels) -> dict:
    """Per drive, what is extra once the plan's copies are verified: units held
    on more drives than their target, and bytes duplicated within the drive.
    Information only: which copy, if any, to let go is a person's decision."""
    out: dict[str, dict] = {}

    def entry(drive_id):
        name = labels.get(drive_id) or f"drive {drive_id}"
        return out.setdefault(name, {"drive_id": drive_id, "beyond_target": [],
                                     "same_drive_duplicate_bytes": 0})

    for u in found:
        holders = solution.holders.get(u.key, u.holders)
        if len(holders) > u.target:
            for d in sorted(holders):
                entry(d)["beyond_target"].append({
                    "unit": u.key, "label": u.label, "bytes": u.bytes,
                    "copies": len(holders), "target": u.target})
    for row in catalog.conn.execute(
            # Physical copies as the redundancy report counts them: a hardlink
            # is the same file twice, not a duplicate.
            "SELECT drive_id, SUM(extra) AS bytes FROM ("
            " SELECT v.drive_id, (COUNT(DISTINCT f.volume_id || ':' ||"
            "   COALESCE(f.frn, 'f' || f.id)) - 1) * MAX(f.size_bytes) AS extra"
            " FROM file f JOIN volume v ON v.id = f.volume_id"
            " WHERE f.vanished_at IS NULL AND f.content_id IS NOT NULL"
            "   AND v.drive_id IS NOT NULL"
            " GROUP BY f.content_id, v.drive_id) GROUP BY drive_id"):
        if row["bytes"]:
            entry(row["drive_id"])["same_drive_duplicate_bytes"] = int(row["bytes"])
    return out


def _summary(catalog, found, dests, solution, labels, order, by_id) -> dict:
    used = defaultdict(int)
    for a in solution.added:
        used[a.drive_id] += a.footprint
    loss_before = sum(u.bytes * u.p_loss for u in found)
    loss_after = cpsat.expected_loss(found, solution)
    sources = {s for u in found for s in u.holders if solution.holders[u.key] != u.holders}
    return {
        "units": len(found),
        "units_at_target_before": sum(1 for u in found if u.copies >= u.target),
        "units_at_target_after": sum(1 for u in found if u.key not in solution.short),
        "bytes_to_copy": sum(a.bytes for a in solution.added),
        "footprint_to_copy": sum(a.footprint for a in solution.added),
        "copies": len(solution.added),
        "drives_to_connect": [labels[d] for d in order],
        "source_drives": sorted(labels.get(s, f"drive {s}") for s in sources),
        "expected_loss_before": loss_before,
        "expected_loss_after": loss_after,
        "drives": [{
            "drive_id": d.drive_id, "label": d.label, "excluded": d.excluded,
            "free_before": d.free, "free_after": (d.free - used[d.drive_id])
            if d.free is not None else None, "reserve": d.reserve,
            "copies_in": sum(1 for a in solution.added if a.drive_id == d.drive_id),
        } for d in dests],
    }


def _projection(catalog, found, solution, by_id, today) -> list[dict]:
    """NDSA storage checks per collection, now and once the plan's copies exist.
    After is projected from the unit's guaranteed holders, so it never claims
    a pass that depends on a copy the plan doesn't make."""
    out = []
    by_collection = defaultdict(list)
    for u in found:
        if u.collection_id is not None:
            by_collection[u.collection_id].append(u)
    for cid, members in sorted(by_collection.items()):
        card = scorecard.ndsa(catalog, cid, today=today)
        checks = {}
        for criterion, passes in PROJECTED.items():
            before = card.result(criterion).status
            after = before
            if before != scorecard.PASS:
                ok = True
                for u in members:
                    h = solution.holders[u.key]
                    zones = {by_id[d].zone for d in h if d in by_id and by_id[d].zone}
                    batches = {by_id[d].batch_key or f"drive-{d}" for d in h if d in by_id}
                    ok = ok and passes(len(h), len(zones), len(batches))
                after = scorecard.PASS if ok else before
            checks[criterion] = {"before": before, "after": after}
        out.append({"collection_id": cid, "title": card.title, "checks": checks})
    return out


def save(catalog, doc: Document) -> int:
    return plans.create(catalog, doc.name, doc.steps, solver=doc.solver, policy=doc.policy,
                        standards=doc.standards, summary=doc.summary)


# -- rendering a stored plan -------------------------------------------------------

def to_json(catalog, plan_id: int) -> dict:
    row = plans.plan(catalog, plan_id)
    if row is None:
        raise plans.PlanError(f"no plan {plan_id}")
    from drivefusion.core.analysis.names import drive_names

    labels = drive_names(catalog)
    stored = list(plans.steps(catalog, plan_id))
    copy_rows = [st for st in stored if st["kind"] == "copy"]
    round_of = dict(zip((st["seq"] for st in copy_rows), assign_rounds(
        [(st["src_drive_id"], st["dst_drive_id"]) for st in copy_rows])))
    return {
        "id": row["id"], "name": row["name"], "status": row["status"],
        "copy_and_verify": COPY_AND_VERIFY,
        "created_at": row["created_at"], "solver": row["solver"],
        "policy": json.loads(row["policy_json"] or "{}"),
        "standards": json.loads(row["standards_json"] or "{}"),
        "summary": json.loads(row["summary_json"] or "{}"),
        "steps": [{
            "seq": s["seq"], "kind": s["kind"], "rationale": s["rationale"], "unit": s["unit"],
            "src_drive_id": s["src_drive_id"], "src_drive": labels.get(s["src_drive_id"]),
            "src_dir_id": s["src_dir_id"],
            "src_path": catalog.dir_path(s["src_dir_id"]) if s["src_dir_id"] else None,
            "dst_drive_id": s["dst_drive_id"], "dst_drive": labels.get(s["dst_drive_id"]),
            "dst_path": s["dst_path"],
            "collection_id": s["collection_id"], "bytes": s["bytes"],
            "footprint_bytes": s["footprint_bytes"],
            "depends_on": [int(x) for x in (s["depends_on"] or "").split(",") if x],
            "risk_delta": s["risk_delta"],
            "round": round_of.get(s["seq"]),
        } for s in stored],
    }


def _bytes(n) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return str(n)


def to_text(catalog, plan_id: int) -> str:
    doc = to_json(catalog, plan_id)
    s = doc["summary"]
    lines = [f"{doc['name']} — plan {doc['id']} ({doc['status']}, {doc['solver']}, "
             f"{doc['created_at']})", "",
             "Each copy: " + COPY_AND_VERIFY, "",
             f"Copies: {s.get('copies', 0)}, {_bytes(s.get('bytes_to_copy'))} "
             f"({_bytes(s.get('footprint_to_copy'))} on disk)",
             f"Connect, in order: {', then '.join(s.get('drives_to_connect') or ['nothing'])}",
             f"Expected loss a year: {_bytes(s.get('expected_loss_before'))} → "
             f"{_bytes(s.get('expected_loss_after'))}",
             f"Units at target: {s.get('units_at_target_before')} → "
             f"{s.get('units_at_target_after')} of {s.get('units')}"]
    if s.get("short_copies"):
        lines.append(f"Short: {s['short_copies']} copies (see the acquire step)")
    lines += ["", "Free space per drive (before → after):"]
    for d in s.get("drives", []):
        note = f"  [{d['excluded']}]" if d["excluded"] else ""
        lines.append(f"  {d['label']}: {_bytes(d['free_before'])} → {_bytes(d['free_after'])}"
                     f", reserve {_bytes(d['reserve'])}{note}")
    ndsa = doc["standards"].get("ndsa") or []
    if ndsa:
        lines += ["", "NDSA storage checks (now → after the plan):"]
        for c in ndsa:
            changed = [f"{k} {v['before']}→{v['after']}" for k, v in c["checks"].items()
                       if v["before"] != v["after"]]
            lines.append(f"  {c['title']}: " + ("; ".join(changed) or "no change"))
    extra = s.get("extra_after") or {}
    if any(e["beyond_target"] or e["same_drive_duplicate_bytes"] for e in extra.values()):
        lines += ["", "Extra once the copies are verified (information; you decide):"]
        for name, e in sorted(extra.items()):
            beyond = sum(x["bytes"] for x in e["beyond_target"])
            parts = []
            if beyond:
                parts.append(f"{_bytes(beyond)} held on more drives than its target")
            if e["same_drive_duplicate_bytes"]:
                parts.append(f"{_bytes(e['same_drive_duplicate_bytes'])} duplicated on this drive")
            if parts:
                lines.append(f"  {name}: " + "; ".join(parts))
    current_round = None
    lines += ["", "Steps:"]
    for st in doc["steps"]:
        if st["round"] != current_round and st["round"] is not None:
            current_round = st["round"]
            lines += ["", f"  Round {current_round} (these copies can run at the same time):"]
        elif st["round"] is None and current_round is not None:
            current_round = None
            lines += ["", "  Then:"]
        if st["kind"] == "copy":
            head = (f"{st['seq']:>4}. copy and verify {st['src_drive'] or 'unconfirmed drive'}: "
                    f"{st['src_path']} → {st['dst_drive']}: {st['dst_path']} "
                    f"({_bytes(st['bytes'])})")
        else:
            head = f"{st['seq']:>4}. {st['kind']}" + (
                f" {st['dst_drive']}: {st['dst_path']}" if st["dst_path"] else "")
        lines.append(head)
        lines.append(f"        why: {st['rationale']}")
        if st["risk_delta"]:
            lines.append(f"        expected loss a year: {_bytes(st['risk_delta'])}")
    return "\n".join(lines) + "\n"
