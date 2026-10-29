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

import importlib
import os
import sys

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
            self.by_volume[volume_id] = drive["id"]

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
        }
