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

    def revive_dirs(drive: str, paths: list[str]) -> None:
        # A folder that exists at the rescan is live, whatever happened to it
        # earlier in the sequence.
        for p in paths:
            dirs.add((drive, p))
            vanished_dirs.discard((drive, p))

    def add(drive: str, path: str, entry: dict) -> None:
        files[(drive, path)] = entry
        vanished.discard((drive, path))
        revive_dirs(drive, _ancestors(path))

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
            revive_dirs(drive, [path, *_ancestors(path)])
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


def _drive_identity(manifest: dict) -> tuple[dict[str, str], set[str]]:
    """Each drive's physical device, and the drives nothing identifies.

    Manifests from before R-RED-06 carry neither field: every drive is then its
    own, identified device.
    """
    device = {d["id"]: d.get("device", d["id"]) for d in manifest["drives"]}
    unknown = {d["id"] for d in manifest["drives"] if d.get("identity") == "none"}
    return device, unknown


def _analyse(manifest: dict, files: dict, identified: set[Key], min_copies: int) -> dict:
    device, unknown = _drive_identity(manifest)
    members: dict[str, list[Key]] = defaultdict(list)
    for key in identified:
        members[files[key]["content"]].append(key)

    groups = {}
    for content, keys in members.items():
        size = files[keys[0]]["size"]
        # §8.1 and REVIEW F-05: copies are proven only on identified devices
        # (a live file is on one device at least); for reclamation, an
        # unidentified drive may be a device of its own.
        proven = max(1, len({device[k[0]] for k in keys if k[0] not in unknown}))
        possible = len({device[k[0]] if k[0] not in unknown else ("unknown", k[0])
                        for k in keys})
        physical = {(k[0], files[k]["link_group"] or k[1]) for k in keys}
        holders = [k[0] for k in keys]
        groups[content] = {
            "size": size,
            "paths": sorted(keys),
            "drives": proven,
            "drives_possible": possible,
            "physical": len(physical),
            "reclaimable": max(0, len(physical) - possible) * size,
            # Where counting volumes rather than identified devices goes wrong.
            "identity_case": (len({device[d] for d in holders}) < len(set(holders))
                              or any(d in unknown for d in holders)),
        }

    # REVIEW.md SR-12 (F-04): a live, non-empty file whose size no other live
    # file shares, in any read state, is one copy on one drive. That is known
    # without reading it, so it counts without a content identity.
    all_sizes = Counter(e["size"] for e in files.values())
    singles = {k: e["size"] for k, e in files.items()
               if k not in identified and e["size"] > 0 and all_sizes[e["size"]] == 1}

    under = {c for c, g in groups.items() if g["drives"] < min_copies}
    under_singles = singles if min_copies > 1 else {}
    reclaim = {c: g["reclaimable"] for c, g in groups.items() if g["reclaimable"] > 0}
    single_bytes = sum(singles.values())
    path_bytes = sum(files[k]["size"] for k in identified) + single_bytes
    unique_bytes = sum(g["size"] for g in groups.values()) + single_bytes
    summary = {
        "distinct_content": len(groups) + len(singles),
        "catalogued_paths": len(identified) + len(singles),
        "path_bytes": path_bytes,
        "unique_bytes": unique_bytes,
        "duplicate_overhead_bytes": path_bytes - unique_bytes,
        "under_protected_items": len(under) + len(under_singles),
        "under_protected_bytes": sum(groups[c]["size"] for c in under)
        + sum(under_singles.values()),
        "reclaimable_bytes": sum(reclaim.values()),
        "single_copy_files": len(singles),
        # Only files that could still be copies of something are unknown.
        "unhashed_files": sum(1 for k, e in files.items()
                              if k not in identified and all_sizes[e["size"]] > 1),
    }
    return {
        "groups": groups,
        "singles": singles,
        "under_protected_singles": set(under_singles),
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
    edited = {(e["drive"], e["path"]) for e in manifest.get("late_edits", [])}
    verify = {
        "corrupted": corrupted,
        # Edited after the rescan with a new date: modified, not checked (§8.1,
        # REVIEW F-06), so they are left out of the verifiable floor below.
        "edited": edited,
        "holding_drives": {
            k: rescan["groups"][files[k]["content"]]["drives"] for k in corrupted
        },
        # Small identified files are always verifiable (their quick hash read
        # every byte); large ones only when a full hash exists, which the spec
        # guarantees for the corrupted ones because they were chosen from
        # duplicate groups. This is therefore a floor, not an exact count.
        "min_checked": sum(1 for k in ident
                           if files[k]["size"] <= SMALL_FILE_BYTES and k not in edited)
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


# -- health and durability (ROADMAP M5.7) -------------------------------------------
#
# Independent of the tool's code: the formulas of PLAN §8.2 worked out again
# here, over the planted drive metadata. Only the *data* is shared: the rate
# and factor tables the tool ships, read straight from their JSON files.

def load_standards(source: str) -> dict:
    """The tool's shipped AFR rate tables (best first) and factor model."""
    import json
    from pathlib import Path

    folder = Path(source) / "standards" / "afr"
    rates, model = [], None
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") == "afr-rates":
            rates.append(data)
        elif data.get("kind") == "afr-model":
            model = data
    order = ("fleet", "rated", "provisional")
    rates.sort(key=lambda t: str(t["version"]), reverse=True)
    rates.sort(key=lambda t: order.index(t["basis"]) if t["basis"] in order else len(order))
    return {"rates": rates, "model": model}


def _band(value: float, ranges: list) -> float:
    for low, high, factor in ranges:
        if value >= low and (high is None or value < high):
            return float(factor)
    return 1.0


def _base_rate(model_name: str | None, media: str, standards: dict) -> tuple[float, bool]:
    """Base AFR, and whether the media class was known."""
    import re

    wanted = re.sub(r"[^A-Z0-9]", "", (model_name or "").upper())
    if len(wanted) >= 6:
        for table in standards["rates"]:
            matches = [m for m in table.get("models", [])
                       if len(re.sub(r"[^A-Z0-9]", "", m["model"].upper())) >= 6
                       and wanted.startswith(re.sub(r"[^A-Z0-9]", "", m["model"].upper()))]
            if matches:
                best = max(matches, key=lambda m: len(m["model"]))
                return float(best["afr"]), True
    for table in standards["rates"]:
        if media in table.get("classes", {}):
            return float(table["classes"][media]), media != "unknown"
    raise ValueError("no base rate for a drive")


def drive_rate(drive: dict | None, standards: dict, today) -> dict:
    """One simulated drive's AFR and confidence; ``None`` is an unverified drive."""
    import datetime as dt

    m = standards["model"]
    if drive is None:
        base, _ = _base_rate(None, "unknown", standards)
        return {"afr": min(base, m["max_afr"]), "confidence": "unknown"}
    media = drive["media"] if drive["media"] in ("hdd", "ssd") else "unknown"
    health = drive.get("health") or {}
    base, known_class = _base_rate(drive["model"], media, standards)
    rate = base
    if health.get("purchase_date"):
        year, month = (int(x) for x in health["purchase_date"].split("-"))
        years = max(0.0, (today - dt.date(year, month, 15)).days / 365.25)
        rate *= _band(years, m["age_years"][media])
    measured = bool(health) and health["smart"] != "hidden"
    if measured:
        if media == "hdd":
            rate *= _band(health["power_on_hours"], m["duty"]["hdd_power_on_hours"])
        if health["smart"] == "failing":
            s = m["smart"]
            # FAILING in vv/smart_mock.py: reallocated 48, pending 8, uncorrectable 2.
            rate *= max(s["pending"], s["uncorrectable"], s["reallocated"])
    rate *= float(m["bus"].get("usb", 1.0))          # the adapter registers every drive on USB
    confidence = ("measured" if measured else
                  "estimated" if known_class or health.get("purchase_date") else "unknown")
    return {"afr": min(rate, m["max_afr"]), "confidence": confidence}


def set_loss(devices: list[dict], standards: dict, today) -> dict:
    """P(loss) of content on exactly these devices (one dict per device)."""
    if not devices:
        r = drive_rate(None, standards, today)
        return {"p_loss": r["afr"], "correlation": 1.0}
    corr = standards["model"]["correlation"]
    rates = [drive_rate(d, standards, today)["afr"] for d in devices]
    factor = 1.0
    batches = Counter((d["manufacturer"], d["model"], d["health"]["purchase_date"][:7])
                      for d in devices if (d.get("health") or {}).get("purchase_date"))
    for size in batches.values():
        factor *= corr["same_batch"] ** (size - 1)
    zones = Counter((d.get("health") or {}).get("disaster_zone") for d in devices)
    for size in zones.values():
        factor *= corr["same_zone"] ** (size - 1)
    product = 1.0
    for rate in rates:
        product *= rate
    return {"p_loss": min(product * factor, min(rates)), "correlation": factor}


def durability(manifest: dict, phase: dict, standards: dict, today) -> dict:
    """Expected P(loss) per device set and expected bytes lost a year."""
    device, unknown = _drive_identity(manifest)
    heads: dict[str, dict] = {}
    for d in manifest["drives"]:
        if d["id"] not in unknown:
            heads.setdefault(device[d["id"]], d)

    def devices_of(holders) -> frozenset:
        return frozenset(device[h] for h in holders if h not in unknown)

    held: dict[frozenset, int] = defaultdict(int)
    for g in phase["groups"].values():
        held[devices_of(k[0] for k in g["paths"])] += g["size"]
    for key, size in phase["singles"].items():
        held[devices_of([key[0]])] += size
    sets = {}
    for devs, size in held.items():
        loss = set_loss([heads[x] for x in sorted(devs)], standards, today)
        sets[devs] = {**loss, "bytes": size}
    return {"sets": sets,
            "expected_bytes_per_year": sum(s["bytes"] * s["p_loss"] for s in sets.values())}
