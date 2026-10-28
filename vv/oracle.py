"""The test oracle: what a correct Drive Fusion must conclude about a dataset.

Computed from the manifest's ground truth — the generator knows which bytes are
identical because it wrote them — and from the rules as docs/PLAN.md states
them. It deliberately imports nothing from ``drivefusion``: an oracle that
reused the tool's own code would agree with the tool's bugs.

Two spec rules shape the expectations beyond "identical bytes are one content":

* **Coverage (§6.6, §8.1).** A file is opened only if its size is shared with
  another live, non-empty catalogued file. Unique sizes are never hashed and
  must be reported as unhashed. On a rescan, a file identified earlier whose
  size and mtime are unchanged keeps its identity (tier 0) even if its size is
  no longer shared.
* **Counting (§8.1).** Copies are distinct drives; hardlinks are one physical
  copy; same-drive extras are the only waste.
"""

from __future__ import annotations

import copy
from collections import Counter, defaultdict

#: The quick hash samples three 64 KiB blocks and reads small files whole
#: (PLAN §6.6). Stated here from the plan, not imported from the tool.
SAMPLE_BYTES = 64 * 1024
SMALL_FILE_BYTES = 3 * SAMPLE_BYTES

Key = tuple[str, str]  # (drive id, path relative to the drive root, "/"-separated)


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _baseline_state(manifest: dict) -> tuple[dict, set]:
    files = {}
    for f in manifest["files"]:
        files[(f["drive"], f["path"])] = {
            "content": f["content"],
            "size": manifest["contents"][f["content"]]["size"],
            "link_group": f.get("link_group"),
            "unreadable": bool(f.get("unreadable")),
            "scenario": f["scenario"],
            "changed": False,
        }
    dirs = {(d["drive"], d["path"]) for d in manifest["dirs"]}
    return files, dirs


def _rescan_state(manifest: dict, files: dict, dirs: set):
    files = copy.deepcopy(files)
    dirs = set(dirs)
    vanished: set[Key] = set()
    vanished_dirs: set[Key] = set()
    sizes = manifest["contents"]

    def add(drive: str, path: str, entry: dict) -> None:
        files[(drive, path)] = entry
        vanished.discard((drive, path))
        dirs.update((drive, a) for a in _ancestors(path))

    for op in manifest["mutations"]:
        drive, path, kind = op["drive"], op["path"], op["op"]
        key = (drive, path)
        if kind == "add":
            add(drive, path, {"content": op["content"], "size": sizes[op["content"]]["size"],
                              "link_group": None, "unreadable": False,
                              "scenario": op["scenario"], "changed": True})
        elif kind == "modify":
            entry = files[key]
            entry.update(content=op["content"], size=sizes[op["content"]]["size"],
                         changed=True, scenario=op["scenario"])
        elif kind == "delete":
            del files[key]
            vanished.add(key)
        elif kind == "move":
            entry = files.pop(key)
            vanished.add(key)
            add(drive, op["to"], {**entry, "changed": True, "scenario": op["scenario"]})
        elif kind == "mkdir":
            dirs.add(key)
            dirs.update((drive, a) for a in _ancestors(path))
        elif kind == "rmtree":
            prefix = path + "/"
            for k in [k for k in files if k[0] == drive and k[1].startswith(prefix)]:
                del files[k]
                vanished.add(k)
            for d in [d for d in dirs if d[0] == drive and (d[1] == path or d[1].startswith(prefix))]:
                dirs.discard(d)
                vanished_dirs.add(d)
    return files, dirs, vanished, vanished_dirs


def _identify(files: dict, previously_identified: set[Key] | None) -> set[Key]:
    """Which files the spec says must carry a content identity."""
    sizes = Counter(e["size"] for e in files.values() if e["size"] > 0)
    out = set()
    for key, e in files.items():
        if e["unreadable"] or e["size"] == 0:
            continue
        kept = (previously_identified is not None and key in previously_identified
                and not e["changed"])
        if kept or sizes[e["size"]] > 1:
            out.add(key)
    return out


def _analyse(manifest: dict, files: dict, identified: set[Key], min_copies: int) -> dict:
    members: dict[str, list[Key]] = defaultdict(list)
    for key in identified:
        members[files[key]["content"]].append(key)

    groups = {}
    for content, keys in members.items():
        size = files[keys[0]]["size"]
        drives = {k[0] for k in keys}
        physical = {(k[0], files[k]["link_group"] or k[1]) for k in keys}
        groups[content] = {
            "size": size,
            "paths": sorted(keys),
            "drives": len(drives),
            "physical": len(physical),
            "reclaimable": max(0, len(physical) - len(drives)) * size,
        }

    under = {c for c, g in groups.items() if g["drives"] < min_copies}
    reclaim = {c: g["reclaimable"] for c, g in groups.items() if g["physical"] > g["drives"]}
    path_bytes = sum(files[k]["size"] for k in identified)
    unique_bytes = sum(g["size"] for g in groups.values())
    summary = {
        "distinct_content": len(groups),
        "catalogued_paths": len(identified),
        "path_bytes": path_bytes,
        "unique_bytes": unique_bytes,
        "duplicate_overhead_bytes": path_bytes - unique_bytes,
        "under_protected_items": len(under),
        "under_protected_bytes": sum(groups[c]["size"] for c in under),
        "reclaimable_bytes": sum(reclaim.values()),
        "unhashed_files": len(files) - len(identified),
    }
    return {
        "groups": groups,
        "duplicates": {c for c, g in groups.items() if len(g["paths"]) > 1},
        "under_protected": under,
        "reclamation": reclaim,
        "summary": summary,
    }


def _scan_counters(manifest: dict, live_dirs: set[Key]) -> dict:
    out = {d["id"]: {"excluded": 0, "links": 0} for d in manifest["drives"]}
    for e in manifest["excluded"]["roots"]:
        out[e["drive"]]["excluded"] += 1
    for link in manifest["links"]:
        parent = link["path"].rsplit("/", 1)[0] if "/" in link["path"] else ""
        if parent == "" or (link["drive"], parent) in live_dirs:
            out[link["drive"]]["links"] += 1
    return out


def expectations(manifest: dict, *, min_copies: int = 2) -> dict:
    """Expected state after each phase of a run: baseline, rescan, verify."""
    base_files, base_dirs = _baseline_state(manifest)
    base_ident = _identify(base_files, None)
    baseline = {
        "files": base_files, "dirs": base_dirs, "vanished": set(), "vanished_dirs": set(),
        "identified": base_ident,
        "scan": _scan_counters(manifest, base_dirs),
        **_analyse(manifest, base_files, base_ident, min_copies),
    }

    files, dirs, vanished, vanished_dirs = _rescan_state(manifest, base_files, base_dirs)
    ident = _identify(files, base_ident)
    rescan = {
        "files": files, "dirs": dirs, "vanished": vanished, "vanished_dirs": vanished_dirs,
        "identified": ident,
        "scan": _scan_counters(manifest, dirs),
        **_analyse(manifest, files, ident, min_copies),
    }

    corrupted = {(c["drive"], c["path"]) for c in manifest["corruptions"]}
    verify = {
        "corrupted": corrupted,
        "holding_drives": {
            k: rescan["groups"][files[k]["content"]]["drives"] for k in corrupted
        },
        # Small identified files are always verifiable (their quick hash read
        # every byte); large ones only when a full hash exists, which the spec
        # guarantees for the corrupted ones because they were chosen from
        # duplicate groups. This is therefore a floor, not an exact count.
        "min_checked": sum(1 for k in ident if files[k]["size"] <= SMALL_FILE_BYTES)
        + sum(1 for k in corrupted if files[k]["size"] > SMALL_FILE_BYTES),
    }
    return {"baseline": baseline, "rescan": rescan, "verify": verify,
            "min_copies": min_copies}


def summary_json(exp: dict) -> dict:
    """A JSON-friendly digest of expectations, written beside the dataset."""
    def phase(p: dict) -> dict:
        return {
            "summary": p["summary"],
            "files": len(p["files"]),
            "dirs": len(p["dirs"]),
            "vanished_files": len(p["vanished"]),
            "identified": len(p["identified"]),
            "duplicate_groups": len(p["duplicates"]),
            "under_protected": sorted(p["under_protected"]),
            "reclamation": p["reclamation"],
            "scan": p["scan"],
        }
    return {
        "min_copies": exp["min_copies"],
        "baseline": phase(exp["baseline"]),
        "rescan": phase(exp["rescan"]),
        "verify": {
            "corrupted": sorted("/".join(k) for k in exp["verify"]["corrupted"]),
            "min_checked": exp["verify"]["min_checked"],
        },
    }
