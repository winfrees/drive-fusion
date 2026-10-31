"""The export writer and the export root (ROADMAP M8.1, REVIEW F-03)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest

from drivefusion.core.errors import CatalogLocationError
from drivefusion.core.export import writer
from drivefusion.core.export.writer import ExportError
from drivefusion.core.store import audit, exports, schema
from drivefusion.core.store.catalog import Catalog

NOW = dt.datetime(2026, 10, 31, 20, 15, tzinfo=dt.timezone.utc)


@pytest.fixture
def setup(tmp_path: Path):
    out = tmp_path / "exports"
    out.mkdir()
    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        yield catalog, out


def test_a_bundle_is_a_new_folder_each_time(setup) -> None:
    catalog, out = setup
    first = writer.open_bundle(catalog, out, "manifests", now=NOW)
    second = writer.open_bundle(catalog, out, "manifests", now=NOW)
    assert first.path.name == "20261031T201500Z-manifests"
    assert second.path.name == "20261031T201500Z-manifests-2"
    with pytest.raises(ExportError, match="not an export kind"):
        writer.open_bundle(catalog, out, "../x")
    with pytest.raises(ExportError, match="does not exist"):
        writer.open_bundle(catalog, out / "missing", "manifests")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape.txt", "a/../../b", "C:/x",
                                 "a\\..\\..\\b", "", "./a", "a//b"])
def test_paths_outside_the_bundle_are_refused(setup, bad) -> None:
    catalog, out = setup
    bundle = writer.open_bundle(catalog, out, "t")
    with pytest.raises(ExportError):
        bundle.write(bad, "x")
    assert sorted(p.name for p in out.parent.iterdir()) == ["cat", "exports"]


def test_nothing_is_ever_overwritten(setup) -> None:
    catalog, out = setup
    bundle = writer.open_bundle(catalog, out, "t")
    bundle.write("a/b.txt", "first")
    with pytest.raises(ExportError, match="already exists"):
        bundle.write("a/b.txt", "second")
    assert (bundle.path / "a" / "b.txt").read_text() == "first"
    with pytest.raises(ExportError, match="written when the export closes"):
        bundle.write(writer.MANIFEST, "{}")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_links_are_never_followed(setup, tmp_path) -> None:
    catalog, out = setup
    bundle = writer.open_bundle(catalog, out, "t")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        os.symlink(elsewhere, bundle.path / "sub")
        os.symlink(elsewhere / "f.txt", bundle.path / "f.txt")
    except OSError:
        pytest.skip("cannot create symlinks here")
    with pytest.raises(ExportError, match="is a link"):
        bundle.write("sub/x.txt", "x")
    with pytest.raises(ExportError):
        bundle.write("f.txt", "x")
    assert list(elsewhere.iterdir()) == []


def test_closing_lists_every_file_and_records_the_export(setup) -> None:
    catalog, out = setup
    bundle = writer.open_bundle(catalog, out, "manifests", now=NOW)
    bundle.write("one.txt", "hello")
    bundle.write_json("deep/two.json", {"b": 1})
    export_id = bundle.close(standards={"fair": "fair-2016"}, summary={"drives": 1})
    record = json.loads((bundle.path / writer.MANIFEST).read_text())
    assert record["kind"] == "manifests" and record["standards"] == {"fair": "fair-2016"}
    assert "consistent with" in record["disclaimer"]
    listed = {f["path"]: f for f in record["files"]}
    assert set(listed) == {"one.txt", "deep/two.json"}
    for rel, f in listed.items():
        data = (bundle.path / rel).read_bytes()
        assert f["bytes"] == len(data) and f["sha256"] == hashlib.sha256(data).hexdigest()
    row = exports.export(catalog, export_id)
    assert row["files"] == 2 and row["path"] == str(bundle.path)
    assert audit.entries(catalog.conn, limit=1)[0]["action"] == "export.write"
    with pytest.raises(ExportError, match="closed"):
        bundle.write("late.txt", "x")
    assert schema.current_version(catalog.conn) == schema.SCHEMA_VERSION


def test_checks_can_refuse_a_file_before_it_is_written(setup) -> None:
    catalog, out = setup
    bundle = writer.open_bundle(catalog, out, "t")

    def no_secrets(rel, data):
        if b"secret" in data:
            raise ExportError(f"{rel}: refused")
    bundle.checks.append(no_secrets)
    with pytest.raises(ExportError, match="refused"):
        bundle.write("x.txt", "a secret")
    assert not (bundle.path / "x.txt").exists()


def test_an_export_root_inside_a_scope_root_is_refused(tmp_path) -> None:
    scanned = tmp_path / "scanned"
    (scanned / "inner").mkdir(parents=True)
    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        volume = catalog.upsert_volume(volume_guid="v1")
        catalog.add_scope_root(volume, str(scanned))
        with pytest.raises(CatalogLocationError, match="inside scope root"):
            writer.open_bundle(catalog, scanned / "inner", "t")
        assert list((scanned / "inner").iterdir()) == []
        (tmp_path / "out").mkdir()
        writer.open_bundle(catalog, tmp_path / "out", "t")   # outside: fine


def test_an_export_root_on_a_scanned_drive_is_refused(tmp_path, monkeypatch) -> None:
    from drivefusion.core import discovery

    (tmp_path / "scanned").mkdir()
    (tmp_path / "out").mkdir()
    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        volume = catalog.upsert_volume(volume_guid="EXT")
        catalog.add_scope_root(volume, str(tmp_path / "scanned"))
        monkeypatch.setattr(discovery, "volume_guid_of", lambda p: "EXT")
        monkeypatch.setattr(discovery, "system_volume_guid", lambda: "SYS")
        with pytest.raises(CatalogLocationError, match="same drive as scope root"):
            writer.open_bundle(catalog, tmp_path / "out", "t")
        monkeypatch.setattr(discovery, "system_volume_guid", lambda: "EXT")
        writer.open_bundle(catalog, tmp_path / "out", "t")   # the system drive may
