"""Collection packages: RO-Crate, DataCite, BagIt (PLAN §8.3, ROADMAP M8.3, D19).

Each confirmed collection becomes a BagIt 1.0 bag (RFC 8493) in the bundle:

    <local_pid>/
      bagit.txt
      bag-info.txt               Payload-Oxum, Bagging-Date, External-Identifier, …
      manifest-sha256.txt        every payload file's SHA-256
      tagmanifest-sha256.txt     the tag files' SHA-256
      data/ro-crate-metadata.json    RO-Crate 1.1: a schema.org Dataset, a File per member
      data/datacite.json             DataCite 4 properties for deposit
      data/members.csv               path, bytes, modified, hash, drives holding it

The payload is the metadata Drive Fusion writes, not the content (D19): the
catalog's content hashes are BLAKE3 or BLAKE2b, never SHA-256, and a file of
unique size was never read at all, so a bag of the content would mean reading
every file again. Each member is listed with the hash the catalog has.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import PurePosixPath, PureWindowsPath

from drivefusion import __version__
from drivefusion.core.export.manifests import _Paths
from drivefusion.core.store import curation

ROCRATE = "https://w3id.org/ro/crate/1.1"
LICENSES = {
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC-BY-NC-4.0": "https://creativecommons.org/licenses/by-nc/4.0/",
    "MIT": "https://opensource.org/licenses/MIT",
}


def _uri(path: str) -> str:
    if len(path) > 1 and path[1] == ":":
        return PureWindowsPath(path).as_uri()
    return PurePosixPath(path if path.startswith("/") else "/" + path).as_uri()


def members(catalog, collection_id: int) -> list[dict]:
    """Every live file of the collection, with where its content is held."""
    conn = catalog.conn
    rows = conn.execute(
        curation._MEMBER_DIRS + " SELECT f.id, f.dir_id, f.name, f.size_bytes, f.mtime_ns, "
        "f.content_id, c.full_hash, c.hash_algo, v.drive_id FROM file f "
        "JOIN volume v ON v.id = f.volume_id LEFT JOIN content c ON c.id = f.content_id "
        "WHERE f.vanished_at IS NULL AND f.dir_id IN (SELECT id FROM tree) "
        "ORDER BY f.dir_id, f.name", (collection_id,)).fetchall()
    labels = {r["id"]: r["nickname"] or r["model"] or r["serial"] or f"drive {r['id']}"
              for r in conn.execute("SELECT id, nickname, model, serial FROM drive")}
    held: dict[int, set] = {}
    contents = sorted({r["content_id"] for r in rows if r["content_id"] is not None})
    for i in range(0, len(contents), 500):
        part = contents[i:i + 500]
        for h in conn.execute(
            "SELECT f.content_id, v.drive_id FROM file f JOIN volume v ON v.id = f.volume_id "
            f"WHERE f.vanished_at IS NULL AND f.content_id IN ({','.join('?' * len(part))})",
                part):
            if h["drive_id"] is not None:
                held.setdefault(h["content_id"], set()).add(h["drive_id"])
    paths = _Paths(catalog)
    out = []
    for r in rows:
        drives = held.get(r["content_id"]) if r["content_id"] is not None else (
            {r["drive_id"]} if r["drive_id"] is not None else set())
        out.append({
            "path": f"{paths(r['dir_id'])}/{r['name']}",
            "bytes": r["size_bytes"],
            "modified": (dt.datetime.fromtimestamp(r["mtime_ns"] / 1e9, dt.timezone.utc)
                         .isoformat(timespec="seconds") if r["mtime_ns"] else None),
            "hash": (f"{r['hash_algo']}:{r['full_hash'].hex()}"
                     if r["full_hash"] is not None else None),
            "drives": sorted(labels[d] for d in (drives or ())),
        })
    return out


def _people(row) -> list[str]:
    return json.loads(row["creators_json"]) if row["creators_json"] else []


def ro_crate(row, files: list[dict]) -> dict:
    people = _people(row)
    licence = row["license"]
    dataset = {
        "@id": "./", "@type": "Dataset", "name": row["title"],
        "identifier": row["external_pid"] or row["local_pid"],
        "dateCreated": row["created_at"],
        "hasPart": [{"@id": _uri(f["path"])} for f in files],
        "contentSize": str(sum(f["bytes"] or 0 for f in files)),
    }
    if row["description"]:
        dataset["description"] = row["description"]
    if licence:
        dataset["license"] = {"@id": LICENSES.get(licence, f"#license-{licence}")}
    if people:
        dataset["creator"] = [{"@id": f"#creator-{i}"} for i in range(1, len(people) + 1)]
    if row["data_type"]:
        dataset["keywords"] = row["data_type"]
    graph = [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
         "conformsTo": {"@id": ROCRATE}, "about": {"@id": "./"}},
        dataset,
    ]
    graph += [{"@id": f"#creator-{i}", "@type": "Person", "name": name}
              for i, name in enumerate(people, start=1)]
    if licence:
        graph.append({"@id": LICENSES.get(licence, f"#license-{licence}"),
                      "@type": "CreativeWork", "name": licence})
    for f in files:
        entity = {"@id": _uri(f["path"]), "@type": "File", "name": PurePosixPath(
            f["path"]).name, "contentSize": str(f["bytes"] or 0)}
        if f["modified"]:
            entity["dateModified"] = f["modified"]
        if f["hash"]:
            entity["identifier"] = f["hash"]
        if f["drives"]:
            entity["description"] = "Held on " + ", ".join(f["drives"])
        graph.append(entity)
    return {"@context": f"{ROCRATE}/context", "@graph": graph}


def datacite(row, relations, files: list[dict], *, year: int) -> dict:
    """DataCite 4 properties. Publisher and publication year are mandatory in
    DataCite; for a collection not yet deposited they are marked as such."""
    people = _people(row) or ["(creator not recorded)"]
    out = {
        "schemaVersion": "http://datacite.org/schema/kernel-4",
        "identifiers": [{"identifier": row["local_pid"], "identifierType": "Local"}]
        + ([{"identifier": row["external_pid"], "identifierType": "DOI"}]
           if row["external_pid"] else []),
        "creators": [{"name": name} for name in people],
        "titles": [{"title": row["title"]}],
        "publisher": {"name": row["project"] or "(unpublished)"},
        "publicationYear": str(year),
        "types": {"resourceTypeGeneral": "Dataset",
                  "resourceType": row["data_type"] or "Dataset"},
        "sizes": [f"{len(files)} files", f"{sum(f['bytes'] or 0 for f in files)} bytes"],
        "relatedIdentifiers": [
            {"relatedIdentifier": rel["to_ref"], "relationType": rel["relation_type"],
             "relatedIdentifierType": "DOI" if rel["to_ref"].startswith("10.") else "Local"}
            for rel in relations],
    }
    if row["description"]:
        out["descriptions"] = [{"description": row["description"],
                                "descriptionType": "Abstract"}]
    if row["license"]:
        out["rightsList"] = [{"rights": row["license"],
                              "rightsUri": LICENSES.get(row["license"], "")}]
    if row["contact"]:
        out["contributors"] = [{"name": row["contact"], "contributorType": "ContactPerson"}]
    return out


def _csv(files: list[dict]) -> str:
    buffer = io.StringIO()
    out = csv.writer(buffer, lineterminator="\n")
    out.writerow(["path", "bytes", "modified", "hash", "drives"])
    for f in files:
        out.writerow([f["path"], f["bytes"], f["modified"] or "", f["hash"] or "",
                      "; ".join(f["drives"])])
    return buffer.getvalue()


def write(bundle, catalog, *, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    made = []
    for row in curation.collections(catalog):
        files = members(catalog, row["id"])
        relations = catalog.conn.execute(
            "SELECT * FROM collection_relation WHERE from_collection = ? ORDER BY id",
            (row["id"],)).fetchall()
        payload = {
            "ro-crate-metadata.json": json.dumps(ro_crate(row, files), indent=1,
                                                 ensure_ascii=False) + "\n",
            "datacite.json": json.dumps(datacite(row, relations, files, year=today.year),
                                        indent=1, ensure_ascii=False) + "\n",
            "members.csv": _csv(files),
        }
        folder = row["local_pid"]
        sums = {}
        for name, text in payload.items():
            data = text.encode("utf-8")
            bundle.write(f"{folder}/data/{name}", data)
            sums[f"data/{name}"] = (hashlib.sha256(data).hexdigest(), len(data))
        tags = {
            "bagit.txt": "BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
            "bag-info.txt": (
                f"Bag-Software-Agent: Drive Fusion {__version__}\n"
                f"Bagging-Date: {today.isoformat()}\n"
                f"External-Identifier: {row['local_pid']}\n"
                f"External-Description: Metadata for the collection {row['title']}: "
                f"{len(files)} files. The content itself is not in this bag; it is listed in "
                "data/members.csv with the drives that hold it.\n"
                f"Payload-Oxum: {sum(n for _, n in sums.values())}.{len(sums)}\n"),
            "manifest-sha256.txt": "".join(f"{h}  {p}\n" for p, (h, _) in sorted(sums.items())),
        }
        for name, text in tags.items():
            bundle.write(f"{folder}/{name}", text)
        tagsums = "".join(f"{hashlib.sha256(t.encode('utf-8')).hexdigest()}  {n}\n"
                          for n, t in sorted(tags.items()))
        bundle.write(f"{folder}/tagmanifest-sha256.txt", tagsums)
        made.append({"collection": row["local_pid"], "files": len(files)})
    return {"collections": len(made), "members": sum(m["files"] for m in made)}
