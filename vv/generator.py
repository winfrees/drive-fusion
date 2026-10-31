"""Plan a randomized dataset: drives, trees, scenarios, mutations, corruptions.

Pure planning, no I/O. The output is a manifest — the complete ground truth for
one dataset — which ``fsops.materialize`` writes to disk and ``oracle`` turns
into expectations. Everything random flows from one ``random.Random(seed)``;
the seed is fresh per dataset unless one is given, so runs differ while any
single run can be rebuilt exactly.

Each scenario is a deliberate trap for a specific requirement (see
``SCENARIO_REQUIREMENTS`` and docs/VV.md §3). Placement, names, sizes, types,
drive choice, and how many instances appear are randomized; *that* each trap
is present is not, so every dataset exercises every requirement its drives
are physically capable of.
"""

from __future__ import annotations

import datetime as dt
import random
import string
from collections import Counter

from vv import __version__, oracle
from vv.content import ALL_TYPES, BINARY_TYPES, SIZE_BANDS, category

MANIFEST_SCHEMA = "drivefusion-vv-manifest/1"

#: Which requirements each scenario kind exists to exercise.
SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "filler": ("R-CAT-01", "R-CAT-08"),
    "cross-drive-copy": ("R-ID-01", "R-RED-01"),
    "all-drives-copy": ("R-ID-01", "R-RED-01", "R-RED-03"),
    "same-drive-dupes": ("R-RED-01", "R-RED-04"),
    "mixed-dupes": ("R-RED-01", "R-RED-04"),
    "renamed-copy": ("R-ID-01",),
    "case-variant": ("R-ID-01", "R-CAT-04"),
    "same-name-diff-size": ("R-ID-02",),
    "same-name-head-diff": ("R-ID-02",),
    "same-name-tail-diff": ("R-ID-02",),
    "same-name-one-byte": ("R-ID-02",),
    "same-name-hidden-diff": ("R-ID-02",),
    "same-size-diff-name": ("R-ID-02",),
    "unique-size": ("R-ID-03", "R-RED-03"),
    "partition-copy": ("R-RED-06",),
    "unverified-copy": ("R-RED-06",),
    "empty-files": ("R-CAT-03",),
    "empty-dirs": ("R-CAT-02",),
    "hardlinks": ("R-RED-02",),
    "symlinks": ("R-SC-03",),
    "excluded": ("R-SC-02",),
    "unscoped": ("R-SC-01",),
    "odd-names": ("R-CAT-04",),
    "deep-path": ("R-CAT-05",),
    "large-copy": ("R-ID-01", "R-ID-02"),
    "version-chain": ("R-ID-02", "R-VER-01"),
    "unreadable": ("R-CAT-06",),
    "rescan-add": ("R-INC-01",),
    "rescan-add-copy": ("R-INC-01", "R-RED-01", "R-RED-03"),
    "rescan-add-large-copy": ("R-INC-01", "R-ID-01"),
    "rescan-modify": ("R-INC-01", "R-ID-02"),
    "rescan-delete": ("R-INC-02", "R-RED-03"),
    "rescan-move": ("R-INC-03",),
    "rescan-rmtree": ("R-INC-02",),
    "rescan-mkdir": ("R-CAT-02",),
    "silent-corruption": ("R-FIX-01", "R-FIX-02", "R-FIX-03"),
    "edit-before-verify": ("R-FIX-04",),
    "drive-health": ("R-HL-01", "R-HL-03", "R-HL-04"),
    "batch-copy": ("R-HL-02", "R-HL-03"),
    "project-series": ("R-COL-01", "R-COL-02", "R-COL-03", "R-COL-04", "R-COL-05",
                       "R-COL-06", "R-FAIR-01", "R-FAIR-02", "R-FAIR-03"),
    "cross-batch-copy": ("R-HL-02", "R-HL-03"),
}

MANUFACTURERS = {
    "Seagate": ("ST4000DM004", "ST2000LM015", "Expansion Desk"),
    "Western Digital": ("WD40EZAZ", "WD Elements", "My Passport"),
    "Toshiba": ("HDWD120", "Canvio Basics"),
    "Samsung": ("T7 Shield", "870 EVO"),
    "SanDisk": ("Extreme Pro USB-C", "Ultra Dual Drive"),
    "Kingston": ("DataTraveler Max", "XS2000"),
}

TOP_FOLDERS = ("Projects", "Photos", "Archive", "Instruments", "Backups", "Users",
               "Shared", "Media", "Lab Data", "Scans", "Grants", "Manuscripts")
SUB_WORDS = ("raw", "processed", "figures", "drafts", "final", "exports", "notes",
             "imaging", "calibration", "old", "misc", "review", "submission")
PEOPLE = ("alice", "bkim", "cortiz", "dpatel", "enguyen", "fsato")

ODD_NAMES = (
    "résumé – final (v2).docx", "数据分析 结果.csv", "Ünïcödé fïlé.txt",
    "notes 📓 2024.md", "data.v2.final.reviewed.csv", "README", "Makefile",
    ".hidden_settings", "file with  double  spaces.txt",
    "O'Brien & Sons #3 [draft].pdf", "100% done; really, final.txt",
    "-leading-dash.log", "ALL CAPS NAME.TXT", "Ελληνικά.json", "кириллица.xml",
)

EXCLUDED_FILE_NAMES = ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "DumpStack.log.tmp")


def _fat_time(rng: random.Random, start_year: int = 2012, end_year: int = 2026) -> int:
    """A random mtime in ns, on a 2-second boundary so FAT32 stores it exactly."""
    start = dt.datetime(start_year, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    end = dt.datetime(end_year, 6, 1, tzinfo=dt.timezone.utc).timestamp()
    seconds = int(rng.uniform(start, end)) // 2 * 2
    return seconds * 1_000_000_000


class Builder:
    """Accumulates one dataset's ground truth."""

    def __init__(self, seed: int, drives: list[dict], files_per_drive: int,
                 large_files: int, unscoped_root: str, size_scale: float = 1.0) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.drives = drives
        self.ids = [d["id"] for d in drives]
        self.files_per_drive = files_per_drive
        self.large_files = large_files
        self.unscoped_root = unscoped_root
        self.size_scale = size_scale

        self.contents: dict[str, dict] = {}
        self.files: dict[tuple[str, str], dict] = {}
        self.dirs: dict[tuple[str, str], str] = {}
        self.names: dict[tuple[str, str], set[str]] = {}
        self.general_dirs: dict[str, list[str]] = {d: [""] for d in self.ids}
        self.protected_dirs: set[tuple[str, str]] = set()
        self.links: list[dict] = []
        self.excluded = {"roots": [], "dirs": [], "files": []}
        self.unscoped: list[dict] = []
        self.mutations: list[dict] = []
        self.corruptions: list[dict] = []
        self.late_edits: list[dict] = []
        self.scenarios: list[dict] = []
        self.sizes: Counter = Counter()
        self.reserved_unique: set[int] = set()

    # -- primitives -------------------------------------------------------

    def caps(self, drive: str) -> dict:
        return next(d for d in self.drives if d["id"] == drive)["caps"]

    def scenario(self, kind: str, note: str = "") -> dict:
        record = {"id": f"S{len(self.scenarios) + 1:03d}", "kind": kind,
                  "requirements": list(SCENARIO_REQUIREMENTS[kind]),
                  "note": note, "members": []}
        self.scenarios.append(record)
        return record

    def _scaled(self, band: tuple[int, int]) -> tuple[int, int]:
        """A size band shrunk to fit the drives' capacity, keeping what it tests.

        A band that starts above the small-file threshold exists to exercise
        the tier-2 full hash, so it never shrinks below twice that threshold:
        a smaller drive gets smaller large files, never no large files.
        """
        if self.size_scale >= 1:
            return band
        lo, hi = band
        floor = 2 * oracle.SMALL_FILE_BYTES if lo > oracle.SMALL_FILE_BYTES else 1
        lo = max(floor, int(lo * self.size_scale))
        return lo, max(lo, int(hi * self.size_scale))

    def _pick_size(self, band: tuple[int, int]) -> int:
        band = self._scaled(band)
        while True:
            size = self.rng.randint(*band)
            if size not in self.reserved_unique:
                return size

    def new_content(self, ext: str | None = None, *, size: int | None = None,
                    band: tuple[int, int] | None = None, share_size: float = 0.0) -> str:
        ext = ext or self.rng.choice(ALL_TYPES)
        if size is None and self.sizes and self.rng.random() < share_size:
            # Same size, different bytes: common in real archives (fixed-size
            # instrument records, camera frames), and the case that makes the
            # quick hash, rather than the size, do the separating.
            size = self.rng.choice(sorted(s for s in self.sizes
                                          if s > 0 and s not in self.reserved_unique))
        if size is None:
            size = self._pick_size(band or SIZE_BANDS[category(ext)])
        key = f"c{len(self.contents) + 1:05d}"
        self.contents[key] = {"type": ext, "size": size, "base": None}
        self.sizes[size] += 1
        return key

    def variant(self, base: str, edits: list[dict]) -> str:
        size = self.contents[base]["size"] + sum(e.get("append", 0) for e in edits)
        key = f"c{len(self.contents) + 1:05d}"
        self.contents[key] = {"type": self.contents[base]["type"], "size": size,
                              "base": base, "edits": edits}
        self.sizes[size] += 1
        return key

    def unique_size(self, band: tuple[int, int]) -> int:
        """A size no other file in the dataset has, or ever will."""
        band = self._scaled(band)
        while True:
            size = self.rng.randint(*band)
            if self.sizes[size] == 0 and size not in self.reserved_unique:
                self.reserved_unique.add(size)
                return size

    def mkdir(self, drive: str, path: str, scenario: str, *, general: bool = False) -> str:
        parts = path.split("/")
        for i in range(len(parts)):
            sub = "/".join(parts[: i + 1])
            if (drive, sub) not in self.dirs:
                parent = "/".join(parts[:i])
                self.names.setdefault((drive, parent), set()).add(parts[i].lower())
                self.dirs[(drive, sub)] = scenario
        if general and path not in self.general_dirs[drive]:
            self.general_dirs[drive].append(path)
        return path

    def free_name(self, drive: str, directory: str, name: str) -> str:
        taken = self.names.setdefault((drive, directory), set())
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext, dot = name, "", ""
        candidate, n = name, 2
        while candidate.lower() in taken:
            candidate = f"{stem} ({n}){dot}{ext}"
            n += 1
        taken.add(candidate.lower())
        return candidate

    def make_name(self, ext: str) -> str:
        r = self.rng
        cat = category(ext)
        if cat in ("image", "raw-photo"):
            stem = r.choice(("IMG_", "DSC_", "P", "scan_")) + f"{r.randrange(10000):04d}"
            ext = ext.upper() if r.random() < 0.3 else ext
        elif cat == "video":
            stem = r.choice(("clip", "GOPR", "MVI_", "session")) + f"{r.randrange(1000):03d}"
        else:
            words = r.sample(("report", "data", "summary", "results", "notes", "plan",
                              "budget", "protocol", "run", "export", "analysis"), 2)
            stem = f"{words[0]}_{words[1]}_{r.randint(2012, 2026)}{r.randint(1, 12):02d}"
            if r.random() < 0.3:
                stem += r.choice(("_final", "_v2", "_copy", " - Copy", "_old"))
        return f"{stem}.{ext}"

    def place(self, drive: str, key: str, scenario: dict, *, directory: str | None = None,
              name: str | None = None, mtime_ns: int | None = None, **extra) -> str:
        directory = self.rng.choice(self.general_dirs[drive]) if directory is None else directory
        name = self.free_name(drive, directory, name or self.make_name(self.contents[key]["type"]))
        path = f"{directory}/{name}" if directory else name
        self.files[(drive, path)] = {
            "drive": drive, "path": path, "content": key,
            "mtime_ns": mtime_ns if mtime_ns is not None else _fat_time(self.rng),
            "scenario": scenario["id"], **extra,
        }
        scenario["members"].append([drive, path])
        return path

    def other_drives(self, drive: str) -> list[str]:
        return [d for d in self.ids if d != drive]

    def device(self, drive: str) -> str:
        return next(d for d in self.drives if d["id"] == drive).get("device", drive)

    def identified(self, drive: str) -> bool:
        return next(d for d in self.drives if d["id"] == drive).get("identity", "serial") != "none"

    # -- the tree ---------------------------------------------------------

    def skeleton(self) -> None:
        r = self.rng
        s = self.scenario("filler", "random folder tree per drive")
        for drive in self.ids:
            for top in r.sample(TOP_FOLDERS, r.randint(3, 6)):
                self.mkdir(drive, top, s["id"], general=True)
                for _ in range(r.randint(1, 4)):
                    sub = r.choice((
                        f"{r.randint(2012, 2026)}",
                        f"PRJ-{r.randrange(10000):04d}",
                        f"{r.randint(2012, 2026)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
                        r.choice(PEOPLE), r.choice(SUB_WORDS),
                    ))
                    path = self.mkdir(drive, f"{top}/{sub}", s["id"], general=True)
                    if r.random() < 0.5:
                        self.mkdir(drive, f"{path}/{r.choice(SUB_WORDS)}", s["id"], general=True)

    def filler(self) -> None:
        s = next(x for x in self.scenarios if x["kind"] == "filler")
        for drive in self.ids:
            count = max(1, int(self.files_per_drive * self.rng.uniform(0.8, 1.2)))
            for _ in range(count):
                self.place(drive, self.new_content(share_size=0.15), s)

    # -- scenarios ----------------------------------------------------------

    def cross_drive_copies(self) -> None:
        if len(self.ids) < 2:
            return
        r = self.rng
        for _ in range(r.randint(4, 8)):
            k = r.randint(2, len(self.ids))
            chosen = r.sample(self.ids, k)
            key = self.new_content()
            s = self.scenario("cross-drive-copy", f"one content on {k} drives")
            mirror = r.random() < 0.5
            mtime = _fat_time(r)
            first_dir = r.choice(self.general_dirs[chosen[0]])
            name = self.make_name(self.contents[key]["type"])
            for drive in chosen:
                if mirror:
                    self.mkdir(drive, first_dir, s["id"]) if first_dir else None
                    self.place(drive, key, s, directory=first_dir, name=name, mtime_ns=mtime)
                else:
                    self.place(drive, key, s,
                               mtime_ns=mtime if r.random() < 0.5 else None)

        if len(self.ids) >= 2:
            key = self.new_content()
            s = self.scenario("all-drives-copy", "on every drive: never under-protected at 2")
            for drive in self.ids:
                self.place(drive, key, s)

    def drive_identity_copies(self) -> None:
        """R-RED-06: content where counting volumes instead of devices goes wrong.

        On both partitions of one device and nowhere else: one copy, and the
        second is same-drive waste. On an identified drive plus a drive nothing
        identifies: one proven copy, and neither is waste.
        """
        r = self.rng
        by_device: dict[str, list[str]] = {}
        for drive in self.ids:
            by_device.setdefault(self.device(drive), []).append(drive)
        for members in by_device.values():
            if len(members) < 2:
                continue
            for _ in range(r.randint(1, 2)):
                key = self.new_content()
                s = self.scenario("partition-copy", "on two volumes of one physical device")
                for drive in members[:2]:
                    self.place(drive, key, s)

        unknown = [d for d in self.ids if not self.identified(d)]
        known = [d for d in self.ids if self.identified(d)]
        for drive in unknown:
            if not known:
                break
            key = self.new_content()
            s = self.scenario("unverified-copy", "on a known drive and an unidentified one")
            self.place(r.choice(known), key, s)
            self.place(drive, key, s)

    def project_series(self) -> None:
        """R-COL-04: a run of project folders a person would call collections.

        Three to four folders named alike with a date (``2021-03 Lake Survey``),
        each holding one file type whose dates fall within a few weeks, under a
        parent of their own. Planned last, like ``health_copies``.
        """
        r = self.rng
        drive = r.choice(self.ids)
        topic, ext = r.choice((("Lake Survey", "csv"), ("Field Plots", "csv"),
                               ("Sky Survey", "fits"), ("Interviews", "txt")))
        parent = f"Projects/{topic} series"
        year = r.randint(2014, 2024)
        s = self.scenario("project-series", f"{parent} on {drive}")
        s["folders"] = []
        first_keys: list[tuple[str, int]] = []
        for month in r.sample(range(1, 13), r.randint(3, 4)):
            folder = self.mkdir(drive, f"{parent}/{year}-{month:02d} {topic}", s["id"])
            s["folders"].append([drive, folder])
            start = dt.datetime(year, month, 2, tzinfo=dt.timezone.utc).timestamp()
            for n in range(r.randint(6, 9)):
                key = self.new_content(ext)
                when = int(start + r.uniform(0, 20 * 86400)) // 2 * 2
                self.place(drive, key, s, directory=folder,
                           name=f"{topic.split()[0].lower()}_{n:02d}.{ext}",
                           mtime_ns=when * 1_000_000_000)
                if len(s["folders"]) == 1:
                    first_keys.append((key, when))
        # R-COL-05: a file that appears in a confirmed folder between scans
        # joins its collection. Skipped if an earlier change removes the folder.
        folder = s["folders"][0][1]
        doomed = [op["path"] for op in self.mutations
                  if op["op"] in ("rmtree", "move") and op["drive"] == drive]
        if not any(folder == d or folder.startswith(d + "/") for d in doomed):
            key = self.new_content(ext)
            path = f"{folder}/{topic.split()[0].lower()}_late.{ext}"
            self.mutations.append({"op": "add", "scenario": s["id"], "drive": drive,
                                   "path": path, "content": key,
                                   "mtime_ns": (int(start) // 2 * 2) * 1_000_000_000})
            s["members"].append([drive, path])
        self._plan_scorecards(s, drive, topic, ext, first_keys)

    def _plan_scorecards(self, s: dict, drive: str, topic: str, ext: str,
                         first_keys: list) -> None:
        """R-FAIR-02/03: collections described to different degrees, and one
        backed up to another drive, so their scorecards differ in known ways.

        The adapter accepts every proposal and records ``describe`` for the
        matching folders, as a person would with ``collections edit``.
        """
        full = {"description": f"{topic}: planted for the V&V bench",
                "creators_json": '["V. V. Bench"]', "contact": "bench@example.org",
                "license": "CC-BY-4.0", "data_type": "tabular", "project": topic}
        plans = [full, {"license": "CC0-1.0"},
                 {k: full[k] for k in ("description", "creators_json", "contact")}]
        s["describe"] = [{"drive": d, "folder": f, "fields": fields}
                         for (d, f), fields in zip(s["folders"], plans)]
        others = [i for i in self.ids if self.identified(i)
                  and self.device(i) != self.device(drive)]
        if not others or not first_keys:
            return
        backup = self.rng.choice(others)
        folder = self.mkdir(backup, f"Backups/{topic} copy", s["id"])
        for n, (key, when) in enumerate(first_keys):
            self.place(backup, key, s, directory=folder,
                       name=f"{topic.split()[0].lower()}_{n:02d}.{ext}",
                       mtime_ns=when * 1_000_000_000)
        s["backup"] = [backup, folder]

    def health_copies(self) -> None:
        """R-HL-02: two copies on one purchase batch, and two across batches.

        Uses the batch ``assign_health`` planted. The pair from one batch and a
        pair from different batches each get content held on them and nowhere
        else, so the tool's P(loss) for the two can be compared.
        """
        drives = {d["id"]: d for d in self.drives}
        with_health = [i for i in self.ids if drives[i].get("health")]
        if with_health:
            kinds = ", ".join(f"{i} {drives[i]['health']['smart']}" for i in with_health)
            self.scenario("drive-health", f"purchase date, zone and SMART per device: {kinds}")
        batch, seen = [], set()
        for i in self.ids:          # one drive per device: not two partitions of one
            if drives[i].get("health", {}).get("batch") and self.device(i) not in seen:
                batch.append(i)
                seen.add(self.device(i))
        if len(batch) < 2:
            return
        key = self.new_content()
        s = self.scenario("batch-copy", "on two drives from one purchase batch")
        for drive in batch[:2]:
            self.place(drive, key, s)
        others = [i for i in self.ids if self.identified(i) and i not in batch
                  and self.device(i) not in {self.device(b) for b in batch}]
        if others:
            key = self.new_content()
            s = self.scenario("cross-batch-copy", "on two drives from different batches")
            for drive in (batch[0], others[0]):
                self.place(drive, key, s)

    def same_drive_dupes(self) -> None:
        r = self.rng
        for _ in range(r.randint(2, 4)):
            drive = r.choice(self.ids)
            key = self.new_content()
            n = r.randint(2, 4)
            s = self.scenario("same-drive-dupes", f"{n} copies on {drive} only")
            for _ in range(n):
                self.place(drive, key, s)
        if len(self.ids) >= 2:
            for _ in range(r.randint(1, 3)):
                a, b = r.sample(self.ids, 2)
                key = self.new_content()
                s = self.scenario("mixed-dupes", f"2 on {a}, 1 on {b}")
                self.place(a, key, s)
                self.place(a, key, s)
                self.place(b, key, s)

    def renamed_and_case(self) -> None:
        r = self.rng
        for _ in range(r.randint(2, 4)):
            ext = r.choice(("jpg", "pdf", "docx", "csv", "mp3"))
            key = self.new_content(ext)
            s = self.scenario("renamed-copy", "same bytes, different name and extension")
            drives = r.sample(self.ids, min(2, len(self.ids)))
            self.place(drives[0], key, s)
            alt = {"jpg": "jpeg", "pdf": "PDF", "docx": "doc", "csv": "txt", "mp3": "bak"}[ext]
            self.place(drives[-1], key, s,
                       name=f"{r.choice(('holiday', 'backup of', 'copy_', 'renamed'))} "
                            f"{r.randrange(999)}.{alt}")
        key = self.new_content("pdf")
        s = self.scenario("case-variant", "Report.PDF vs report.pdf")
        drives = r.sample(self.ids, min(2, len(self.ids)))
        stem = f"Report_{r.randrange(10000)}"
        self.place(drives[0], key, s, name=f"{stem}.PDF")
        directory = r.choice([d for d in self.general_dirs[drives[-1]] if d] or [""])
        if len(drives) == 1:
            directory = self.mkdir(drives[0], f"case-test-{r.randrange(999)}", s["id"])
        self.place(drives[-1], key, s, directory=directory, name=f"{stem.lower()}.pdf")

    def _pair_same_name(self, kind: str, a: str, b: str, note: str) -> None:
        """Two different contents at the same relative path on two drives."""
        s = self.scenario(kind, note)
        drives = self.rng.sample(self.ids, min(2, len(self.ids)))
        directory = self.rng.choice(self.general_dirs[drives[0]])
        name = self.make_name(self.contents[a]["type"])
        if len(drives) == 2:
            self.mkdir(drives[1], directory, s["id"]) if directory else None
            self.place(drives[0], a, s, directory=directory, name=name)
            self.place(drives[1], b, s, directory=directory, name=name)
        else:
            alt = self.mkdir(drives[0], f"{directory}/same name" if directory else "same name", s["id"])
            self.place(drives[0], a, s, directory=directory, name=name)
            self.place(drives[0], b, s, directory=alt, name=name)

    def same_name_different_content(self) -> None:
        r = self.rng
        for _ in range(r.randint(1, 3)):
            a = self.new_content("docx")
            b = self.new_content("docx")
            self._pair_same_name("same-name-diff-size", a, b, "same path, different size")
        for _ in range(r.randint(1, 2)):
            a = self.new_content(r.choice(("csv", "bin", "jpg")))
            b = self.variant(a, [{"flip": [r.randrange(0, 64), r.randint(1, 8)]}])
            self._pair_same_name("same-name-head-diff", a, b, "same size, first bytes differ")
        for _ in range(r.randint(1, 2)):
            a = self.new_content(r.choice(("pdf", "tif", "dat")))
            size = self.contents[a]["size"]
            b = self.variant(a, [{"flip": [size - r.randint(1, 32), 1]}])
            self._pair_same_name("same-name-tail-diff", a, b, "same size, last bytes differ")
        for _ in range(r.randint(1, 2)):
            a = self.new_content(r.choice(("txt", "json", "py")), band=(500, 150_000))
            b = self.variant(a, [{"flip": [r.randrange(self.contents[a]["size"]), 1]}])
            self._pair_same_name("same-name-one-byte", a, b, "small file, one byte differs")
        for _ in range(r.randint(1, 2)):
            # Above the small-file threshold, differing only between the head
            # block and the midpoint block: invisible to the quick hash, so
            # only the tier-2 full hash can tell these apart.
            a = self.new_content(r.choice(("h5", "mp4", "tif")), band=(700_000, 2_500_000))
            size = self.contents[a]["size"]
            offset = r.randint(oracle.SAMPLE_BYTES + 1, size // 2 - 2)
            b = self.variant(a, [{"flip": [offset, 1]}])
            self._pair_same_name("same-name-hidden-diff", a, b,
                                 f"differs only at byte {offset}, outside every quick-hash sample")
        for _ in range(r.randint(1, 3)):
            size = self._pick_size((2_000, 400_000))
            s = self.scenario("same-size-diff-name", f"two different contents of {size} bytes")
            for drive in r.sample(self.ids, min(2, len(self.ids))) * (1 if len(self.ids) > 1 else 2):
                self.place(drive, self.new_content(size=size), s)

    def unique_sizes(self) -> None:
        s = self.scenario("unique-size", "size shared with nothing: must never be hashed")
        for _ in range(self.rng.randint(2, 5)):
            drive = self.rng.choice(self.ids)
            self.place(drive, self.new_content(size=self.unique_size((1_000, 900_000))), s)

    def empties(self) -> None:
        r = self.rng
        s = self.scenario("empty-files", "zero-byte files, some with the same name")
        empty = self.new_content("txt", size=0)
        for drive in self.ids:
            for name in r.sample(("__init__.py", ".keep", "placeholder.txt", "EMPTY", "lock"),
                                 r.randint(1, 3)):
                self.place(drive, empty, s, name=name)
        s = self.scenario("empty-dirs", "empty folders and nested empty chains")
        for drive in self.ids:
            for _ in range(r.randint(1, 3)):
                parent = r.choice(self.general_dirs[drive])
                name = self.free_name(drive, parent, r.choice(("New folder", "empty", "TODO", "tmp_out")))
                path = f"{parent}/{name}" if parent else name
                if r.random() < 0.4:
                    path += "/nested/deeper"
                self.mkdir(drive, path, s["id"])
                s["members"].append([drive, path])

    def hardlinks(self) -> None:
        capable = [d for d in self.ids if self.caps(d)["hardlinks"]]
        for drive in capable[:3]:
            key = self.new_content(self.rng.choice(("bin", "h5", "zip")))
            s = self.scenario("hardlinks", f"one inode, several paths on {drive}")
            group = f"L{len(self.scenarios)}"
            mtime = _fat_time(self.rng)
            for _ in range(self.rng.randint(2, 3)):
                self.place(drive, key, s, mtime_ns=mtime, link_group=group)
            if len(self.ids) > 1 and self.rng.random() < 0.6:
                self.place(self.rng.choice(self.other_drives(drive)), key, s)

    def symlinks(self) -> None:
        for drive in [d for d in self.ids if self.caps(d)["symlinks"]][:3]:
            s = self.scenario("symlinks", f"file link, dir link and a loop on {drive}")
            targets = [p for (d, p) in self.files if d == drive and "/" in p
                       and self.files[(d, p)]["scenario"] == self.scenarios[0]["id"]]
            if not targets:
                continue
            target = self.rng.choice(targets)
            target_dir = target.rsplit("/", 1)[0]
            self.protected_dirs.add((drive, target_dir))
            home = self.mkdir(drive, f"Links-{self.rng.randrange(999)}", s["id"])
            for kind, name, tgt in (("file", "shortcut-to-file", target),
                                    ("dir", "shortcut-to-folder", target_dir),
                                    ("dir", "loop-to-self", home)):
                path = f"{home}/{name}"
                self.links.append({"drive": drive, "path": path, "target": tgt, "kind": kind})
                s["members"].append([drive, path])
            self.protected_dirs.add((drive, home))

    def excluded_entries(self) -> None:
        r = self.rng
        s = self.scenario("excluded", "default exclusions holding real-looking content")
        donors = [f["content"] for f in self.files.values() if self.contents[f["content"]]["size"]]
        for drive in self.ids:
            choices = r.sample(range(6), r.randint(3, 6))
            parent = r.choice([d for d in self.general_dirs[drive]])
            for c in choices:
                if c == 0:
                    root = "$RECYCLE.BIN"
                    inner = f"{root}/S-1-5-21-{r.randrange(10**9)}-1001"
                    self._excluded_tree(drive, root, inner, f"$R{r.randrange(10**5):05d}.docx",
                                        r.choice(donors))
                elif c == 1:
                    root = "System Volume Information"
                    self._excluded_tree(drive, root, root, "IndexerVolumeGuid", r.choice(donors))
                elif c == 2:
                    root = f".Trash-{r.choice((1000, 1001))}"
                    self._excluded_tree(drive, root, f"{root}/files", "deleted_report.pdf",
                                        r.choice(donors))
                elif c == 3:
                    name = r.choice(EXCLUDED_FILE_NAMES)
                    self._excluded_file(drive, name, r.choice(donors))
                elif c == 4:
                    name = self.free_name(drive, parent, f"~$report_{r.randrange(999)}.docx")
                    self._excluded_file(drive, f"{parent}/{name}" if parent else name,
                                        self.new_content("docx", band=(162, 162)))
                else:
                    name = self.free_name(drive, parent, f"cache_{r.randrange(10**6)}.tmp")
                    self._excluded_file(drive, f"{parent}/{name}" if parent else name,
                                        r.choice(donors))
        s["members"] = [[e["drive"], e["path"]] for e in self.excluded["roots"]]

    def _excluded_tree(self, drive: str, root: str, inner: str, name: str, key: str) -> None:
        self.excluded["roots"].append({"drive": drive, "path": root})
        self.excluded["dirs"].append({"drive": drive, "path": inner})
        self.excluded["files"].append({"drive": drive, "path": f"{inner}/{name}", "content": key})
        self.names.setdefault((drive, ""), set()).add(root.lower())

    def _excluded_file(self, drive: str, path: str, key: str) -> None:
        self.excluded["roots"].append({"drive": drive, "path": path})
        self.excluded["files"].append({"drive": drive, "path": path, "content": key})
        parent, _, name = path.rpartition("/")
        self.names.setdefault((drive, parent), set()).add(name.lower())

    def unscoped_copies(self) -> None:
        s = self.scenario("unscoped", "copies outside every scope root")
        donors = [f["content"] for f in self.files.values()]
        for i in range(self.rng.randint(2, 4)):
            key = self.rng.choice(donors)
            path = f"outside-scope/{i}-{self.make_name(self.contents[key]['type'])}"
            self.unscoped.append({"path": path, "content": key})
            s["members"].append(["unscoped", path])

    def odd_names(self) -> None:
        s = self.scenario("odd-names", "unicode, punctuation, dots, no extension, long names")
        r = self.rng
        long_name = "long_" + "".join(r.choices(string.ascii_lowercase, k=190)) + ".txt"
        for name in r.sample(ODD_NAMES, r.randint(6, len(ODD_NAMES))) + [long_name]:
            ext = name.rpartition(".")[2].lower() if "." in name.strip(".") else "bin"
            ext = ext if ext in ALL_TYPES else "txt"
            self.place(r.choice(self.ids), self.new_content(ext), s, name=name)

    def deep_path(self) -> None:
        s = self.scenario("deep-path", "full path beyond 260 characters")
        drive = self.rng.choice(self.ids)
        path = "Deep"
        while len(path) < 280:
            path += f"/level-{len(path.split('/')):02d}-" + "".join(
                self.rng.choices(string.ascii_lowercase, k=24))
        self.mkdir(drive, path, s["id"])
        key = self.new_content("txt")
        self.place(drive, key, s, directory=path, name="bottom-of-a-very-deep-tree.txt")
        if len(self.ids) > 1:
            self.place(self.rng.choice(self.other_drives(drive)), key, s)

    def large_copies(self) -> None:
        r = self.rng
        for _ in range(self.large_files):
            key = self.new_content(r.choice(("mp4", "mov", "h5", "zip", "7z")),
                                   band=(1_500_000, 6_000_000))
            s = self.scenario("large-copy", "multi-megabyte file, full-hash path")
            for drive in r.sample(self.ids, min(len(self.ids), r.randint(2, 3))) or self.ids:
                self.place(drive, key, s)
            if len(self.ids) == 1:
                self.place(self.ids[0], key, s)

    def versions(self) -> None:
        r = self.rng
        s = self.scenario("version-chain", "v1, v2 = v1 + appended, v3 = v2 with an edit")
        drive = r.choice(self.ids)
        directory = self.mkdir(drive, f"{r.choice(self.general_dirs[drive]) or 'Manuscripts'}/"
                                      f"versions-{r.randrange(999)}".lstrip("/"), s["id"])
        v1 = self.new_content("docx", band=(20_000, 150_000))
        v2 = self.variant(v1, [{"append": r.randint(100, 5_000)}])
        size2 = self.contents[v2]["size"]
        v3 = self.variant(v2, [{"flip": [r.randrange(size2), 2]}])
        stem = r.choice(("manuscript", "thesis", "grant_aims"))
        for i, key in enumerate((v1, v2, v3), start=1):
            self.place(drive, key, s, directory=directory, name=f"{stem}_v{i}.docx")

    def unreadable(self) -> None:
        capable = [d for d in self.ids if self.caps(d)["unreadable"]]
        if not capable:
            return
        s = self.scenario("unreadable", "permission denied on read")
        drive = self.rng.choice(capable)
        key = self.new_content("pdf", band=(5_000, 100_000))
        self.place(drive, key, s, unreadable=True)
        # A readable twin shares its size, so the hash pass will try to open
        # the unreadable one: that is the path where an error could be fatal.
        self.place(self.rng.choice(self.ids), key, s)

    # -- between scans --------------------------------------------------------

    def _mutable(self, drive: str | None = None, *, min_size: int = 1) -> list[tuple[str, str]]:
        filler = self.scenarios[0]["id"]
        return [k for k, f in self.files.items()
                if f["scenario"] == filler and not f.get("link_group") and not f.get("unreadable")
                and (drive is None or k[0] == drive)
                and self.contents[f["content"]]["size"] >= min_size
                and k[1].rpartition("/")[0] not in {p for (d, p) in self.protected_dirs if d == k[0]}]

    def _copy_holders(self, min_paths: int = 2, large: bool | None = None) -> dict[str, list]:
        holders: dict[str, list] = {}
        for k, f in self.files.items():
            # Empty files are never hashed, so they have no identity a copy
            # set, a move, or a corruption could be judged by.
            if f.get("link_group") or f.get("unreadable") or not self.contents[f["content"]]["size"]:
                continue
            holders.setdefault(f["content"], []).append(k)
        return {c: ks for c, ks in holders.items() if len(ks) >= min_paths
                and (large is None or (self.contents[c]["size"] > oracle.SMALL_FILE_BYTES) == large)}

    def mutations_plan(self) -> None:
        r = self.rng
        touched: set[tuple[str, str]] = set()

        def op(kind: str, scenario: dict, **fields) -> None:
            self.mutations.append({"op": kind, "scenario": scenario["id"], **fields})
            scenario["members"].append([fields["drive"], fields.get("to", fields["path"])])
            touched.add((fields["drive"], fields["path"]))

        s = self.scenario("rescan-add", "new files with new content")
        for _ in range(r.randint(2, 5)):
            drive = r.choice(self.ids)
            key = self.new_content()
            directory = r.choice(self.general_dirs[drive])
            name = self.free_name(drive, directory, self.make_name(self.contents[key]["type"]))
            op("add", s, drive=drive, path=f"{directory}/{name}".lstrip("/"),
               content=key, mtime_ns=_fat_time(r, 2026))

        if len(self.ids) >= 2:
            # Promote a single-drive file to two drives: it must leave the
            # under-protected list.
            singles = [k for k in self._mutable(min_size=1)
                       if sum(1 for f in self.files.values()
                              if f["content"] == self.files[k]["content"]) == 1]
            if singles:
                src = r.choice(singles)
                s = self.scenario("rescan-add-copy", "single-drive content gains a second drive")
                drive = r.choice(self.other_drives(src[0]))
                directory = r.choice(self.general_dirs[drive])
                name = self.free_name(drive, directory, src[1].rpartition("/")[2])
                op("add", s, drive=drive, path=f"{directory}/{name}".lstrip("/"),
                   content=self.files[src]["content"], mtime_ns=self.files[src]["mtime_ns"])

        large = self._copy_holders(2, large=True)
        if large:
            # A new copy of large content that was already fully hashed. The
            # new file's identity has to meet the existing group's.
            content = r.choice(sorted(large))
            s = self.scenario("rescan-add-large-copy",
                              "new copy of large, already-verified content")
            drive = r.choice(self.ids)
            directory = r.choice(self.general_dirs[drive])
            name = self.free_name(drive, directory, f"copy of {self.make_name(self.contents[content]['type'])}")
            op("add", s, drive=drive, path=f"{directory}/{name}".lstrip("/"),
               content=content, mtime_ns=_fat_time(r, 2026))

        candidates = self._mutable(min_size=200)
        r.shuffle(candidates)
        s = self.scenario("rescan-modify", "edited in place: new bytes and a new mtime")
        for k in candidates[:2]:
            base = self.files[k]["content"]
            size = self.contents[base]["size"]
            new = (self.variant(base, [{"flip": [r.randrange(size), 4]}]) if r.random() < 0.5
                   else self.variant(base, [{"append": r.randint(10, 2_000)}]))
            op("modify", s, drive=k[0], path=k[1], content=new,
               mtime_ns=self.files[k]["mtime_ns"] + r.randint(1, 400) * 86_400 * 10**9)

        s = self.scenario("rescan-delete", "deleted between scans")
        # Drop one of exactly two cross-drive copies: the content must join
        # the under-protected list on the rescan.
        link_targets = {(l["drive"], l["target"]) for l in self.links}
        two_drive = [c for c, ks in self._copy_holders(2).items()
                     if len(ks) == 2 and len({k[0] for k in ks}) == 2
                     and not any(k in touched or k in link_targets for k in ks)]
        if two_drive:
            content = r.choice(sorted(two_drive))
            victim = r.choice(self._copy_holders(2)[content])
            op("delete", s, drive=victim[0], path=victim[1])
        for k in [k for k in candidates[2:] if k not in touched][: r.randint(1, 3)]:
            op("delete", s, drive=k[0], path=k[1])

        holders = [ks for ks in self._copy_holders(2).values()
                   if not any(k in touched for k in ks)]
        if holders:
            s = self.scenario("rescan-move", "moved and renamed on the same drive")
            k = r.choice(r.choice(holders))
            drive = k[0]
            # Not mkdir'd: the folder comes into existence with the move, so it
            # is part of the rescan's tree and not the baseline's.
            directory = f"Reorganized-{r.randrange(10**4)}/{r.choice(SUB_WORDS)}"
            name = self.free_name(drive, directory, f"renamed-{k[1].rpartition('/')[2]}")
            op("move", s, drive=drive, path=k[1], to=f"{directory}/{name}")

        s = self.scenario("rescan-rmtree", "a whole folder deleted between scans")
        for drive in r.sample(self.ids, len(self.ids)):
            options = []
            mutable = set(self._mutable(drive))
            for (d, p) in self.dirs:
                if d != drive or "/" not in p or (d, p) in self.protected_dirs:
                    continue
                inside_files = [k for k in self.files if k[0] == d and k[1].startswith(p + "/")]
                inside_dirs = [q for (e, q) in self.dirs if e == d and q.startswith(p + "/")]
                if (inside_files and all(k in mutable and k not in touched for k in inside_files)
                        and not any(e["drive"] == d and e["path"].startswith(p + "/")
                                    for e in self.excluded["roots"])
                        and not any((d, q) in self.protected_dirs for q in inside_dirs)
                        and not any(l["drive"] == d and (l["target"] == p or l["target"].startswith(p + "/"))
                                    for l in self.links)
                        and not any(m["drive"] == d and m.get("to", m["path"]).startswith(p + "/")
                                    for m in self.mutations)):
                    options.append(p)
            if options:
                op("rmtree", s, drive=drive, path=r.choice(sorted(options)))
                break

        s = self.scenario("rescan-mkdir", "a new empty folder")
        drive = r.choice(self.ids)
        # Not inside a deleted subtree: creating it there would bring the
        # deleted folders back into existence.
        removed = [m["path"] for m in self.mutations if m["op"] == "rmtree" and m["drive"] == drive]
        parent = r.choice([p for p in self.general_dirs[drive]
                           if not any(p == x or p.startswith(x + "/") for x in removed)])
        name = self.free_name(drive, parent, "New folder (rescan)")
        op("mkdir", s, drive=drive, path=f"{parent}/{name}".lstrip("/"))

    def corruption_plan(self, manifest: dict) -> None:
        """Pick verifiable files whose content is unaffected by the mutations."""
        exp = oracle.expectations(manifest)
        base, rescan = exp["baseline"], exp["rescan"]
        stable = [
            c for c, g in base["groups"].items()
            if len(g["paths"]) >= 2 and c in rescan["groups"]
            and rescan["groups"][c]["paths"] == g["paths"]
            and all(not base["files"][k]["link_group"] for k in g["paths"])
        ]
        small = [c for c in stable if base["groups"][c]["size"] <= oracle.SMALL_FILE_BYTES]
        large = [c for c in stable if base["groups"][c]["size"] > oracle.SMALL_FILE_BYTES]
        picks = []
        for pool in (small, large):
            if pool:
                picks.append(self.rng.choice(sorted(pool)))
        extra = [c for c in stable if c not in picks]
        picks += self.rng.sample(sorted(extra), min(len(extra), self.rng.randint(0, 2)))
        if not picks:
            return
        s = self.scenario("silent-corruption", "bytes flipped, size and mtime preserved")
        for c in picks:
            drive, path = self.rng.choice(base["groups"][c]["paths"])
            size = base["groups"][c]["size"]
            self.corruptions.append({"drive": drive, "path": path,
                                     "offset": self.rng.randrange(size)})
            s["members"].append([drive, path])

        # R-FIX-04: an ordinary edit after the rescan, with a new date. Not
        # corruption, and must not be reported as such.
        others = [c for c in stable if c not in picks]
        if not others:
            return
        s = self.scenario("edit-before-verify", "edited after the rescan; the date moves on")
        for c in self.rng.sample(sorted(others), min(len(others), self.rng.randint(1, 2))):
            drive, path = self.rng.choice(base["groups"][c]["paths"])
            self.late_edits.append({"drive": drive, "path": path,
                                    "offset": self.rng.randrange(base["groups"][c]["size"])})
            s["members"].append([drive, path])

    # -- assembly -----------------------------------------------------------

    def manifest(self, mode: str, params: dict) -> dict:
        return {
            "schema": MANIFEST_SCHEMA,
            "generator": __version__,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "seed": self.seed,
            "mode": mode,
            "params": params,
            "drives": self.drives,
            "unscoped_root": self.unscoped_root,
            "contents": self.contents,
            "files": sorted(self.files.values(), key=lambda f: (f["drive"], f["path"])),
            "dirs": [{"drive": d, "path": p, "scenario": s}
                     for (d, p), s in sorted(self.dirs.items())],
            "links": self.links,
            "excluded": self.excluded,
            "unscoped": self.unscoped,
            "mutations": self.mutations,
            "corruptions": self.corruptions,
            "late_edits": self.late_edits,
            "scenarios": self.scenarios,
        }


def mock_drive_identity(rng: random.Random, index: int, fs: str) -> dict:
    maker = rng.choice(sorted(MANUFACTURERS))
    return {
        "id": f"D{index:02d}",
        "label": f"VV-D{index:02d}",
        "fs": fs,
        "manufacturer": maker,
        "model": rng.choice(MANUFACTURERS[maker]),
        "serial": "".join(rng.choices(string.ascii_uppercase + string.digits, k=12)),
        "media": rng.choice(("hdd", "ssd", "flash")),
    }


def assign_devices(drives: list[dict], seed: int) -> None:
    """Give simulated drives the identity troubles real ones have (R-RED-06).

    With three or more drives, two of them become partitions of one physical
    device: same device, same serial. With four or more, one other drive sits
    behind a USB bridge that reports no identity at all. A separate random
    stream keeps every other choice the seed makes unchanged.
    """
    rng = random.Random(seed ^ 0x5EED_DE71CE)
    for drive in drives:
        drive.setdefault("device", drive["id"])
        drive.setdefault("identity", "serial")
    if len(drives) >= 3:
        first, second = rng.sample(drives, 2)
        for field in ("device", "serial", "manufacturer", "model", "media"):
            second[field] = first[field]
    if len(drives) >= 4:
        shared = {d["id"] for d in drives if d["device"] != d["id"]} | {
            d["device"] for d in drives if d["device"] != d["id"]}
        candidates = [d for d in drives if d["id"] not in shared]
        rng.choice(candidates)["identity"] = "none"


#: What a simulated drive's SMART says: healthy, failing (pending and
#: reallocated sectors), or nothing at all, as behind many USB bridges.
SMART_KINDS = ("healthy", "failing", "hidden")

#: Disaster zones a simulated drive can be kept in; None is "not recorded".
ZONES = ("home", "office", "offsite", None)


def assign_health(drives: list[dict], seed: int) -> None:
    """Give each simulated device what M5 scores it on (ROADMAP M5.7).

    A purchase month, a disaster zone (or none recorded) and what its SMART
    would say. With two or more identified devices, two of them are made one
    purchase batch: same maker, model and purchase month. Partitions of one
    device share all of it. Drives nothing identifies get nothing: the tool
    has no drive row to record it on. A separate random stream keeps every
    other choice the seed makes unchanged.
    """
    rng = random.Random(seed ^ 0x4EA1_7B47)
    devices: dict[str, dict] = {}
    for drive in drives:
        if drive.get("identity", "serial") == "none":
            continue
        device = drive.get("device", drive["id"])
        if device not in devices:
            devices[device] = {
                "purchase_date": f"{rng.randint(2014, 2025)}-{rng.randint(1, 12):02d}",
                "disaster_zone": rng.choice(ZONES),
                "smart": rng.choice(SMART_KINDS),
                "power_on_hours": rng.randint(500, 60_000),
            }
    order = sorted(devices)
    if len(order) >= 2:
        first, second = rng.sample(order, 2)
        devices[second]["purchase_date"] = devices[first]["purchase_date"]
        devices[first]["batch"] = devices[second]["batch"] = True
        heads = {d.get("device", d["id"]): d for d in drives if d.get("identity") != "none"}
        for field in ("manufacturer", "model"):
            for drive in drives:
                if drive.get("device", drive["id"]) == second:
                    drive[field] = heads[first][field]
    for drive in drives:
        info = devices.get(drive.get("device", drive["id"]))
        if info is not None:
            drive["health"] = dict(info)


def default_cluster_bytes(fs: str, capacity: int) -> int:
    """Windows' default cluster size for a volume of this filesystem and size.

    Gives a simulated drive the cluster slack a real one would have: on a large
    exFAT drive a 1 KB file still occupies 128 KiB (PLAN §6.7).
    """
    if fs == "exFAT":
        if capacity <= 256 * 2**20:
            return 4096
        return 32 * 1024 if capacity <= 32 * 2**30 else 128 * 1024
    return 4096


def _on_disk(size: int, cluster: int) -> int:
    return -(-size // cluster) * cluster if size else 0


def footprints(manifest: dict, *, host_cluster: int = 4096) -> dict[str, dict]:
    """Bytes each drive will occupy, in its own clusters and in the host's.

    ``capacity_peak`` is what a real drive of the simulated filesystem would
    need, cluster slack included: that is what must fit the chosen size.
    ``host_peak`` is what the folder will actually consume on the disk hosting
    it. Both are peaks over the run: between scans files are added and grown
    before anything is deleted, so they are counted on top of the baseline.
    """
    sizes = manifest["contents"]
    out = {}
    for d in manifest["drives"]:
        cluster = d.get("cluster_bytes") or host_cluster
        seen_links: set[str] = set()
        files: list[int] = []
        for f in manifest["files"]:
            if f["drive"] != d["id"]:
                continue
            group = f.get("link_group")
            if group:
                if group in seen_links:
                    continue
                seen_links.add(group)
            files.append(sizes[f["content"]]["size"])
        files += [sizes[f["content"]]["size"] for f in manifest["excluded"]["files"]
                  if f["drive"] == d["id"]]
        dirs = sum(1 for x in manifest["dirs"] + manifest["excluded"]["dirs"]
                   if x["drive"] == d["id"])
        current = {f["path"]: sizes[f["content"]]["size"] for f in manifest["files"]
                   if f["drive"] == d["id"]}
        growth: list[int] = []
        for op in manifest["mutations"]:
            if op["drive"] != d["id"]:
                continue
            if op["op"] == "add":
                growth.append(sizes[op["content"]]["size"])
            elif op["op"] == "modify":
                growth.append(max(0, sizes[op["content"]]["size"] - current.get(op["path"], 0)))
            elif op["op"] == "mkdir":
                dirs += 1

        def total(c: int) -> int:
            return sum(_on_disk(s, c) for s in files + growth) + (dirs + 1) * c

        out[d["id"]] = {"capacity_peak": total(cluster), "host_peak": total(host_cluster),
                        "baseline": sum(_on_disk(s, cluster) for s in files) + (dirs + 1) * cluster}
    return out


def build(seed: int, drives: list[dict], *, files_per_drive: int, large_files: int,
          unscoped_root: str, mode: str, params: dict, size_scale: float = 1.0) -> dict:
    """Plan one dataset. ``drives`` must already carry ``root`` and ``caps``."""
    b = Builder(seed, drives, files_per_drive, large_files, unscoped_root, size_scale)
    b.skeleton()
    b.filler()
    b.cross_drive_copies()
    b.drive_identity_copies()
    b.same_drive_dupes()
    b.renamed_and_case()
    b.same_name_different_content()
    b.unique_sizes()
    b.empties()
    b.hardlinks()
    b.symlinks()
    b.odd_names()
    b.deep_path()
    b.large_copies()
    b.versions()
    b.unreadable()
    b.excluded_entries()
    b.unscoped_copies()
    b.mutations_plan()
    b.corruption_plan(b.manifest(mode, params))
    # Last, so every earlier random draw, and so the rest of the dataset, is
    # what it was before M5.7 for the same seed.
    b.health_copies()
    b.project_series()
    return b.manifest(mode, params)


#: Each refit shrinks file sizes by this factor; below MIN_SCALE the file
#: count alone, not file size, is what no longer fits.
REFIT_FACTOR = 0.85
MIN_SCALE = 0.01


class CapacityError(ValueError):
    """The chosen drive size cannot hold even a minimal dataset."""


def build_fit(seed: int, drives: list[dict], *, files_per_drive: int, large_files: int,
              unscoped_root: str, mode: str, params: dict) -> dict:
    """Plan a dataset whose every drive fits the capacity it was given.

    Drives without a ``capacity_bytes`` are unconstrained. Otherwise file
    sizes are scaled down until the peak footprint — cluster slack and the
    files added between scans included — fits every drive. The plan is a
    pure function of seed and parameters, so the same command reproduces the
    same dataset, scale and all.
    """
    scale = 1.0
    while True:
        manifest = build(seed, drives, files_per_drive=files_per_drive,
                         large_files=large_files, unscoped_root=unscoped_root,
                         mode=mode, params={**params, "size_scale": scale},
                         size_scale=scale)
        fits = footprints(manifest)
        over = {d["id"]: fits[d["id"]]["capacity_peak"] for d in drives
                if d.get("capacity_bytes") and fits[d["id"]]["capacity_peak"] > d["capacity_bytes"]}
        if not over:
            for d in drives:
                d["baseline_bytes"] = fits[d["id"]]["baseline"]
            return manifest
        if scale * REFIT_FACTOR < MIN_SCALE:
            worst = max(over, key=over.get)
            raise CapacityError(
                f"{worst} needs at least ~{over[worst] / 1e6:,.0f} MB even with every file "
                "shrunk to a minimum; the number of files and folders, each taking at least "
                "one cluster, is what does not fit. Use a larger --drive-size or a smaller "
                "--files-per-drive.")
        scale *= REFIT_FACTOR
