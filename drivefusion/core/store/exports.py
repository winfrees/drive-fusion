"""Exports in the catalog (ROADMAP M8.1).

A row per bundle that ``core/export/writer.py`` closed: its kind, where it
is, how many files and bytes, and the standards versions it used. Recorded
with an audited ``export.write`` in the same transaction.
"""

from __future__ import annotations

import json

from drivefusion.core.store.schema import utcnow


def record(catalog, *, kind: str, root: str, path: str, files: int, size: int,
           standards: dict, summary: dict) -> int:
    conn = catalog.conn
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        export_id = int(conn.execute(
            "INSERT INTO export (created_at, kind, root, path, files, bytes, standards_json, "
            "summary_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), kind, root, path, files, size, json.dumps(standards, sort_keys=True),
             json.dumps(summary, sort_keys=True))).lastrowid)
        catalog.record("export.write", {"export_id": export_id, "kind": kind, "path": path,
                                        "files": files, "bytes": size})
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    return export_id


def exports(catalog):
    return catalog.conn.execute("SELECT * FROM export ORDER BY id DESC").fetchall()


def export(catalog, export_id: int):
    return catalog.conn.execute("SELECT * FROM export WHERE id = ?", (export_id,)).fetchone()
