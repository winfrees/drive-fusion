"""Per-drive manifests (PLAN §3.3, §10, ROADMAP M8.2).

One folder per drive in the bundle, holding

* ``_DRIVE-FUSION.json``: the drive's identity and recorded metadata, its
  volumes, every live file on it (path, size, modified time, content hash
  where one is known, collection), and for each file where its sibling copies
  live, by drive and path;
* ``_CATALOG.md``: the same for a person: identity, volumes, totals, contents
  by collection and by top-level folder, and how much is held only here.

Copying the folder onto its drive makes a shelved drive self-describing
without the app; that is a step for the person, never done by Drive Fusion
(which writes nothing to a scanned drive). Volumes whose drive the catalog
can't name get a manifest of their own, so nothing catalogued is left out.

Hashes are given as the catalog holds them (``blake3:…`` or ``blake2b:…``);
a file of unique size has none, because it was never read.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from drivefusion.core.store import curation

LIVE = "f.vanished_at IS NULL"


def _when(ns) -> str | None:
    if ns is None:
        return None
    return dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).isoformat(timespec="seconds")


def _label(drive) -> str:
    return drive["nickname"] or drive["model"] or drive["serial"] or f"drive {drive['id']}"


def _folder_name(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in text).strip("-.")
    return safe or "drive"


class _Paths:
    """Every live folder's full path, built once from the interned tree."""

    def __init__(self, catalog) -> None:
        self.rows = {r["id"]: (r["parent_id"], r["name"]) for r in catalog.conn.execute(
            "SELECT id, parent_id, name FROM dir")}
        self.cache: dict[int, str] = {}

    def __call__(self, dir_id: int) -> str:
        if dir_id in self.cache:
            return self.cache[dir_id]
        chain, d = [], dir_id
        while d is not None and d not in self.cache:
            parent, name = self.rows[d]
            chain.append((d, name))
            d = parent
        path = self.cache.get(d, "") if d is not None else ""
        for node, name in reversed(chain):
            path = name if not path else (path.rstrip("/\\") + "/" + name)
            self.cache[node] = path
        return self.cache[dir_id]


def _collections_by_dir(catalog) -> dict[int, tuple[int, str]]:
    children: dict[int, list[int]] = defaultdict(list)
    for r in catalog.conn.execute("SELECT id, parent_id FROM dir WHERE vanished_at IS NULL"):
        children[r["parent_id"]].append(r["id"])
    out: dict[int, tuple[int, str]] = {}
    for coll in curation.collections(catalog):
        for rule in curation.rules(catalog, coll["id"]):
            stack = [rule["dir_id"]]
            while stack:
                d = stack.pop()
                out[d] = (coll["id"], coll["title"])
                stack.extend(children[d])
    return out


def build(catalog) -> list[dict]:
    """A manifest per drive, then one per volume with no drive."""
    conn = catalog.conn
    paths = _Paths(catalog)
    in_collection = _collections_by_dir(catalog)
    drives = {r["id"]: r for r in conn.execute("SELECT * FROM drive")}
    volumes = {r["id"]: r for r in conn.execute("SELECT * FROM volume")}
    rows = conn.execute(
        "SELECT f.id, f.volume_id, f.dir_id, f.name, f.size_bytes, f.mtime_ns, f.content_id, "
        "c.full_hash, c.hash_algo FROM file f LEFT JOIN content c ON c.id = f.content_id "
        f"WHERE {LIVE} ORDER BY f.volume_id, f.dir_id, f.name").fetchall()

    holders: dict[int, list[tuple]] = defaultdict(list)
    for r in rows:
        if r["content_id"] is not None:
            holders[r["content_id"]].append(
                (volumes[r["volume_id"]]["drive_id"], r["volume_id"],
                 f"{paths(r['dir_id'])}/{r['name']}"))

    def where(volume_id) -> tuple:
        drive_id = volumes[volume_id]["drive_id"]
        return ("drive", drive_id) if drive_id is not None else ("volume", volume_id)

    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[where(r["volume_id"])].append(r)
    for drive_id in drives:
        if any(v["drive_id"] == drive_id for v in volumes.values()):
            groups.setdefault(("drive", drive_id), [])

    def sibling(drive_id, volume_id, path) -> dict:
        if drive_id is not None:
            return {"drive_id": drive_id, "drive": _label(drives[drive_id]), "path": path}
        return {"drive_id": None, "volume_id": volume_id,
                "volume": volumes[volume_id]["label"] or volumes[volume_id]["volume_guid"],
                "path": path, "note": "on a volume whose drive is not confirmed"}

    out = []
    for key in sorted(groups, key=lambda k: (k[0] != "drive", k[1])):
        kind, ident = key
        files = []
        for r in groups[key]:
            path = f"{paths(r['dir_id'])}/{r['name']}"
            own_drive = volumes[r["volume_id"]]["drive_id"]
            elsewhere = []
            for d, v, p in holders.get(r["content_id"], ()):
                same = (d == own_drive) if own_drive is not None else (v == r["volume_id"])
                if not same:
                    elsewhere.append(sibling(d, v, p))
            coll = in_collection.get(r["dir_id"])
            files.append({
                "path": path, "bytes": r["size_bytes"], "modified": _when(r["mtime_ns"]),
                "hash": (f"{r['hash_algo']}:{r['full_hash'].hex()}"
                         if r["full_hash"] is not None else None),
                "collection": coll[1] if coll else None,
                "copies_elsewhere": elsewhere,
            })
        if kind == "drive":
            d = drives[ident]
            identity = {k: d[k] for k in (
                "id", "serial", "layout_id", "identity", "model", "manufacturer", "bus",
                "media", "capacity_bytes", "nickname", "purchase_date", "batch_key", "role",
                "location", "disaster_zone", "retired_at")}
            label = _label(d)
            vols = [v for v in volumes.values() if v["drive_id"] == ident]
        else:
            v = volumes[ident]
            identity = {"id": None, "identity": "unconfirmed",
                        "note": "the catalog cannot name the drive this volume is on"}
            label = f"unconfirmed volume {v['label'] or ident}"
            vols = [v]
        out.append({
            "format": "drive-fusion-manifest/1",
            "kind": kind, "label": label,
            "folder": _folder_name(f"{label}-{kind[0]}{ident}"),
            "drive": identity,
            "volumes": [{k: v[k] for k in ("id", "volume_guid", "label", "fs_type",
                                            "capacity_bytes", "free_bytes", "cluster_bytes",
                                            "last_scanned_at") if k in v.keys()}
                        for v in vols],
            "totals": {"files": len(files), "bytes": sum(f["bytes"] or 0 for f in files),
                       "only_here": sum(1 for f in files if not f["copies_elsewhere"])},
            "files": files,
        })
    return out


def _bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{n:,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{n:,} B"


def markdown(manifest: dict) -> str:
    d, t = manifest["drive"], manifest["totals"]
    lines = [f"# {manifest['label']}", "",
             "Written by Drive Fusion. Drive Fusion changed nothing on this drive; this file "
             "describes what the catalog recorded, so the drive explains itself on the shelf. "
             "The full listing is in `_DRIVE-FUSION.json` beside it.", "",
             "## Identity", ""]
    for key in ("serial", "model", "manufacturer", "media", "capacity_bytes", "purchase_date",
                "batch_key", "role", "location", "disaster_zone", "identity", "note"):
        if d.get(key):
            value = _bytes(d[key]) if key == "capacity_bytes" else d[key]
            lines.append(f"- **{key.replace('_', ' ')}:** {value}")
    lines += ["", "## Volumes", ""]
    for v in manifest["volumes"]:
        lines.append(f"- {v.get('label') or v['volume_guid']} ({v.get('fs_type') or '?'}): "
                     f"{_bytes(v.get('capacity_bytes'))}, {_bytes(v.get('free_bytes'))} free, "
                     f"last scanned {v.get('last_scanned_at') or 'unknown'}")
    lines += ["", "## Contents", "",
              f"{t['files']:,} files, {_bytes(t['bytes'])}; {t['only_here']:,} of them have no "
              "copy on any other drive the catalog knows.", ""]
    by_coll: dict[str, list] = defaultdict(lambda: [0, 0])
    by_top: dict[str, list] = defaultdict(lambda: [0, 0])
    for f in manifest["files"]:
        c = by_coll[f["collection"] or "(not in a collection)"]
        c[0] += 1
        c[1] += f["bytes"] or 0
        parts = f["path"].replace("\\", "/").split("/")
        top = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
        g = by_top[top]
        g[0] += 1
        g[1] += f["bytes"] or 0
    lines += ["| Collection | Files | Size |", "|---|---:|---:|"]
    lines += [f"| {name} | {n:,} | {_bytes(b)} |" for name, (n, b) in sorted(by_coll.items())]
    lines += ["", "| Folder | Files | Size |", "|---|---:|---:|"]
    lines += [f"| `{name}` | {n:,} | {_bytes(b)} |" for name, (n, b) in sorted(by_top.items())]
    lines += ["", "Where each file's other copies are is listed per file in "
              "`_DRIVE-FUSION.json` (`copies_elsewhere`).", ""]
    return "\n".join(lines)


def write(bundle, catalog) -> dict:
    """Write every manifest into ``bundle``; returns a summary for _EXPORT.json."""
    made = build(catalog)
    for m in made:
        bundle.write_json(f"{m['folder']}/_DRIVE-FUSION.json", m)
        bundle.write(f"{m['folder']}/_CATALOG.md", markdown(m))
    return {"manifests": len(made), "drives": sum(1 for m in made if m["kind"] == "drive"),
            "files": sum(m["totals"]["files"] for m in made)}
