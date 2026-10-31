"""Drive a Drive Fusion version and record what it concluded, in a neutral form.

This is the only module that knows how a particular Drive Fusion is invoked and
how its catalog is laid out. Everything it records is normalized to
``(drive id, path relative to the drive root)`` so the evaluator compares
*conclusions* — which paths the tool believes share content, how many drives
it says hold each — and never the tool's internal ids.

When a future version changes its API or schema, this adapter changes and the
oracle, the evaluator, and every dataset stay as they are. Another front end
(the frozen ``drivefusion.exe``, say) can be measured the same way by writing a
different adapter that emits the same ``observed.json`` shape.

Drive registration has two modes:

``simulated``  each drive root is registered as its own volume on its own
               physical drive, with the mock identity from the manifest. This
               is how one disk can stand in for twenty, exactly as the
               product's own redundancy tests do.
``physical``   each root is identified by Drive Fusion's real discovery —
               exactly what ``drivefusion scope add`` does — and the run is
               refused unless every root resolves to a different physical
               device. Otherwise the tool would be right to count two "drives"
               as one, and the oracle would be wrong.
"""

from __future__ import annotations

import datetime as dt
import importlib
import os
import sys
from collections import defaultdict
from pathlib import Path

ADAPTER_NAME = "drivefusion-python-api"


class AdapterError(RuntimeError):
    pass


class DriveFusionAdapter:
    def __init__(self, catalog_path: str, *, src: str | None = None) -> None:
        if src:
            sys.path.insert(0, os.path.abspath(src))
            for name in [m for m in sys.modules if m == "drivefusion" or m.startswith("drivefusion.")]:
                del sys.modules[name]
        df = importlib.import_module("drivefusion")
        self.version = df.__version__
        self.source = os.path.dirname(os.path.abspath(df.__file__))
        self.analysis = importlib.import_module("drivefusion.core.analysis")
        self.identity = importlib.import_module("drivefusion.core.identity")
        self.scan_mod = importlib.import_module("drivefusion.core.scan")
        self.scope = importlib.import_module("drivefusion.core.scope")
        self.discovery = importlib.import_module("drivefusion.core.discovery")
        catalog_mod = importlib.import_module("drivefusion.core.store.catalog")
        self.catalog = catalog_mod.Catalog(catalog_path)
        self.roots: dict[str, dict] = {}       # drive id -> {root, volume_id, root_id}
        self.by_volume: dict[int, str] = {}
        self.discovered: dict[str, dict] = {}
        self.health_drives: set[int] = set()
        self.smart_argvs: list[list[str]] = []

    def close(self) -> None:
        self.catalog.close()

    # -- setup ------------------------------------------------------------

    def register(self, manifest: dict) -> None:
        cat = self.catalog
        existing: list[str] = []
        seen_devices: dict[str, str] = {}
        for drive in manifest["drives"]:
            root = self.scope.validate_new_root(drive["root"], existing)
            existing.append(root)
            if manifest["mode"] == "physical":
                volume = self.discovery.identify_volume(root)
                hardware = volume.drive
                device = ((hardware.serial or getattr(hardware, "layout_id", None))
                          if hardware else None)
                if device is None:
                    raise AdapterError(
                        f"{drive['id']} ({root}): Drive Fusion can't identify this device "
                        "(no serial, no partition-table identity), so the bench can't know "
                        "the true copy counts. Use identifiable devices in physical mode; "
                        "the unidentified case (R-RED-06) is tested in simulated mode.")
                if device in seen_devices:
                    raise AdapterError(
                        f"{drive['id']} ({root}) and {seen_devices[device]} are on the same "
                        f"physical device ({device}). Physical mode needs one device per "
                        "drive; leave out --drive-path to simulate drives on one disk."
                    )
                seen_devices[device] = drive["id"]
                if hasattr(cat, "record_discovered_volume"):
                    volume_id = cat.record_discovered_volume(volume)
                else:  # versions before REVIEW F-05
                    drive_id = None
                    if hardware and hardware.serial:
                        drive_id = cat.upsert_drive(
                            serial=hardware.serial, model=hardware.model,
                            manufacturer=hardware.manufacturer, bus=hardware.bus,
                            media=hardware.media)
                    fields = volume.as_volume_fields()
                    if drive_id is not None:
                        fields["drive_id"] = drive_id
                    volume_id = cat.upsert_volume(volume_guid=volume.volume_guid, **fields)
                self.discovered[drive["id"]] = {
                    "volume_guid": volume.volume_guid, "fs_type": volume.fs_type,
                    "device": device, "label": volume.label,
                }
                file_ids = volume.supports_file_ids
            else:
                # A simulated drive's size is the one it was generated to fit,
                # and its free space what is left after the baseline dataset —
                # what discovery would have reported for a real drive this size.
                capacity = drive.get("capacity_bytes")
                free = capacity - drive.get("baseline_bytes", 0) if capacity else None
                # Partitions of one simulated device share its serial; a drive
                # behind an identity-hiding bridge gets no drive at all.
                drive_id = None
                if drive.get("identity", "serial") != "none":
                    drive_id = cat.upsert_drive(
                        serial=f"VV-{manifest['seed']}-{drive['serial']}",
                        model=drive["model"], manufacturer=drive["manufacturer"],
                        media=drive["media"], bus="usb", nickname=drive["label"],
                        capacity_bytes=capacity)
                file_ids = drive["fs"] == "NTFS"
                volume_id = cat.upsert_volume(
                    volume_guid=f"vv:{manifest['seed']}:{drive['id']}", label=drive["label"],
                    fs_type=drive["fs"], drive_id=drive_id, supports_usn=0,
                    supports_file_ids=int(file_ids), supports_hardlink=int(drive["fs"] == "NTFS"),
                    capacity_bytes=capacity, free_bytes=free,
                    cluster_bytes=drive.get("cluster_bytes"), rescan_cost="full-walk")
                device = (f"simulated:{drive['serial']}"
                          if drive.get("identity", "serial") != "none"
                          else f"unidentified:{drive['id']}")
            cat.conn.commit()
            root_id = cat.add_scope_root(volume_id, root)
            self.roots[drive["id"]] = {"root": root, "volume_id": volume_id, "root_id": root_id,
                                       "file_ids": file_ids, "device": device}
            if manifest["mode"] == "simulated" and drive_id is not None and drive.get("health"):
                self._plant_health(drive_id, drive)
            self.by_volume[volume_id] = drive["id"]

    def _plant_health(self, drive_id: int, drive: dict) -> None:
        """Record what the user would know about this simulated drive, and
        give the tool the drive's SMART output (``vv/smart_mock.py``).

        Versions before M5 have no drive metadata or SMART parser; their
        health tests are N/A.
        """
        if not hasattr(self.catalog, "update_drive") or drive_id in self.health_drives:
            return
        self.health_drives.add(drive_id)
        health = drive["health"]
        changes = {"purchase-date": health["purchase_date"]}
        if health["disaster_zone"]:
            changes["zone"] = health["disaster_zone"]
        self.catalog.update_drive(drive_id, changes)
        try:
            smart = importlib.import_module("drivefusion.core.health.smart")
        except ImportError:
            return
        from vv import smart_mock

        text = smart_mock.output(health["smart"], model=drive["model"], serial=drive["serial"],
                                 power_on_hours=health["power_on_hours"])

        def runner(exe, device, dev_type=None):
            # The command the tool would run, built by the tool itself.
            self.smart_argvs.append(smart.query_argv(exe, device, dev_type))
            return text

        smart.read_drive(self.catalog, drive_id, smartctl=Path("smartctl.exe"), runner=runner,
                         locate=lambda _c, _d: (f"/dev/vv-{drive['id']}", ""))

    # -- the tool's verbs ---------------------------------------------------

    def scan(self) -> dict:
        out = {}
        for drive, info in self.roots.items():
            result = self.scan_mod.scan_root(
                self.catalog, root_path=info["root"], root_id=info["root_id"],
                volume_id=info["volume_id"],
                excludes=self.scope.ExclusionSet.for_root(()),
                supports_file_ids=info["file_ids"])
            c = result["counters"]
            out[drive] = {"files": c.files, "dirs": c.dirs, "bytes": c.bytes,
                          "excluded": c.excluded, "links": c.reparse_skipped,
                          "unreadable": c.unreadable, "unplaced": c.unplaced,
                          "vanished": result["merged"]["vanished"],
                          "coverage": result["coverage"]}
        return out

    def hash(self) -> dict:
        c = self.identity.hash_pass(self.catalog)
        return {"candidates": c.candidates, "quick_hashed": c.quick_hashed,
                "full_hashed": c.full_hashed, "unreadable": c.unreadable,
                "summary": c.summary()}

    def propose(self, manifest: dict | None = None) -> dict | None:
        """Run collection inference (M6.2) and read back what it proposed, and
        what collections and rules exist after it. None before M6.2."""
        try:
            infer = importlib.import_module("drivefusion.core.curate.infer")
        except ImportError:
            return None
        proposals = []
        for p in infer.infer(self.catalog):
            drive, rel = self._locate(p.evidence["path"])
            e = p.evidence
            proposals.append({"drive": drive, "path": rel, "files": e["files"],
                              "bytes": e["bytes"], "date_span": e["date_span"],
                              "has_evidence": bool(e["why"]) and bool(e["samples"]),
                              "queued": p.queued})
        conn = self.catalog.conn
        found = {"proposals": proposals,
                 "collections": conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0],
                 "rules": conn.execute("SELECT COUNT(*) FROM collection_rule").fetchone()[0]}
        # Then act as a person would (M6.3): accept every proposal, so the
        # rescan can show whether files added later join by rule.
        try:
            confirm = importlib.import_module("drivefusion.core.curate.confirm")
        except ImportError:
            return found
        curation = importlib.import_module("drivefusion.core.store.curation")
        for row in curation.candidates(self.catalog):
            confirm.accept(self.catalog, row["id"])
        # And describe the planted ones as a person would (M6.10).
        plans = {(d["drive"], d["folder"]): d["fields"]
                 for sc in (manifest or {}).get("scenarios", []) for d in sc.get("describe", [])}
        for row in curation.collections(self.catalog):
            for rule in curation.rules(self.catalog, row["id"]):
                where = self._locate(self.catalog.dir_path(rule["dir_id"], os.sep))
                if where in plans:
                    curation.edit_collection(self.catalog, row["id"], plans[where])
        return found

    def scorecards(self) -> list[dict] | None:
        """Every collection's FAIR and NDSA results (M6.7). None before then."""
        try:
            scorecard = importlib.import_module("drivefusion.core.fair.scorecard")
        except ImportError:
            return None
        curation = importlib.import_module("drivefusion.core.store.curation")
        out = []
        for row in curation.collections(self.catalog):
            folders = [list(self._locate(self.catalog.dir_path(r["dir_id"], os.sep)))
                       for r in curation.rules(self.catalog, row["id"])]
            cards = {}
            for rubric_id in ("fair", "ndsa"):
                card = scorecard.score(self.catalog, row["id"], rubric_id)
                cards[rubric_id] = {
                    "rubric": card.rubric, "confirmed": card.rubric_accepted,  # checked or reviewed (D14)
                    "levels": card.levels,
                    "results": {r.criterion: [r.status, bool(r.remedy)] for r in card.results},
                    "order": [r.criterion for r in card.results]}
            out.append({"title": row["title"], "folders": folders, **cards})
        return out

    def versions(self) -> list[dict] | None:
        """Run version-set detection (M6.5) and read back each set's member
        paths and which one is preferred. None before M6.5."""
        try:
            versions = importlib.import_module("drivefusion.core.curate.versions")
        except ImportError:
            return None
        curation = importlib.import_module("drivefusion.core.store.curation")
        versions.refresh(self.catalog)
        out = []
        for found in curation.version_sets(self.catalog):
            members = []
            for m in curation.version_members(self.catalog, found["id"]):
                row = self.catalog.conn.execute(
                    "SELECT dir_id, name FROM file WHERE id = ?", (m["file_id"],)).fetchone()
                full = os.path.join(self.catalog.dir_path(row["dir_id"], os.sep), row["name"])
                members.append({"path": list(self._locate(full)),
                                "preferred": bool(m["is_preferred"])})
            out.append({"label": found["label"], "members": members})
        return out

    def plan(self) -> dict | None:
        """Make a plan (M7) as a person would, and read back its steps and units
        in manifest terms. Also probe that a step that removes is refused.
        None before M7."""
        try:
            document = importlib.import_module("drivefusion.core.planner.document")
        except ImportError:
            return None
        capacity = importlib.import_module("drivefusion.core.planner.capacity")
        plans = importlib.import_module("drivefusion.core.store.plans")
        cat = self.catalog
        doc = document.make(cat, use_cpsat=False)
        stored = document.to_json(cat, document.save(cat, doc))
        to_manifest = {d.drive_id: self.by_volume.get(d.volume_id)
                       for d in capacity.destinations(cat)}

        def where(dir_id):
            return list(self._locate(cat.dir_path(dir_id, os.sep))) if dir_id else None

        steps = [{"seq": st["seq"], "kind": st["kind"], "unit": st["unit"],
                  "src": where(st["src_dir_id"]), "dst": to_manifest.get(st["dst_drive_id"]),
                  "bytes": st["bytes"], "footprint": st["footprint_bytes"],
                  "depends_on": st["depends_on"],
                  # What the plan says this copy shares with another (D18).
                  "shares": [k for k, phrase in (("batch", "a purchase batch"),
                                                 ("zone", "a recorded zone"))
                             if st["kind"] == "copy" and "it shares" in st["rationale"]
                             and phrase in st["rationale"].split("it shares", 1)[1]]}
                 for st in stored["steps"]]
        units = [{"key": u.key, "kind": u.kind, "folder": where(u.dir_id), "target": u.target,
                  "bytes": u.bytes, "short": doc.short.get(u.key, (0, 0))[0]}
                 for u in doc.units]
        try:
            plans.create(cat, "vv probe", [plans.Step("delete", "a removal, which must fail")],
                         solver="vv", policy={}, standards={}, summary={})
            refused = False
        except plans.PlanError:
            refused = True
        return {"steps": steps, "units": units, "removal_refused": refused,
                "solver": stored["solver"]}

    def export(self, export_root: str) -> list[dict] | None:
        """Write the exports a person would (M8), into ``export_root``, and say
        which bundles were made. None before M8."""
        try:
            writer = importlib.import_module("drivefusion.core.export.writer")
        except ImportError:
            return None
        bundles = []
        bundle = writer.open_bundle(self.catalog, export_root, "bench")
        bundle.close(summary={"note": "the writer alone (M8.1)"})
        bundles.append({"kind": bundle.kind, "path": bundle.path.name})
        return bundles

    def collections(self) -> list[dict] | None:
        """Each confirmed collection's folders and members, by the tool's rules."""
        try:
            curation = importlib.import_module("drivefusion.core.store.curation")
        except ImportError:
            return None
        out = []
        for row in curation.collections(self.catalog):
            folders = [list(self._locate(self.catalog.dir_path(r["dir_id"], os.sep)))
                       for r in curation.rules(self.catalog, row["id"])]
            out.append({"title": row["title"], "folders": folders,
                        **curation.member_stats(self.catalog, row["id"])})
        return out

    def verify(self) -> dict:
        result = self.identity.verify_pass(self.catalog)
        incidents = []
        for inc in self.analysis.integrity_incidents(self.catalog, limit=100_000):
            drive, rel = self._locate(os.path.join(inc["path"], inc["name"]))
            incidents.append({"drive": drive, "path": rel, "result": inc["result"],
                              "other_copies": inc["other_copies"]})
        modified = None
        if "modified_paths" in result:          # versions with REVIEW F-06
            modified = [list(self._locate(p)) for p in result["modified_paths"]]
        return {"checked": result["checked"], "mismatched": result["mismatched"],
                "unreadable": result["unreadable"], "incidents": incidents,
                "modified": modified}

    # -- reading conclusions back ---------------------------------------------

    def _locate(self, absolute: str) -> tuple[str, str]:
        for drive, info in self.roots.items():
            try:
                rel = os.path.relpath(absolute, info["root"])
            except ValueError:  # Windows: on another drive letter entirely
                continue
            if not rel.startswith(".."):
                return drive, "" if rel == "." else rel.replace(os.sep, "/")
        return "?", absolute

    def _dir_paths(self) -> dict[int, tuple[str, str, bool]]:
        rows = list(self.catalog.conn.execute(
            "SELECT id, volume_id, parent_id, name, vanished_at FROM dir"))
        by_id = {int(r["id"]): r for r in rows}
        cache: dict[int, str] = {}

        def absolute(dir_id: int) -> str:
            if dir_id not in cache:
                row = by_id[dir_id]
                cache[dir_id] = (row["name"] if row["parent_id"] is None
                                 else os.path.join(absolute(int(row["parent_id"])), row["name"]))
            return cache[dir_id]

        out = {}
        for dir_id, row in by_id.items():
            drive, rel = self._locate(absolute(dir_id))
            out[dir_id] = (drive, rel, row["vanished_at"] is not None)
        return out

    def _group(self, g, file_keys: dict[int, list]) -> dict:
        out = {"content_id": g.content_id, "size": g.size_bytes, "paths": g.paths,
               "drives": g.drives, "physical_copies": g.physical_copies,
               "reclaimable_bytes": g.reclaimable_bytes,
               "drives_possible": getattr(g, "drives_possible", None)}
        # Versions with REVIEW F-04 report single-copy files as groups with no
        # content id; the only way to say which file is meant is its file id.
        file_id = getattr(g, "file_id", None)
        if g.content_id is None and file_id is not None:
            out["members"] = [file_keys[file_id]]
        return out

    def collect(self, min_copies: int) -> dict:
        dirs = self._dir_paths()
        files = []
        file_keys: dict[int, list] = {}
        for r in self.catalog.conn.execute(
                "SELECT id, dir_id, name, size_bytes, content_id, vanished_at, read_state FROM file"):
            drive, rel = dirs[int(r["dir_id"])][:2]
            path = f"{rel}/{r['name']}" if rel else r["name"]
            file_keys[int(r["id"])] = [drive, path]
            files.append([drive, path, int(r["size_bytes"]),
                          r["content_id"], r["vanished_at"] is not None, r["read_state"]])
        a = self.analysis
        everything = len(self.roots) + 1  # every content is "under" N+1 copies
        # Versions with REVIEW F-12 report when each volume was last scanned.
        scanned = {int(v["id"]): v["last_scanned_at"] for v in self.catalog.volumes()
                   if "last_scanned_at" in v.keys()}

        def groups(found) -> list[dict]:
            return [self._group(g, file_keys) for g in found]

        return {
            "files": files,
            "dirs": [[d, p, v] for d, p, v in dirs.values() if p],
            "groups_all": groups(a.under_protected(self.catalog, min_copies=everything)),
            "duplicates": groups(a.duplicate_groups(self.catalog)),
            "under_protected": groups(a.under_protected(self.catalog, min_copies=min_copies)),
            "reclamation": groups(a.reclamation_candidates(self.catalog)),
            "summary": a.redundancy_summary(self.catalog, min_copies=min_copies),
            "volumes": {d: {"volume_id": i["volume_id"], "device": i["device"],
                            "last_scanned_at": scanned.get(i["volume_id"]),
                            **self.discovered.get(d, {})} for d, i in self.roots.items()},
            "health": self._health(),
            "collections": self.collections(),
            "coverage": self._coverage(min_copies),
            "scorecards": self.scorecards(),
        }

    def _coverage(self, min_copies: int) -> dict | None:
        try:
            coverage = importlib.import_module("drivefusion.core.curate.coverage")
        except ImportError:
            return None
        found = coverage.coverage(self.catalog, min_copies=min_copies)
        return {"total_bytes": found.total.bytes, "filed_bytes": found.filed.bytes,
                "unfiled_bytes": found.unfiled.bytes, "filed_items": found.filed.items,
                "share": found.share}

    def _health(self) -> dict | None:
        """Each drive's recorded health and failure rate, and the durability
        figures, keyed by manifest drive ids. None before M5.5."""
        try:
            afr = importlib.import_module("drivefusion.core.health.afr")
            durability = importlib.import_module("drivefusion.core.health.durability")
            smart = importlib.import_module("drivefusion.core.health.smart")
        except ImportError:
            return None
        today = dt.date.today()
        cat = self.catalog
        drive_of = {d: cat.conn.execute("SELECT drive_id FROM volume WHERE id = ?",
                                        (i["volume_id"],)).fetchone()["drive_id"]
                    for d, i in self.roots.items()}
        names = defaultdict(list)
        for d, drive_id in drive_of.items():
            if drive_id is not None:
                names[drive_id].append(d)
        drives = {}
        for d, drive_id in drive_of.items():
            if drive_id is None:
                continue
            row = smart.latest(cat, drive_id)
            risk = afr.drive_risk(cat.drive(drive_id), today=today, health=row,
                                  rate_tables=afr.tables(cat))
            drives[d] = {
                "health": None if row is None else {
                    k: row[k] for k in ("confidence", "reallocated", "pending",
                                        "uncorrectable", "crc_errors", "power_on_hours")},
                "afr": risk.afr, "confidence": risk.confidence,
            }
        found = durability.exposure(cat, today=today)
        return {
            "today": today.isoformat(),
            "model_version": found.model_version,
            "drives": drives,
            "expected_bytes_per_year": found.expected_bytes_per_year,
            "confidence": found.confidence,
            "sets": [{"drives": sorted(n for i in s.drive_ids for n in names[i]),
                      "unverified": not s.drive_ids, "p_loss": s.p_loss,
                      "correlation": s.correlation, "bytes": s.bytes, "items": s.items}
                     for s in found.sets],
            "smart_argvs": self.smart_argvs,
        }
