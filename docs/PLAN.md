# Drive Fusion — Build Plan

A cross-platform desktop tool that catalogs every drive you own (internal, external,
connected or sitting on a shelf), proves how many real copies of each byte exist,
and produces **reviewable, resumable, safe plans** for consolidating that content into
a traceable order.

---

## 1. The problem, stated precisely

You have:

| Symbol | Quantity | What it drives |
|---|---|---|
| `n` | drives, each of capacity `cap_d` | the bin-packing constraint |
| `m` | copies and/or near-copies (versions) of a file | the dedup / redundancy question |
| `l` | manufacturers | **correlated** failure risk — 3 copies on 3 drives from one bad batch is not 3 copies |
| `p` | purchase dates | age → failure rate, and warranty status |
| `o` | usage (power-on hours, writes, duty cycle) | wear → failure rate |

Three use cases, in dependency order:

1. **Catalog** — know what exists, where, on which physical device, even when that
   device is unplugged.
2. **Confirm redundancy** — for every distinct piece of *content*, how many copies exist,
   on how many **distinct physical drives**, and what is the probability all of them
   die in the next year.
3. **Plan the merge** — produce an ordered, verifiable set of operations that
   consolidates duplicates, satisfies a redundancy policy, fits the capacity you own,
   and leaves the result findable by a human.

The unifying metric that ties `n, m, l, p, o` together is **expected bytes lost per year**.
Every plan is scored against it. That single number is what turns "I have lots of copies"
into "I have the *right* copies in the *right* places."

---

## 2. Design decisions (locked in)

These are chosen, not open questions. Rationale is given so they can be revisited
deliberately rather than by drift.

| Decision | Choice | Why |
|---|---|---|
| Language / runtime | Python 3.11+ | Requested; `os.scandir`, `hashlib`, and modern typing are all we need. |
| GUI | **PySide6 (Qt 6)** | Qt's model/view handles million-row tables with lazy paging; native look on Win/macOS/Linux; LGPL; packages cleanly with PyInstaller. Tkinter cannot render this UI credibly; Electron/webview adds a second runtime. |
| Catalog store | **SQLite** (WAL) + **FTS5** for path search | Single file, zero-install, transactional, ships with CPython, survives being carried on a USB stick. Handles 10M+ rows fine with the right indices. |
| ORM | none — hand-written SQL in a `store/` layer | Query shapes here are unusual (group-by-content, capacity rollups); an ORM would be pure overhead and would slow bulk inserts. |
| Hashing | `blake3` if installed, else `hashlib.blake2b` | BLAKE3 is ~5–10× faster than SHA-256 and multithreads; blake2b keeps the app dependency-free as a fallback. Algorithm is stored per-row so both can coexist. |
| Executable | **PyInstaller, one-folder** per OS | One-file mode unpacks to temp on every launch (slow, and antivirus-hostile). One-folder starts fast and is trivially zippable. |
| Architecture | `core` (no GUI) → `cli` + `gui` | The engine must be testable headlessly and scriptable in cron; the GUI is one of two front ends, never the place logic lives. |
| Optimizer | greedy + local search by default; **OR-Tools CP-SAT** when installed | Greedy always available and explainable; CP-SAT gives provably better packings on the mid-size instances that matter. |
| Deletion policy | copy → verify → **quarantine** → expire | Nothing is ever `unlink`ed as part of a plan. Deletions move to a per-drive quarantine with a retention window and are reversible until it lapses. |

---

## 3. Architecture

```
drivefusion/
├── core/
│   ├── model.py          # dataclasses: Drive, Volume, FileRecord, Content, Plan, Op
│   ├── store/            # SQLite schema, migrations, DAOs, FTS index
│   ├── discovery/        # enumerate physical drives + volumes per OS
│   │   ├── linux.py      #   lsblk --json, /sys/block, blkid
│   │   ├── darwin.py     #   diskutil list -plist, system_profiler SPStorageDataType
│   │   └── windows.py    #   PowerShell Get-PhysicalDisk / Get-Volume (JSON out)
│   ├── health/           # smartctl --json parsing, risk scoring, AFR tables
│   ├── scan/             # walker, stat capture, hashing pipeline, resume
│   ├── identity/         # content identity, quick-hash, hardlink & clone handling
│   ├── analysis/         # duplicate groups, version sets, redundancy report
│   ├── policy/           # redundancy rules, keep-rules, naming templates
│   ├── planner/          # placement solver, op graph, simulation
│   ├── executor/         # journaled op runner, verify, quarantine, rollback
│   └── report/           # exports: CSV/JSON/HTML, per-drive manifests
├── cli/                  # `drivefusion scan|report|plan|apply|verify`
├── gui/                  # PySide6 views + Qt worker threads
└── packaging/            # .spec files, icons, CI build matrix
```

**Threading rule:** the GUI thread never does I/O. Every scan, hash, plan, and copy runs
in a `QThread` worker that reports progress via signals; the DB is accessed from a single
writer thread with a queue, readers use their own connections in WAL mode.

---

## 4. Data model

Abbreviated DDL; the shape is the point.

```sql
-- A physical device. Survives being unplugged, reformatted, or re-lettered.
CREATE TABLE drive (
  id INTEGER PRIMARY KEY,
  serial TEXT UNIQUE,            -- identity anchor
  wwn TEXT, model TEXT, manufacturer TEXT,
  bus TEXT,                      -- sata|usb|nvme|thunderbolt
  media TEXT,                    -- hdd|ssd|flash|optical
  capacity_bytes INTEGER,
  nickname TEXT,                 -- "Shelf B, blue Seagate"
  purchase_date TEXT, purchase_price_cents INTEGER, vendor TEXT,
  warranty_end TEXT,
  batch_key TEXT,                -- manufacturer+model+purchase month → correlation group
  role TEXT,                     -- primary|replica|archive|scratch|offsite
  location TEXT,                 -- desk|shelf|safe-deposit|friend's house
  retired_at TEXT, notes TEXT
);

-- A filesystem on a drive. One drive may hold several.
CREATE TABLE volume (
  id INTEGER PRIMARY KEY,
  drive_id INTEGER REFERENCES drive(id),
  fs_uuid TEXT UNIQUE, label TEXT, fs_type TEXT,
  capacity_bytes INTEGER, free_bytes INTEGER,
  last_mount_point TEXT, last_seen_at TEXT,
  case_sensitive INTEGER, supports_hardlink INTEGER, supports_clone INTEGER
);

CREATE TABLE scan (
  id INTEGER PRIMARY KEY, volume_id INTEGER, root TEXT,
  started_at TEXT, finished_at TEXT, status TEXT,      -- running|done|aborted
  files_seen INTEGER, bytes_seen INTEGER, hashed_bytes INTEGER,
  tool_version TEXT
);

-- Distinct content, addressed by hash. This is what redundancy is counted over.
CREATE TABLE content (
  id INTEGER PRIMARY KEY,
  size_bytes INTEGER NOT NULL,
  quick_hash BLOB NOT NULL,      -- size + head 64K + tail 64K + midpoint
  full_hash BLOB,                -- NULL until confirmed
  hash_algo TEXT,                -- blake3|blake2b
  media_kind TEXT,               -- image|audio|video|doc|archive|other
  UNIQUE(size_bytes, quick_hash, full_hash)
);

-- A path. Many paths → one content.
CREATE TABLE file (
  id INTEGER PRIMARY KEY,
  volume_id INTEGER, content_id INTEGER,
  dir_path TEXT, name TEXT, ext TEXT,
  size_bytes INTEGER, mtime_ns INTEGER,
  inode INTEGER, nlink INTEGER, is_symlink INTEGER,
  first_seen_scan INTEGER, last_seen_scan INTEGER,
  vanished_at TEXT               -- set when a later scan no longer finds it
);
CREATE INDEX ix_file_content ON file(content_id);
CREATE INDEX ix_file_vol_dir ON file(volume_id, dir_path);
CREATE VIRTUAL TABLE file_fts USING fts5(name, dir_path, content='file', content_rowid='id');

-- Near-duplicates: same photo re-encoded, doc v1/v2/final-FINAL.
CREATE TABLE similarity (content_id INTEGER, kind TEXT, hash BLOB);  -- dhash/pHash/audio/ssdeep
CREATE TABLE version_set (id INTEGER PRIMARY KEY, label TEXT, method TEXT, confidence REAL);
CREATE TABLE version_member (version_set_id INTEGER, content_id INTEGER, is_preferred INTEGER);

-- Health over time → the `o` axis.
CREATE TABLE drive_health (
  drive_id INTEGER, observed_at TEXT,
  power_on_hours INTEGER, temperature_c INTEGER,
  reallocated INTEGER, pending INTEGER, uncorrectable INTEGER, crc_errors INTEGER,
  wear_leveling INTEGER, tbw_written INTEGER,
  self_test_status TEXT, smart_json TEXT,
  annual_failure_prob REAL       -- computed, see §6
);

-- Plans are data, not scripts.
CREATE TABLE plan (id INTEGER PRIMARY KEY, created_at TEXT, name TEXT,
                   policy_snapshot TEXT, objective_json TEXT, status TEXT);
CREATE TABLE plan_op (
  id INTEGER PRIMARY KEY, plan_id INTEGER, seq INTEGER,
  kind TEXT,                     -- copy|verify|hardlink|move|quarantine|expire|mkdir|manifest
  src_volume_id INTEGER, src_path TEXT,
  dst_volume_id INTEGER, dst_path TEXT,
  content_id INTEGER, bytes INTEGER,
  depends_on TEXT,               -- JSON list of plan_op.id
  state TEXT,                    -- pending|running|done|failed|skipped|rolled_back
  attempts INTEGER, error TEXT, started_at TEXT, finished_at TEXT
);
```

**Identity notes that matter in practice**

- A drive is identified by `serial`, a volume by `fs_uuid` — never by mount point or
  drive letter, which change between sessions.
- Hardlinks: files sharing `(volume_id, inode)` are one physical copy. Counting them as
  two would inflate the redundancy count and is a correctness bug, not a cosmetic one.
- APFS/Btrfs/ReFS clones look like independent files but share blocks; they count as
  one copy for *durability* and as separate files for *space accounting*. Flag them.
- Windows: open paths with the `\\?\` prefix to survive >260-char paths.
- Case-insensitive volumes: normalize for matching, store as observed.

---

## 5. Scanning and content identity

Three-tier hashing keeps a multi-terabyte scan tractable:

1. **Tier 0 — stat only.** `os.scandir` gives name, size, mtime, inode without extra syscalls.
   If `(volume, dir, name, size, mtime_ns, inode)` matches the previous scan, reuse the
   existing `content_id`. Re-scans of an unchanged drive are then a metadata walk: minutes, not hours.
2. **Tier 1 — quick hash.** Only for sizes that appear more than once anywhere in the
   catalog. Hash size + first 64 KiB + last 64 KiB + a midpoint block. Unique size ⇒ unique
   content ⇒ never read the body at all.
3. **Tier 2 — full hash.** Only for files whose quick hash collides with another file.
   This is the only tier that reads whole files, and it runs on a small fraction of bytes.

Concurrency is per-device, because the bottleneck is physical: **1–2 readers for a spinning
disk** (seek thrash otherwise), **4–8 for SSD/NVMe**, chosen from the discovered `media` field.
Scans checkpoint after every N files so a yanked cable costs seconds, not a whole pass.

Similarity hashing (Tier 3, opt-in, for version detection) runs only over selected media
classes: dHash/pHash for images, chromaprint for audio, keyframe hashes for video,
normalized-text shingles for documents.

---

## 6. Risk model — turning `l`, `p`, `o` into a number

For each drive `d`, estimate an annualized failure probability `f_d`:

```
f_d = base_afr(model_family)                # seeded from published fleet stats, user-editable
      × age_factor(now − purchase_date)     # bathtub curve: high year 0, low 1–3, rising 4+
      × duty_factor(power_on_hours, media)  # HDD: hours; SSD: TBW vs endurance rating
      × smart_factor(reallocated, pending, uncorrectable, crc)   # a pending sector is a step change,
                                            # not a nudge — this term dominates when non-zero
      × bus_factor(bus, enclosure)          # USB enclosures and their bridges fail too
```

For a content item held on drive set `D`, loss requires *all* of `D` to fail:

```
P(loss) = Π f_d × corr(D)
```

where `corr(D) > 1` when drives share a `batch_key` (same manufacturer + model + purchase
month) or a `location` (one flood, one surge, one burglary). **This is what makes `l`
and `p` first-class:** two copies on two drives from the same order are worth measurably
less than two copies across two manufacturers bought a year apart, and the tool says so
in bytes rather than in vibes.

Portfolio metric, shown on the dashboard and used as the planner objective:

```
expected_bytes_lost_per_year = Σ_over_content ( size × P(loss) )
```

Reports translate that back into English: *"1.4 TB of your library — 12,300 files —
sits on a single 2019 drive with 4 pending sectors. Expected loss: 380 GB/yr.
Copying that set to `ARCHIVE-2` costs 1.4 TB and drops it to 3 GB/yr."*

---

## 7. Redundancy and duplication analysis

Derived views, computed on demand from the catalog:

- **`copies(content)`** — count of *distinct drives* holding it (not paths, not hardlinks).
- **Under-protected set** — `copies < policy.min_copies`, sorted by `size × P(loss)`.
- **Over-duplicated set** — `copies > min_copies`, or multiple copies *on the same drive*
  (which buys zero durability and costs real space). Reclaimable bytes are stated per drive.
- **Version sets** — near-duplicates grouped with a proposed *preferred* member chosen by
  keep-rules (highest resolution / longest duration / newest mtime / shortest path /
  most-descriptive name — configurable, always previewed, never auto-applied).
- **Orphans and rot** — files present in an old scan but missing now; content whose full
  hash changed while mtime did not (silent corruption / bit rot signal).

---

## 8. The planner

### 8.1 Placement units

Solving per-file over 10M files is intractable and, worse, produces an unreviewable plan.
The planner works over **placement units**: a directory subtree, a tag, or a version set —
typically a few hundred to a few thousand units. Units are what a human can actually
approve, and they preserve locality (photos from one trip stay together).

### 8.2 Formulation

Binary `x[u][d]` = unit `u` is stored on drive `d`.

```
minimize   α · Σ_u size_u · P(loss | drives holding u)      # durability
         + β · Σ bytes moved                                # work
         + γ · Σ drives touched                             # human effort: plugging things in
         + δ · imbalance(free space across drives)          # headroom
         + ε · Σ policy_soft_violations                     # tidiness

subject to Σ_u size_u · x[u][d] ≤ cap_d − reserve_d         for all d
           Σ_d x[u][d] ≥ k_u                                min copies
           Σ_{d ∈ batch b} x[u][d] ≤ k_u − 1                diversity, when k_u ≥ 2
           x[u][d] = 0                                       for excluded drives (role/location)
           x[u][d] = 1                                       for pinned units
```

Two solvers behind one interface: a greedy seed (place the riskiest, largest units first,
then hill-climb with swap/move neighborhoods) and CP-SAT when `ortools` is present. Greedy's
solution is the CP-SAT warm start, so the optimizer can only improve on an explainable baseline.

### 8.3 From assignment to operations

The target assignment is diffed against reality to produce a dependency-ordered op graph:

```
mkdir → copy → verify(full hash at destination) → [update catalog] → quarantine source → expire
```

Hard invariants, enforced at plan time **and re-checked immediately before each destructive
op against the live catalog**:

- Never reduce a content item below `min_copies`. A source is quarantined only after its
  replacement is byte-verified at the destination.
- Never delete the *only* copy of anything, regardless of policy.
- Quarantine is a real directory on the same volume (`.drivefusion/quarantine/<plan>/`);
  space is reclaimed on explicit expiry, so an "oops" is a restore, not a recovery job.

### 8.4 Simulation before anything moves

Every plan renders as a before/after: per-drive free space bars, total bytes copied,
drives that must be connected and in what order, wall-clock estimate from measured
throughput, and the change in `expected_bytes_lost_per_year`. Plans export to JSON so they
can be diffed, archived, and re-run.

---

## 9. Findability — the "traceable order" requirement

Consolidation without a scheme just relocates the mess. The tool enforces order in four ways:

1. **Naming templates** per collection, e.g. `{category}/{year}/{yyyy-mm-dd}_{event}/{original_name}`,
   applied as *renames inside a plan* with full before/after preview and an undo map.
2. **Per-drive manifest** written at the drive root on every plan completion:
   `_DRIVE-FUSION.json` (machine) + `_CATALOG.md` (human) listing drive identity, contents
   by collection, counts, sizes, hashes, and where the sibling copies live. **A shelved drive
   is then self-describing without the app.**
3. **Global index** — the SQLite catalog, searchable by name, path, tag, size, date, drive,
   or copy count, plus FTS5 over paths. Answers "which drive is X on" for offline drives.
4. **Stable content IDs** — the full hash is the permanent name of a piece of content;
   renames and moves never orphan its history.

---

## 10. GUI

Eight screens, all reading the same core:

1. **Dashboard** — drives with capacity/free bars colored by health; headline numbers:
   total unique bytes, duplicate waste, under-protected bytes, expected loss/yr.
2. **Drives** — inventory; edit purchase date, price, vendor, warranty, location, nickname,
   role; SMART detail and history sparklines; "connected / last seen 12 days ago".
3. **Catalog** — tree + virtualized table, columns for size, copies, holding drives,
   riskiest holder; instant search; saved filters.
4. **Duplicates & Versions** — groups with reclaimable bytes, side-by-side compare
   (image preview / text diff), keep-rule preview.
5. **Policy** — rules table: match (glob/tag/size/media) → min copies, diversity required,
   allowed roles, naming template.
6. **Plan** — the proposed op list, grouped by drive, each op individually approvable;
   simulation panel; "requires: plug in DRIVE-3, then DRIVE-7".
7. **Run** — live progress, per-file throughput, verify results, pause/resume/abort,
   full log; safe to close and reopen — the journal is in the DB.
8. **Reports** — export CSV/JSON/HTML; per-drive manifests; insurance-style inventory.

Non-negotiables: no modal blocking on I/O; every long operation cancellable; every
destructive action requires typed confirmation naming the drive.

---

## 11. Packaging

- **PyInstaller one-folder** per OS, built in CI on `windows-latest`, `macos-latest`, `ubuntu-latest`.
- App data (catalog DB, logs, config) in `platformdirs.user_data_dir("DriveFusion")`,
  with a **portable mode** flag that puts the DB beside the executable — so the whole tool
  and its catalog can live on a thumb drive and travel to the machine the drives are attached to.
- Migrations run automatically on launch; the DB is backed up before any schema change.
- Ship a bundled `smartctl` where licensing allows, else detect and prompt; degrade
  gracefully to "health unknown" rather than failing.
- Signing: Authenticode on Windows and notarization on macOS are the difference between
  "it runs" and "users can run it" — budget for certs before the first external release.

---

## 12. Testing

- **Synthetic filesystem fixtures**: generated trees with known duplicate/version structure;
  every analysis assertion is exact, not approximate.
- **Loopback volumes** (Linux/macOS) to exercise real multi-volume flows, including
  capacity-exhaustion and unplug-mid-copy.
- **Property tests on the planner** — the invariants from §8.3 are asserted over randomized
  drive/content populations: no plan may ever reduce any content below its policy minimum,
  and no plan may exceed any drive's capacity.
- **Fault injection in the executor**: kill mid-copy, corrupt a destination byte, fill the
  destination, remove the source — each must leave the catalog consistent and the plan resumable.
- **Scale test**: a 10M-row synthetic catalog to keep query and UI paging honest.

---

## 13. Milestones

| # | Deliverable | Scope |
|---|---|---|
| **M0** | Skeleton | repo layout, packaging config, CI matrix, `drivefusion --version` runs from a built exe |
| **M1** | Catalog core | discovery, schema+migrations, scanner with tiered hashing, resume, `scan`/`ls`/`find` CLI |
| **M2** | Analysis | duplicate groups, copies-per-drive, under-protected report, wasted-space report, CSV/JSON export |
| **M3** | GUI shell | PySide6 app, dashboard + drives + catalog browser over M1/M2, threaded workers |
| **M4** | Health & risk | SMART collection, AFR tables, purchase/usage metadata, `expected_bytes_lost_per_year` |
| **M5** | Planner | policy engine, placement units, greedy solver + CP-SAT, op graph, simulation, plan export |
| **M6** | Executor | journaled runner, verify, quarantine, resume, rollback; Run + Plan screens |
| **M7** | Order & polish | naming templates, per-drive manifests, version sets/similarity, reports, signed builds |

M1–M2 alone already answer use cases 1 and 2 from the CLI; the GUI at M3 makes them usable;
M5–M6 deliver use case 3. Each milestone is independently useful — a good property when
the thing being automated is *deleting your own data*.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hashing many TB is slow | Tiered hashing (§5); unique sizes never read; mtime/inode reuse makes re-scans metadata-only |
| Removable drives change mount points / letters | Identity via serial + fs_uuid, never path |
| Hardlinks & CoW clones inflate apparent redundancy | Detect via inode/nlink and clone flags; count physical copies, not paths |
| A bug deletes data | Copy-verify-quarantine-expire; invariants re-checked live before each destructive op; deletions never in the same pass as copies |
| SMART unavailable over some USB bridges | Degrade to age + capacity heuristics; mark confidence explicitly rather than inventing a number |
| Optimizer output is unreviewable | Placement units, per-op approval, plain-language simulation summary |
| Antivirus flags the packed exe | One-folder build, signed binaries, published hashes |

---

## 15. Open questions for you

1. **Scale** — roughly how many drives and how many total files? Under ~2M files the
   pure-SQLite path is comfortable; well beyond that, M1 should budget for extra indexing work.
2. **Platforms** — Windows only, or macOS/Linux too? It affects discovery/SMART effort
   (§3) more than anything else in the plan.
3. **Cloud/offsite** — should offsite (B2/S3/rsync target) count as a copy in the policy,
   or is this local-drives-only for v1?
4. **Deletion appetite** — should v1 ship the quarantine/expire path at all, or stop at
   "copy and report", leaving reclamation manual until you trust it?

Defaults if you'd rather not decide now: local-only, all three platforms, copy-and-report
first with quarantine gated behind an explicit setting.
