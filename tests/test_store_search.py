"""The name search index (ROADMAP M6.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.cli.main import main
from drivefusion.core.store import schema
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path):
    with Catalog(tmp_path / "catalog.db") as cat:
        conn = cat.conn
        volume = conn.execute("INSERT INTO volume (volume_guid, label) VALUES ('v', 'V')").lastrowid
        root = conn.execute("INSERT INTO dir (volume_id, name, depth) VALUES (?, '/r', 0)",
                            (volume,)).lastrowid
        survey = conn.execute("INSERT INTO dir (volume_id, parent_id, name, depth) "
                              "VALUES (?, ?, '2021-03 Lake Survey', 1)", (volume, root)).lastrowid

        def add(name, where=root):
            return conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes) "
                                "VALUES (?, ?, ?, 1)", (volume, where, name)).lastrowid

        cat.ids = {n: add(n) for n in ("report.txt", "Résumé final.docx", "thesis_v2.docx",
                                       "duplicate.bin")}
        cat.ids["station_01.csv"] = add("station_01.csv", survey)
        yield cat


def names(found) -> list[str]:
    return sorted(r["name"] for r in found)


def test_words_match_as_prefixes_in_any_order(catalog) -> None:
    assert names(catalog.search("report")) == ["report.txt"]
    assert names(catalog.search("txt rep")) == ["report.txt"]
    assert names(catalog.search("thesis v2")) == ["thesis_v2.docx"]
    assert names(catalog.search("nothing")) == []


def test_a_folder_name_finds_the_files_in_it(catalog) -> None:
    assert names(catalog.search("lake surv")) == ["station_01.csv"]


def test_accents_dont_matter(catalog) -> None:
    assert names(catalog.search("resume")) == ["Résumé final.docx"]


def test_the_index_follows_renames_and_deletes(catalog) -> None:
    conn = catalog.conn
    conn.execute("UPDATE file SET name = 'summary.txt' WHERE id = ?", (catalog.ids["report.txt"],))
    assert names(catalog.search("report")) == [] and names(catalog.search("summary")) == ["summary.txt"]
    conn.execute("DELETE FROM file WHERE id = ?", (catalog.ids["duplicate.bin"],))
    assert names(catalog.search("duplicate")) == []


def test_vanished_files_only_on_request(catalog) -> None:
    catalog.conn.execute("UPDATE file SET vanished_at = 'x' WHERE id = ?",
                         (catalog.ids["report.txt"],))
    assert names(catalog.search("report")) == []
    assert names(catalog.search("report", include_vanished=True)) == ["report.txt"]


def test_a_search_uses_the_index_not_a_scan(catalog) -> None:
    plan = " ".join(r[3] for r in catalog.conn.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM file_name_fts WHERE file_name_fts MATCH 'x*'"))
    assert "VIRTUAL TABLE INDEX" in plan


def test_an_older_catalog_gets_its_index_built_on_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with Catalog(db) as cat:
        conn = cat.conn
        for name in ("file_name_fts", "dir_name_fts"):
            conn.execute(f"DROP TABLE {name}")
        for trigger in [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%_fts_%'")]:
            conn.execute(f"DROP TRIGGER {trigger}")
        volume = conn.execute("INSERT INTO volume (volume_guid) VALUES ('v')").lastrowid
        root = conn.execute("INSERT INTO dir (volume_id, name, depth) VALUES (?, '/', 0)",
                            (volume,)).lastrowid
        conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes) "
                     "VALUES (?, ?, 'legacy notes.md', 1)", (volume, root))
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (10, 'x')")
    with Catalog(db) as cat:
        assert schema.current_version(cat.conn) == schema.SCHEMA_VERSION
        assert names(cat.search("legacy")) == ["legacy notes.md"]


def test_without_the_index_search_falls_back_to_a_substring_match(catalog, monkeypatch) -> None:
    monkeypatch.setattr(schema, "search_index_ready", lambda conn: False)
    assert names(catalog.search("plica")) == ["duplicate.bin"]


def test_the_cli_searches_words_or_substrings(catalog, tmp_path, capsys) -> None:
    path = str(tmp_path / "catalog.db")
    assert main(["--catalog", path, "find", "lake survey"]) == 0
    assert "station_01.csv" in capsys.readouterr().out
    assert main(["--catalog", path, "find", "plica"]) == 1, "words match from the start"
    capsys.readouterr()
    assert main(["--catalog", path, "find", "plica", "--like"]) == 0
    assert "duplicate.bin" in capsys.readouterr().out
    assert main(["--catalog", path, "find", "%v2%"]) == 0
