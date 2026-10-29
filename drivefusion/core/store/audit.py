"""The audit log: append-only and hash-chained (docs/PLAN.md §7.3, FAIR R1.2).

Every entry records what was done to the catalog, when, and by whom, and
carries ``entry_hash = SHA-256(prev_hash, id, at, actor, action, detail_json)``.
Each entry therefore vouches for the one before it: editing, deleting or
reordering any entry breaks the chain from that point on, and
:func:`verify` says where. SQL triggers (schema v6) refuse UPDATE and DELETE
on the table outright; the chain is what catches a change made around them.

What the chain cannot show is the newest entries being cut off the end,
since nothing comes after them to vouch for them. ``drivefusion audit
verify`` prints the head hash so it can be noted elsewhere and compared.

Appends run in ``BEGIN IMMEDIATE`` so two connections (the GUI keeps one
per thread) cannot both chain onto the same previous entry.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from drivefusion.core.store.schema import utcnow

#: Which front end is acting: "cli", "gui", or "library" for direct use (the
#: V&V bench, tests). Set once by each entry point.
_front_end = "library"


def set_front_end(name: str) -> None:
    global _front_end
    _front_end = name


def actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name is not worth failing a scan
        user = "unknown"
    return f"{user} via {_front_end}"


def _entry_hash(prev: bytes | None, entry_id: int, at: str, who: str,
                action: str, detail_json: str) -> bytes:
    payload = json.dumps([entry_id, at, who, action, detail_json],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev or b"") + payload.encode("utf-8")).digest()


def append(conn: sqlite3.Connection, action: str, detail: dict | None = None) -> int:
    """Add one entry and return its id.

    Inside a caller's transaction the entry joins it; otherwise it takes the
    write lock first, so the previous entry it chains onto is the last one.
    """
    detail_json = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute(
            "SELECT id, entry_hash FROM audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        entry_id = (last[0] + 1) if last else 1
        prev = last[1] if last else None
        at, who = utcnow(), actor()
        conn.execute(
            "INSERT INTO audit (id, at, actor, action, detail_json, prev_hash, entry_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, at, who, action, detail_json, prev,
             _entry_hash(prev, entry_id, at, who, action, detail_json)),
        )
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    return entry_id


@dataclass(frozen=True)
class Verification:
    entries: int
    head: bytes | None
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(conn: sqlite3.Connection) -> Verification:
    """Walk the chain from the first entry and report every break."""
    problems: list[str] = []
    prev: bytes | None = None
    expected_id = 1
    count = 0
    for row in conn.execute(
        "SELECT id, at, actor, action, detail_json, prev_hash, entry_hash "
        "FROM audit ORDER BY id"
    ):
        entry_id, at, who, action, detail_json, prev_hash, entry_hash = tuple(row)
        count += 1
        if entry_id != expected_id:
            problems.append(f"entry {expected_id}"
                            + (f" to {entry_id - 1}" if entry_id - 1 > expected_id else "")
                            + " missing")
        if prev_hash != prev:
            problems.append(f"entry {entry_id} does not follow the entry before it")
        if entry_hash != _entry_hash(prev_hash, entry_id, at, who, action, detail_json or ""):
            problems.append(f"entry {entry_id} was changed after it was written")
        prev = entry_hash
        expected_id = entry_id + 1
    return Verification(entries=count, head=prev, problems=problems)


def entries(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT id, at, actor, action, detail_json FROM audit ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))
