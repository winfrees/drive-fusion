"""Collections, the review queue and version sets in the catalog (ROADMAP M6.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.store import audit, curation, schema
from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store.curation import CurationError, VersionMember


@pytest.fixture
def tree(tmp_path: Path):
    """root/ with project/ (2 files), project/raw/ (1 file) and other/ (1 file)."""
    with Catalog(tmp_path / "catalog.db") as catalog:
        conn = catalog.conn
        volume = conn.execute("INSERT INTO volume (volume_guid) VALUES ('v')").lastrowid

        def mkdir(name, parent=None, depth=0):
            return conn.execute(
                "INSERT INTO dir (volume_id, parent_id, name, depth) VALUES (?, ?, ?, ?)",
                (volume, parent, name, depth)).lastrowid

        root = mkdir("root")
        dirs = {"root": root, "project": mkdir("project", root, 1),
                "other": mkdir("other", root, 1)}
        dirs["raw"] = mkdir("raw", dirs["project"], 2)

        def add_file(where, name, size):
            conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes) "
                         "VALUES (?, ?, ?, ?)", (volume, dirs[where], name, size))

        add_file("project", "a.csv", 100)
        add_file("project", "b.csv", 200)
        add_file("raw", "c.bin", 1000)
        add_file("other", "d.txt", 7)
        catalog.add_file = add_file
        yield catalog, dirs


def last_action(catalog) -> dict:
    entry = audit.entries(catalog.conn, limit=1)[0]
    return {"action": entry["action"], **json.loads(entry["detail_json"])}


def test_the_schema_is_v9_with_the_curation_tables(tree) -> None:
    catalog, _ = tree
    assert schema.current_version(catalog.conn) == 9
    tables = {r[0] for r in catalog.conn.execute("SELECT name FROM sqlite_master")}
    assert {"collection", "collection_rule", "collection_relation", "collection_candidate",
            "version_set", "version_member"} <= tables


# -- the review queue -----------------------------------------------------------------

def test_a_proposal_only_queues_and_joins_nothing(tree) -> None:
    catalog, dirs = tree
    cid = curation.propose(catalog, dirs["project"], 0.8, {"why": ["one file type"]})
    [queued] = curation.candidates(catalog)
    assert queued["id"] == cid and queued["status"] == "proposed"
    assert json.loads(queued["evidence_json"]) == {"why": ["one file type"]}
    assert curation.collections(catalog) == [], "inference never creates a collection"


def test_proposing_again_refreshes_a_pending_candidate(tree) -> None:
    catalog, dirs = tree
    first = curation.propose(catalog, dirs["project"], 0.5, {"v": 1})
    again = curation.propose(catalog, dirs["project"], 0.9, {"v": 2})
    [queued] = curation.candidates(catalog)
    assert first == again and queued["score"] == 0.9


def test_a_rejection_sticks_however_often_it_is_proposed(tree) -> None:
    catalog, dirs = tree
    cid = curation.propose(catalog, dirs["other"], 0.4, {})
    curation.decide(catalog, cid, "rejected")
    assert last_action(catalog)["action"] == "collection.candidate.rejected"
    assert curation.propose(catalog, dirs["other"], 0.99, {}) is None
    assert curation.candidates(catalog) == []
    assert curation.candidate(catalog, cid)["score"] == 0.4
    with pytest.raises(CurationError, match="already rejected"):
        curation.decide(catalog, cid, "accepted", collection_id=1)


def test_accepting_names_the_collection(tree) -> None:
    catalog, dirs = tree
    cid = curation.propose(catalog, dirs["project"], 0.8, {})
    with pytest.raises(CurationError, match="names the collection"):
        curation.decide(catalog, cid, "accepted")
    with pytest.raises(CurationError, match="accepted or rejected"):
        curation.decide(catalog, cid, "maybe")
    coll = curation.create_collection(catalog, "Project", dir_ids=[dirs["project"]])
    curation.decide(catalog, cid, "accepted", collection_id=coll)
    assert curation.candidate(catalog, cid)["collection_id"] == coll


# -- collections ---------------------------------------------------------------------

def test_a_collection_is_created_with_a_permanent_local_pid_and_audited(tree) -> None:
    catalog, dirs = tree
    coll = curation.create_collection(catalog, "  Field data  ", license="CC-BY-4.0")
    row = curation.collection(catalog, coll)
    assert row["title"] == "Field data" and row["license"] == "CC-BY-4.0"
    assert row["local_pid"].startswith("dfc-") and len(row["local_pid"]) == 20
    assert curation.collection(catalog, row["local_pid"])["id"] == coll
    entry = audit.entries(catalog.conn, limit=1)[0]
    assert entry["action"] == "collection.create"
    other = curation.create_collection(catalog, "Field data")
    assert curation.collection(catalog, other)["local_pid"] != row["local_pid"]


def test_a_collection_needs_a_title_and_known_fields(tree) -> None:
    catalog, _ = tree
    with pytest.raises(CurationError, match="needs a title"):
        curation.create_collection(catalog, "  ")
    with pytest.raises(CurationError, match="not collection fields"):
        curation.create_collection(catalog, "X", colour="red")


def test_membership_is_by_rule_and_includes_files_added_later(tree) -> None:
    catalog, dirs = tree
    coll = curation.create_collection(catalog, "Project", dir_ids=[dirs["project"]])
    assert last_action(catalog)["action"] == "collection.rule.add"
    assert curation.member_stats(catalog, coll) == {"files": 3, "bytes": 1300}
    catalog.add_file("raw", "later.bin", 50)                 # appears after confirmation
    assert curation.member_stats(catalog, coll) == {"files": 4, "bytes": 1350}


def test_vanished_files_and_folders_are_not_members(tree) -> None:
    catalog, dirs = tree
    coll = curation.create_collection(catalog, "Project", dir_ids=[dirs["project"]])
    catalog.conn.execute("UPDATE dir SET vanished_at = 'x' WHERE id = ?", (dirs["raw"],))
    catalog.conn.execute("UPDATE file SET vanished_at = 'x' WHERE name = 'a.csv'")
    assert curation.member_stats(catalog, coll) == {"files": 1, "bytes": 200}


def test_one_folder_belongs_to_one_collection(tree) -> None:
    catalog, dirs = tree
    curation.create_collection(catalog, "A", dir_ids=[dirs["project"]])
    b = curation.create_collection(catalog, "B")
    with pytest.raises(CurationError, match="already collection"):
        curation.add_subtree_rule(catalog, b, dirs["project"])
    with pytest.raises(CurationError, match="no folder"):
        curation.add_subtree_rule(catalog, b, 9999)


def test_edits_are_audited_with_old_and_new_values(tree) -> None:
    catalog, _ = tree
    coll = curation.create_collection(catalog, "Draft")
    assert curation.edit_collection(catalog, coll, {"title": "Final", "license": "CC0-1.0"}) \
        == {"title": "Final", "license": "CC0-1.0"}
    detail = last_action(catalog)
    assert detail["action"] == "collection.edit"
    assert detail["changes"]["title"] == ["Draft", "Final"]
    assert curation.edit_collection(catalog, coll, {"title": "Final"}) == {}, "no change"
    with pytest.raises(CurationError):
        curation.edit_collection(catalog, coll, {"title": ""})


# -- version sets ----------------------------------------------------------------------

def test_version_sets_are_replaced_per_run(tree) -> None:
    catalog, _ = tree
    conn = catalog.conn
    ids = [conn.execute("INSERT INTO content (size_bytes) VALUES (?)", (n,)).lastrowid
           for n in (10, 11, 12)]
    stored = curation.replace_version_sets(catalog, [
        {"label": "report", "confidence": 0.9, "rationale": "newest",
         "members": [VersionMember(ids[0]), VersionMember(ids[1], preferred=True)]},
    ], method="name+size")
    assert stored == 1 and last_action(catalog)["action"] == "versions.find"
    [found] = curation.version_sets(catalog)
    members = curation.version_members(catalog, found["id"])
    assert [m["content_id"] for m in members] == [ids[1], ids[0]]
    assert members[0]["is_preferred"] == 1

    curation.replace_version_sets(catalog, [], method="name+size")
    assert curation.version_sets(catalog) == []
    with pytest.raises(CurationError, match="two or more members"):
        curation.replace_version_sets(catalog, [{"members": [VersionMember(ids[2])]}],
                                      method="m")
