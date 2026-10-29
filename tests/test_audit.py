"""The audit log: append-only, hash-chained, and honest about its one blind spot."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from drivefusion.core.store import audit
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "catalog.db") as cat:
        yield cat


def test_entries_chain_and_verify(catalog: Catalog) -> None:
    for i in range(5):
        audit.append(catalog.conn, "test.event", {"n": i})
    result = audit.verify(catalog.conn)
    assert result.ok and result.entries >= 5 and result.head
    newest = audit.entries(catalog.conn, limit=1)[0]
    assert newest["action"] == "test.event" and "via library" in newest["actor"]


def test_the_log_refuses_updates_and_deletes(catalog: Catalog) -> None:
    audit.append(catalog.conn, "test.event")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        catalog.conn.execute("UPDATE audit SET action = 'forged'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        catalog.conn.execute("DELETE FROM audit")


def _around_the_triggers(conn: sqlite3.Connection, statement: str, params=()) -> None:
    """What someone determined could do: drop the triggers, edit, put them back."""
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("DROP TRIGGER audit_no_delete")
    conn.execute(statement, params)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("UPDATE audit SET detail_json = '{\"n\":99}' WHERE id = 3", "entry 3 was changed"),
        ("UPDATE audit SET actor = 'someone else' WHERE id = 2", "entry 2 was changed"),
        ("DELETE FROM audit WHERE id = 3", "entry 3 missing"),
    ],
)
def test_a_change_made_around_the_triggers_is_detected(
    catalog: Catalog, statement: str, expected: str
) -> None:
    for i in range(5):
        audit.append(catalog.conn, "test.event", {"n": i})
    _around_the_triggers(catalog.conn, statement)
    result = audit.verify(catalog.conn)
    assert not result.ok
    assert any(expected in p for p in result.problems), result.problems


def test_a_rewritten_entry_with_a_recomputed_hash_breaks_the_next_link(catalog: Catalog) -> None:
    """Recomputing the edited entry's own hash is not enough: its successor
    still names the old one."""
    for i in range(4):
        audit.append(catalog.conn, "test.event", {"n": i})
    row = catalog.conn.execute(
        "SELECT id, at, actor, action, prev_hash FROM audit WHERE id = 2"
    ).fetchone()
    forged = '{"n":42}'
    new_hash = audit._entry_hash(row["prev_hash"], 2, row["at"], row["actor"],
                                 row["action"], forged)
    _around_the_triggers(catalog.conn,
                         "UPDATE audit SET detail_json = ?, entry_hash = ? WHERE id = 2",
                         (forged, new_hash))
    problems = audit.verify(catalog.conn).problems
    assert any("entry 3 does not follow" in p for p in problems), problems


def test_concurrent_appends_from_two_connections_keep_one_chain(tmp_path: Path) -> None:
    """The GUI writes from one connection per thread."""
    path = tmp_path / "catalog.db"
    Catalog(path).close()

    def writer(tag: str) -> None:
        with Catalog(path) as cat:
            for i in range(25):
                audit.append(cat.conn, "test.event", {"by": tag, "n": i})

    threads = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with Catalog(path) as cat:
        result = audit.verify(cat.conn)
        assert result.ok, result.problems
        assert cat.conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action = 'test.event'"
        ).fetchone()[0] == 50


def test_an_append_inside_a_callers_transaction_rolls_back_with_it(catalog: Catalog) -> None:
    before = audit.verify(catalog.conn).entries
    catalog.conn.execute("BEGIN")
    audit.append(catalog.conn, "test.event")
    catalog.conn.execute("ROLLBACK")
    assert audit.verify(catalog.conn).entries == before


# -- what gets recorded ----------------------------------------------------------

def _actions(catalog: Catalog) -> list[str]:
    return [r["action"] for r in reversed(audit.entries(catalog.conn))]


def test_the_work_that_matters_is_recorded(catalog: Catalog, sample_tree: Path) -> None:
    import json

    from drivefusion.core.identity import hash_pass, verify_pass
    from drivefusion.core.scan import scan_root

    volume_id = catalog.upsert_volume(volume_guid="audit:1")
    root_id = catalog.add_scope_root(volume_id, str(sample_tree))
    scan_root(catalog, root_path=str(sample_tree), root_id=root_id, volume_id=volume_id)
    hash_pass(catalog)
    verify_pass(catalog)
    drive = catalog.confirm_volume_drive(volume_id)
    catalog.remove_scope_root(root_id)

    assert _actions(catalog) == [
        "scope.add", "scan.finished", "hash.pass", "verify.pass",
        "drive.confirm", "scope.remove",
    ]
    detail = {r["action"]: json.loads(r["detail_json"]) for r in audit.entries(catalog.conn)}
    assert detail["scope.add"]["path"] == str(sample_tree)
    assert detail["scan.finished"]["status"] == "done" and detail["scan.finished"]["files"] > 0
    assert detail["drive.confirm"]["drive_id"] == drive
    assert audit.verify(catalog.conn).ok


def test_a_failed_scan_is_recorded_too(catalog: Catalog, tmp_path: Path) -> None:
    import json

    from drivefusion.core.scan import scan_root

    gone = tmp_path / "unplugged"
    gone.mkdir()
    volume_id = catalog.upsert_volume(volume_guid="audit:2")
    root_id = catalog.add_scope_root(volume_id, str(gone))
    gone.rmdir()
    scan_root(catalog, root_path=str(gone), root_id=root_id, volume_id=volume_id)
    last = audit.entries(catalog.conn, limit=1)[0]
    assert last["action"] == "scan.finished"
    assert json.loads(last["detail_json"])["status"] == "failed"


def test_a_schema_upgrade_is_recorded(tmp_path: Path) -> None:
    import json

    path = tmp_path / "old.db"
    with Catalog(path) as catalog:
        catalog.conn.execute("DELETE FROM schema_version")
        catalog.conn.execute("INSERT INTO schema_version VALUES (5, '2026-01-01T00:00:00+00:00')")
    with Catalog(path) as upgraded:
        last = audit.entries(upgraded.conn, limit=1)[0]
        assert last["action"] == "catalog.upgraded"
        assert json.loads(last["detail_json"])["from"] == 5


def test_the_cli_names_itself_and_can_verify_the_chain(tmp_path: Path, sample_tree: Path, capsys) -> None:
    from drivefusion.cli.main import main

    catalog = tmp_path / "cat" / "catalog.db"
    assert main(["--catalog", str(catalog), "--accept-disclaimer", "scope", "add", str(sample_tree)]) == 0
    capsys.readouterr()
    assert main(["--catalog", str(catalog), "audit", "list"]) == 0
    assert "via cli" in capsys.readouterr().out
    assert main(["--catalog", str(catalog), "audit", "verify"]) == 0
    assert "audit log intact" in capsys.readouterr().out

    conn = sqlite3.connect(catalog)
    _around_the_triggers(conn, "UPDATE audit SET action = 'forged' WHERE id = 1")
    conn.commit()
    conn.close()
    assert main(["--catalog", str(catalog), "audit", "verify"]) == 1
    assert "changed after it was written" in capsys.readouterr().err
