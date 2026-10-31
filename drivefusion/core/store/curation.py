"""Collections, the review queue and version sets in the catalog (ROADMAP M6.1).

The rules PLAN §8.4 sets, enforced here where the rows are written:

* **Inference only proposes.** :func:`propose` writes to ``collection_candidate``
  and nowhere else. A candidate a person rejected stays rejected however
  often inference proposes it again, and one already decided is not re-opened.
* **A collection exists only by an explicit act**, :func:`create_collection`,
  and is audited, as is every rule, decision and edit.
* **Membership is by rule.** A ``subtree`` rule makes every file under a
  directory a member, including files that appear there later.

Version sets are a report (PLAN §8.1): :func:`replace_version_sets` swaps the
whole set for a new run's, and the run is audited as one entry.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from drivefusion.core.store.schema import utcnow

STATUSES = ("proposed", "accepted", "rejected")

#: The PLAN §7.3 fields a person may set on a collection, besides its title.
METADATA = ("description", "creators_json", "contact", "license", "external_pid",
            "data_type", "project", "ndsa_target", "min_copies", "require_diversity",
            "require_offsite", "retention_note")


class CurationError(ValueError):
    """A request the curation rules refuse; the message says why."""


@dataclass(frozen=True)
class VersionMember:
    file_id: int
    content_id: int | None = None
    preferred: bool = False


def new_local_pid() -> str:
    """A permanent local name for a collection (FAIR F1): never reused, never
    derived from a path, so renames and moves don't orphan it."""
    return f"dfc-{uuid.uuid4().hex[:16]}"


# -- the review queue ------------------------------------------------------------

def propose(catalog, dir_id: int, score: float, evidence: dict) -> int | None:
    """Put a subtree in the review queue, or refresh a pending proposal's score
    and evidence. A decided candidate is left as it is. Returns the candidate
    id, or None when it was already decided."""
    row = catalog.conn.execute(
        "SELECT id, status FROM collection_candidate WHERE dir_id = ?", (dir_id,)
    ).fetchone()
    payload = json.dumps(evidence, separators=(",", ":"), sort_keys=True)
    if row is None:
        return int(catalog.conn.execute(
            "INSERT INTO collection_candidate (dir_id, score, evidence_json, proposed_at) "
            "VALUES (?, ?, ?, ?)", (dir_id, float(score), payload, utcnow())
        ).lastrowid)
    if row["status"] != "proposed":
        return None
    catalog.conn.execute(
        "UPDATE collection_candidate SET score = ?, evidence_json = ?, proposed_at = ? "
        "WHERE id = ?", (float(score), payload, utcnow(), row["id"]))
    return int(row["id"])


def candidates(catalog, status: str | None = "proposed"):
    sql = "SELECT * FROM collection_candidate"
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    return catalog.conn.execute(sql + " ORDER BY score DESC, id", params).fetchall()


def candidate(catalog, candidate_id: int):
    return catalog.conn.execute(
        "SELECT * FROM collection_candidate WHERE id = ?", (candidate_id,)).fetchone()


def decide(catalog, candidate_id: int, status: str, *, collection_id: int | None = None) -> None:
    """Record a person's decision on a candidate (accepted or rejected)."""
    if status not in ("accepted", "rejected"):
        raise CurationError(f"a decision is accepted or rejected, not {status!r}")
    row = candidate(catalog, candidate_id)
    if row is None:
        raise CurationError(f"no candidate {candidate_id}")
    if row["status"] != "proposed":
        raise CurationError(f"candidate {candidate_id} was already {row['status']}")
    if status == "accepted" and collection_id is None:
        raise CurationError("an accepted candidate names the collection it became")
    catalog.conn.execute(
        "UPDATE collection_candidate SET status = ?, decided_at = ?, collection_id = ? "
        "WHERE id = ?", (status, utcnow(), collection_id, candidate_id))
    catalog.record(f"collection.candidate.{status}", {
        "candidate_id": candidate_id, "dir_id": row["dir_id"], "collection_id": collection_id})


# -- collections ---------------------------------------------------------------------

def create_collection(catalog, title: str, *, dir_ids: list[int] = (), **metadata) -> int:
    """Create a collection, optionally with subtree rules. Audited."""
    title = (title or "").strip()
    if not title:
        raise CurationError("a collection needs a title")
    unknown = set(metadata) - set(METADATA)
    if unknown:
        raise CurationError(f"not collection fields: {sorted(unknown)}")
    pid = new_local_pid()
    columns = ["local_pid", "title", "created_at", *metadata]
    collection_id = int(catalog.conn.execute(
        f"INSERT INTO collection ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        (pid, title, utcnow(), *metadata.values()),
    ).lastrowid)
    catalog.record("collection.create", {"collection_id": collection_id, "local_pid": pid,
                                         "title": title, **metadata})
    for dir_id in dir_ids:
        add_subtree_rule(catalog, collection_id, dir_id)
    return collection_id


def collection(catalog, key):
    """A collection by id or local PID."""
    column = "local_pid" if isinstance(key, str) else "id"
    return catalog.conn.execute(
        f"SELECT * FROM collection WHERE {column} = ?", (key,)).fetchone()


def collections(catalog):
    return catalog.conn.execute("SELECT * FROM collection ORDER BY title, id").fetchall()


def edit_collection(catalog, collection_id: int, changes: dict) -> dict:
    """Change a collection's title or metadata. Audited with old and new values."""
    row = collection(catalog, collection_id)
    if row is None:
        raise CurationError(f"no collection {collection_id}")
    allowed = {"title", *METADATA}
    unknown = set(changes) - allowed
    if unknown:
        raise CurationError(f"not collection fields: {sorted(unknown)}")
    if "title" in changes and not (changes["title"] or "").strip():
        raise CurationError("a collection needs a title")
    effective = {k: v for k, v in changes.items() if row[k] != v}
    if effective:
        catalog.conn.execute(
            f"UPDATE collection SET {', '.join(f'{k} = ?' for k in effective)} WHERE id = ?",
            (*effective.values(), collection_id))
        catalog.record("collection.edit", {
            "collection_id": collection_id,
            "changes": {k: [row[k], v] for k, v in effective.items()}})
    return effective


def add_subtree_rule(catalog, collection_id: int, dir_id: int) -> int:
    """Make every file under ``dir_id`` a member, now and later. Audited.

    A directory belongs to at most one collection's rule: two collections
    claiming one subtree would make membership ambiguous.
    """
    if collection(catalog, collection_id) is None:
        raise CurationError(f"no collection {collection_id}")
    if catalog.conn.execute("SELECT 1 FROM dir WHERE id = ?", (dir_id,)).fetchone() is None:
        raise CurationError(f"no folder {dir_id} in the catalog")
    holder = catalog.conn.execute(
        "SELECT collection_id FROM collection_rule WHERE kind = 'subtree' AND dir_id = ?",
        (dir_id,)).fetchone()
    if holder is not None:
        raise CurationError(f"folder {dir_id} is already collection {holder['collection_id']}'s")
    rule_id = int(catalog.conn.execute(
        "INSERT INTO collection_rule (collection_id, kind, dir_id, created_at) "
        "VALUES (?, 'subtree', ?, ?)", (collection_id, dir_id, utcnow())).lastrowid)
    catalog.record("collection.rule.add", {"collection_id": collection_id, "rule_id": rule_id,
                                           "kind": "subtree", "dir_id": dir_id})
    return rule_id


def rules(catalog, collection_id: int):
    return catalog.conn.execute(
        "SELECT * FROM collection_rule WHERE collection_id = ? ORDER BY id",
        (collection_id,)).fetchall()


#: Every live directory under a collection's subtree rules, the rule
#: directories included. Parameter: the collection id.
_MEMBER_DIRS = (
    "WITH RECURSIVE tree(id) AS ("
    "  SELECT dir_id FROM collection_rule WHERE collection_id = ? AND kind = 'subtree' "
    "  UNION SELECT d.id FROM dir d JOIN tree t ON d.parent_id = t.id "
    "  WHERE d.vanished_at IS NULL)"
)


def member_stats(catalog, collection_id: int) -> dict:
    """Live files and bytes in a collection, by its rules."""
    row = catalog.conn.execute(
        _MEMBER_DIRS + " SELECT COUNT(f.id) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes "
        "FROM file f WHERE f.vanished_at IS NULL AND f.dir_id IN (SELECT id FROM tree)",
        (collection_id,)).fetchone()
    return {"files": int(row["files"]), "bytes": int(row["bytes"])}


# -- version sets ----------------------------------------------------------------------

def replace_version_sets(catalog, sets: list[dict], *, method: str) -> int:
    """Replace every version set with a new run's. Each set is
    ``{"label", "confidence", "rationale", "members": [VersionMember, ...]}``.
    One audit entry for the run. Returns how many sets were stored."""
    conn = catalog.conn
    conn.execute("DELETE FROM version_member")
    conn.execute("DELETE FROM version_set")
    now = utcnow()
    for found in sets:
        members = found["members"]
        if len(members) < 2 or sum(m.preferred for m in members) > 1:
            raise CurationError("a version set has two or more members and at most one "
                                "preferred")
        set_id = conn.execute(
            "INSERT INTO version_set (label, method, confidence, rationale, found_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (found.get("label"), method, found.get("confidence"), found.get("rationale"), now),
        ).lastrowid
        conn.executemany(
            "INSERT INTO version_member (version_set_id, file_id, content_id, is_preferred) "
            "VALUES (?, ?, ?, ?)",
            [(set_id, m.file_id, m.content_id, int(m.preferred)) for m in members])
    catalog.record("versions.find", {"method": method, "sets": len(sets)})
    return len(sets)


def version_sets(catalog):
    return catalog.conn.execute("SELECT * FROM version_set ORDER BY id").fetchall()


def version_members(catalog, set_id: int):
    return catalog.conn.execute(
        "SELECT * FROM version_member WHERE version_set_id = ? ORDER BY is_preferred DESC, "
        "file_id", (set_id,)).fetchall()
