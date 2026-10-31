"""Collection packages: RO-Crate, DataCite, BagIt (ROADMAP M8.3)."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from drivefusion.core.export import packages, writer
from drivefusion.core.store import curation
from tests.test_export_manifests import fleet  # noqa: F401  (the fixture)


def validate_bag(bag: Path) -> list[str]:
    """An RFC 8493 check written from the RFC, not from the writer."""
    problems = []
    if (bag / "bagit.txt").read_text(encoding="utf-8").splitlines()[0] != "BagIt-Version: 1.0":
        problems.append("bagit.txt")
    info = dict(line.split(": ", 1) for line in (bag / "bag-info.txt").read_text(encoding="utf-8").splitlines())
    payload = sorted(p for p in (bag / "data").rglob("*") if p.is_file())
    octets = sum(p.stat().st_size for p in payload)
    if info.get("Payload-Oxum") != f"{octets}.{len(payload)}":
        problems.append(f"oxum {info.get('Payload-Oxum')} != {octets}.{len(payload)}")
    listed = {}
    for line in (bag / "manifest-sha256.txt").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        listed[rel] = digest
    if set(listed) != {p.relative_to(bag).as_posix() for p in payload}:
        problems.append("manifest does not cover the payload exactly")
    for rel, digest in listed.items():
        if hashlib.sha256((bag / rel).read_bytes()).hexdigest() != digest:
            problems.append(f"{rel}: checksum")
    tags = {}
    for line in (bag / "tagmanifest-sha256.txt").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        tags[rel] = digest
    if not {"bagit.txt", "bag-info.txt", "manifest-sha256.txt"} <= set(tags):
        problems.append("tag manifest misses a tag file")
    for rel, digest in tags.items():
        if hashlib.sha256((bag / rel).read_bytes()).hexdigest() != digest:
            problems.append(f"{rel}: tag checksum")
    return problems


@pytest.fixture
def packaged(fleet):  # noqa: F811
    coll = curation.collections(fleet.c)[0]
    curation.edit_collection(fleet.c, coll["id"], {
        "license": "CC-BY-4.0", "creators_json": None, "description": "Field data"})
    fleet.c.conn.execute("UPDATE collection SET creators_json = ? WHERE id = ?",
                         (json.dumps(["A. Smith", "B. Jones"]), coll["id"]))
    fleet.c.conn.execute("INSERT INTO collection_relation (from_collection, to_ref, "
                         "relation_type) VALUES (?, '10.1234/abc', 'IsVersionOf')",
                         (coll["id"],))
    bundle = writer.open_bundle(fleet.c, fleet.out, "collections")
    summary = packages.write(bundle, fleet.c, today=dt.date(2026, 10, 31))
    bundle.close(summary=summary)
    return fleet, bundle.path / coll["local_pid"], summary


def test_each_collection_is_a_valid_bag(packaged) -> None:
    _, bag, summary = packaged
    assert summary == {"collections": 1, "members": 2}
    assert validate_bag(bag) == []
    assert "content itself is not in this bag" in (bag / "bag-info.txt").read_text(encoding="utf-8")


def test_the_ro_crate_lists_exactly_the_members(packaged) -> None:
    _, bag, _ = packaged
    crate = json.loads((bag / "data" / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    graph = {e["@id"]: e for e in crate["@graph"]}
    assert graph["ro-crate-metadata.json"]["conformsTo"]["@id"] == packages.ROCRATE
    root = graph["./"]
    assert root["@type"] == "Dataset" and root["license"]["@id"].startswith("https://")
    parts = [graph[p["@id"]] for p in root["hasPart"]]
    assert sorted(int(p["contentSize"]) for p in parts) == [77, 300]
    assert all(p["@type"] == "File" and p["@id"].startswith("file:///") for p in parts)
    shared = next(p for p in parts if p["contentSize"] == "300")
    assert shared["identifier"].startswith("blake3:") and "ARCHIVE-2" in shared["description"]
    assert [graph[c["@id"]]["name"] for c in root["creator"]] == ["A. Smith", "B. Jones"]


def test_datacite_has_its_required_properties(packaged) -> None:
    _, bag, _ = packaged
    dc = json.loads((bag / "data" / "datacite.json").read_text(encoding="utf-8"))
    for key in ("identifiers", "creators", "titles", "publisher", "publicationYear", "types"):
        assert dc[key], key
    assert dc["types"]["resourceTypeGeneral"] == "Dataset"
    assert dc["relatedIdentifiers"] == [{"relatedIdentifier": "10.1234/abc",
                                         "relationType": "IsVersionOf",
                                         "relatedIdentifierType": "DOI"}]
    rows = list(csv.DictReader((bag / "data" / "members.csv").open(encoding="utf-8", newline="")))
    assert sorted(int(r["bytes"]) for r in rows) == [77, 300]


def test_a_tampered_bag_fails_the_check(packaged) -> None:
    """The validator above is itself tested: it notices a changed payload."""
    _, bag, _ = packaged
    target = bag / "data" / "members.csv"
    target.chmod(0o644)
    target.write_text(target.read_text(encoding="utf-8") + "extra\n", encoding="utf-8")
    assert validate_bag(bag)
