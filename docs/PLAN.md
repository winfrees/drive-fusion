# Drive Fusion — Build Plan

A cross-platform desktop tool that catalogs every drive you own (internal, external,
connected or sitting on a shelf), proves how many real copies of each byte exist, and
produces **plans** for consolidating that content into a traceable, FAIR-aligned order.

**Drive Fusion is a planning and reporting instrument. It never modifies, moves, or
deletes your data.** Its only outputs are a local catalog database and export documents.
See §2 — this is enforced by construction and tested, not merely promised.

---

## 1. The problem, stated precisely

You have:

| Symbol | Quantity | What it drives |
|---|---|---|
| `n` | drives, each of capacity `cap_d` | the capacity constraint on any consolidation plan |
| `m` | copies and/or near-copies (versions) of a file | the deduplication / redundancy question |
| `l` | manufacturers | **correlated** failure risk — 3 copies on 3 drives from one bad batch is not 3 copies |
| `p` | purchase dates | age → failure rate; also warranty and retention clocks |
| `o` | usage (power-on hours, writes, duty cycle) | wear → failure rate |

Three use cases, in dependency order:

1. **Catalog** — know what exists, where, on which physical device, even when that device
   is unplugged.
2. **Confirm redundancy** — for every distinct piece of *content*, how many copies exist,
   on how many **distinct physical drives**, in how many locations, and what is the
   probability that all of them are lost.
3. **Plan the merge** — produce an ordered, reviewable set of *recommended* steps that
   consolidates duplicates, satisfies a preservation policy, fits the capacity you own,
   and leaves the result findable by a human and by a machine.

Two metrics tie `n, m, l, p, o` together and score every plan:

- **Expected bytes lost per year** — the durability number (§6).
- **FAIR + preservation-level scorecards** — the stewardship number (§7).

Together they turn "I have lots of copies" into "I have the *right* copies, in the *right*
places, described well enough to be found and reused."

---

## 2. Read-only by construction

The requirement is absolute: *there must never be an opportunity for the tool to delete
anything.* A policy statement is not sufficient for that — a bug, a bad refactor, or a
misfired code path could violate a policy. So the guarantee is structural.

### 2.1 The rule

The tool may write to exactly two places:

1. its own application-data directory (catalog DB, logs, config); and
2. an **export directory the user explicitly chooses**, where every export is written to a
   new timestamped folder — existing exports are never overwritten or removed.

Everything else — every catalogued volume, every scanned path — is opened **read-only, and
nothing else**. There is no code path in the product that removes, renames, truncates, or
writes a byte to scanned media, because no such call is permitted to exist.

### 2.2 How it is enforced

| Layer | Mechanism |
|---|---|
| **Single gateway** | All access to catalogued volumes goes through `core/fsio.py`, whose entire public surface is `scandir()`, `stat()`, and `open_read()`. It opens files with `O_RDONLY` (plus `O_NOATIME` where permitted, so reading does not even update access times) and returns file objects that raise on any write method. There is no `open_write` to call. |
| **Build-time lint** | A custom AST checker in CI fails the build if any module outside `core/store/` and `core/export/` references `os.remove`, `os.unlink`, `os.rmdir`, `os.rename`, `os.replace`, `os.truncate`, `shutil.rmtree`, `shutil.move`, `shutil.copy*`, `Path.unlink`, `Path.rename`, `Path.write_*`, `os.chmod`, `os.chown`, `os.utime`, or `open()` with any mode other than `"rb"`. This runs on every commit and is a required status check. |
| **Export-path assertion** | `core/export/` is the only writer of user-facing files. Every write is routed through one function that asserts the target resolves inside the user-chosen export root, and that the target does not already exist. It has no delete function. |
| **OS-level read-only mounts** | Where the platform allows, volumes are mounted read-only for the duration of a scan (`mount -o ro` on Linux, `diskutil mount readOnly` on macOS). On Windows, handles are opened with read-only access and full sharing so the tool can never block or alter another process's file. |
| **No-touch regression test** | The test suite builds a fixture tree, records a Merkle hash of every path, size, mtime, and byte, runs a **full** scan → analysis → plan → export cycle, and re-hashes. The assertion is byte-and-metadata identity. If any code ever touches source data, this test fails. |
| **Denylist on generated text** | Plan exports may optionally include *copy-only* command previews (`rsync` without `--delete`, `robocopy` without `/MIR` or `/PURGE`) for the user to review and run themselves. A linter rejects any generated string containing `rm`, `del`, `rmdir`, `unlink`, `shred`, `format`, `mkfs`, `--delete`, `/MIR`, `/PURGE`. The tool will not so much as *print* a destructive command. |

### 2.3 What this changes about the design

- There is **no executor**. Plans are documents, not runnable jobs. A plan's terminal state
  is "exported," never "applied."
- **Deduplication is a report, not an operation.** Redundant copies are presented as
  *reclamation candidates* with evidence — hashes, locations, copy counts, which copy the
  keep-rules favor and why — and the decision and the action belong entirely to you.
- **Per-drive manifests are written into the export bundle**, not onto the drive. If you
  want a drive to carry its own catalog (§9), copying that file onto it is a step on your
  checklist that you perform.
- **Reorganization is proposed as a mapping**, an explicit old-path → new-path table you
  can review, diff, archive, and execute with your own tools.

---

## 3. Standards alignment

The tool is designed against the following published frameworks. Because these change —
NIH replaced the DMS Plan format in May 2026, mid-design — **every rubric ships as a
versioned, citable rules file under `standards/`**, each carrying its source URL, version,
and retrieval date. Reports state which version they scored against. Updating to a new
revision of a standard is a data change, not a code change.

| Framework | Role in the tool |
|---|---|
| **FAIR Principles** (Wilkinson et al. 2016) | The per-collection scorecard: Findable / Accessible / Interoperable / Reusable (§7.2) |
| **NIH Data Management & Sharing Policy** (NOT-OD-21-013, effective 2023-01-25) | Governs what must be planned for and shared; drives the DMS export (§7.3) |
| **NIH DMS Plan format, 2026 revision** (NOT-OD-26-100) | The actual export target: one page, seven Elements, Yes/No/Not-Applicable for Elements 1–3, 5, 7; narrative for Element 4 (≤300 words) and Element 6 (≤100 words); a table of anticipated data types and the repository for each |
| **NIH guidance on protecting participant privacy** (NOT-OD-22-213) | Sensitivity flagging and export redaction (§7.4) |
| **NIH Genomic Data Sharing Policy** (NOT-OD-14-124) | Controlled-access designation for genomic data |
| **NDSA Levels of Digital Preservation v2.0** | The preservation scorecard across Storage, Integrity, Control, Metadata, Content, at four levels; drives redundancy policy targets |
| **OAIS** (ISO 14721) | Vocabulary for the export bundle: SIP / AIP / DIP roles |
| **BagIt** (RFC 8493) | Export bundle packaging with `manifest-sha256.txt` and `tagmanifest` |
| **RO-Crate** / **schema.org `Dataset`** | Machine-readable collection description (JSON-LD) |
| **DataCite Metadata Schema** | Deposit-ready metadata and `relationType` links between versions |
| **PREMIS** / **PROV-O** | Fixity and provenance events in exports |
| **TRUST Principles** | Criteria used when the tool ranks candidate repositories |
| **CARE Principles for Indigenous Data Governance** | Governance flags that sit alongside FAIR, never overridden by it |
| **2 CFR 200.334 / 45 CFR 75.361** | Record-retention clocks (§7.5) |

**Scope disclaimer, stated in the app and in every export:** Drive Fusion assists with
documentation and preservation planning. It does not determine regulatory compliance, and
its sensitivity detection is heuristic. Responsibility for compliance with NIH policy,
IRB determinations, HIPAA, consent terms, and institutional requirements remains with you
and your institution.

---

## 4. Architecture

```
drivefusion/
├── core/
│   ├── fsio.py           # THE read-only gateway (§2.2) — the only reader of user media
│   ├── model.py          # dataclasses: Drive, Volume, FileRecord, Content, Collection, Plan
│   ├── store/            # SQLite schema, migrations, DAOs, FTS index
│   ├── discovery/        # enumerate physical drives + volumes per OS
│   │   ├── linux.py      #   lsblk --json, /sys/block, blkid
│   │   ├── darwin.py     #   diskutil list -plist, system_profiler SPStorageDataType
│   │   └── windows.py    #   PowerShell Get-PhysicalDisk / Get-Volume (JSON out)
│   ├── health/           # smartctl --json parsing, risk scoring, AFR tables
│   ├── scan/             # walker, stat capture, hashing pipeline, resume, fixity
│   ├── identity/         # content identity, quick-hash, hardlink & clone handling
│   ├── analysis/         # duplicate groups, version sets, redundancy, integrity drift
│   ├── curate/           # collections, controlled vocabularies, metadata completeness
│   ├── sensitivity/      # heuristic PHI / human-subjects / controlled-access flags
│   ├── fair/             # FAIR + NDSA scorecards, gap findings, remediation advice
│   ├── policy/           # preservation rules: min copies, diversity, NDSA level targets
│   ├── planner/          # placement solver → recommended-step documents (no execution)
│   └── export/           # THE only writer of user-facing files (§2.2)
├── standards/            # versioned rubric + template files, each with source + date
├── cli/                  # drivefusion scan|report|score|plan|export   (no mutating verbs)
├── gui/                  # PySide6 views + Qt worker threads
└── packaging/            # .spec files, icons, CI build matrix
```

**Threading rule:** the GUI thread never does I/O. Every scan, hash, score, and plan runs
in a `QThread` worker reporting progress via signals. The catalog is written by a single
writer thread behind a queue; readers use their own connections in WAL mode.

**Network rule:** the tool makes **no outbound network connections** — no telemetry, no
update checks, no metadata lookups — by default. This is a hard default because catalogs
of human-subjects data can contain sensitive information in filenames alone. Any future
online feature (e.g. repository lookup) ships opt-in, off, and clearly labeled.

---

## 5. Data model

Abbreviated DDL; the shape is the point.

```sql
-- A physical device. Survives being unplugged, reformatted, or re-lettered.
CREATE TABLE drive (
  id INTEGER PRIMARY KEY,
  serial TEXT UNIQUE,            -- identity anchor
  wwn TEXT, model TEXT, manufacturer TEXT,
  bus TEXT,                      -- sata|usb|nvme|thunderbolt
  media TEXT,                    -- hdd|ssd|flash|optical|tape
  capacity_bytes INTEGER,
  nickname TEXT,                 -- "Shelf B, blue Seagate"
  purchase_date TEXT, purchase_price_cents INTEGER, vendor TEXT,
  warranty_end TEXT,
  batch_key TEXT,                -- manufacturer+model+purchase month → correlation group
  role TEXT,                     -- working|replica|archive|offsite
  location TEXT,                 -- lab bench|shelf|institutional store|offsite
  disaster_zone TEXT,            -- for NDSA geographic-separation scoring
  encryption TEXT,               -- none|fde|filevault|bitlocker|luks
  retired_at TEXT, notes TEXT
);

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
  started_at TEXT, finished_at TEXT, status TEXT,
  files_seen INTEGER, bytes_seen INTEGER, hashed_bytes INTEGER,
  mounted_readonly INTEGER,      -- recorded evidence of §2.2 enforcement
  tool_version TEXT, standards_version TEXT
);

-- Distinct content, addressed by hash. Redundancy is counted over this.
CREATE TABLE content (
  id INTEGER PRIMARY KEY,
  size_bytes INTEGER NOT NULL,
  quick_hash BLOB NOT NULL,      -- size + head 64K + tail 64K + midpoint
  full_hash BLOB,                -- NULL until confirmed
  hash_algo TEXT,                -- blake3|blake2b|sha256
  media_kind TEXT,               -- image|audio|video|doc|tabular|genomic|imaging|archive|other
  format_id TEXT,                -- PRONOM PUID where identifiable
  format_risk TEXT,              -- open|proprietary|at-risk|unknown  → Reusability finding
  UNIQUE(size_bytes, quick_hash, full_hash)
);

CREATE TABLE file (
  id INTEGER PRIMARY KEY,
  volume_id INTEGER, content_id INTEGER,
  dir_path TEXT, name TEXT, ext TEXT,
  size_bytes INTEGER, mtime_ns INTEGER,
  inode INTEGER, nlink INTEGER, is_symlink INTEGER,
  first_seen_scan INTEGER, last_seen_scan INTEGER,
  vanished_at TEXT               -- metadata outlives the bits (FAIR A2)
);
CREATE INDEX ix_file_content ON file(content_id);
CREATE INDEX ix_file_vol_dir ON file(volume_id, dir_path);
CREATE VIRTUAL TABLE file_fts USING fts5(name, dir_path, content='file', content_rowid='id');

-- The curation unit: a dataset/collection a human reasons about and a repository ingests.
CREATE TABLE collection (
  id INTEGER PRIMARY KEY,
  local_pid TEXT UNIQUE,         -- stable local persistent identifier (FAIR F1)
  title TEXT, description TEXT,
  creators_json TEXT, contact TEXT,
  license TEXT,                  -- FAIR R1.1; NULL is a reportable gap
  external_pid TEXT,             -- DOI / dbGaP / SRA / GEO accession once deposited
  award_number TEXT, project TEXT,
  data_type TEXT,                -- controlled vocabulary
  sensitivity TEXT,              -- public|deidentified|limited|identifiable|controlled-access
  consent_notes TEXT,
  retention_basis TEXT, retention_until TEXT,
  share_by TEXT,                 -- expected sharing date (publication / end of award)
  target_repository TEXT,
  ndsa_target INTEGER,           -- 1..4
  min_copies INTEGER, require_diversity INTEGER, require_offsite INTEGER
);
CREATE TABLE collection_member (collection_id INTEGER, file_id INTEGER);
CREATE TABLE collection_relation (               -- FAIR I3 qualified references
  from_collection INTEGER, to_ref TEXT, relation_type TEXT   -- DataCite relationType
);

-- Near-duplicates: reprocessed images, doc v1/v2/final.
CREATE TABLE similarity (content_id INTEGER, kind TEXT, hash BLOB);
CREATE TABLE version_set (id INTEGER PRIMARY KEY, label TEXT, method TEXT, confidence REAL);
CREATE TABLE version_member (version_set_id INTEGER, content_id INTEGER, is_preferred INTEGER);

-- Fixity over time → bit-rot detection (NDSA Integrity).
CREATE TABLE fixity_check (
  content_id INTEGER, file_id INTEGER, checked_at TEXT,
  expected_hash BLOB, observed_hash BLOB, result TEXT   -- ok|mismatch|unreadable|missing
);

-- Health over time → the `o` axis.
CREATE TABLE drive_health (
  drive_id INTEGER, observed_at TEXT,
  power_on_hours INTEGER, temperature_c INTEGER,
  reallocated INTEGER, pending INTEGER, uncorrectable INTEGER, crc_errors INTEGER,
  wear_leveling INTEGER, tbw_written INTEGER,
  self_test_status TEXT, smart_json TEXT,
  annual_failure_prob REAL, confidence TEXT      -- measured|estimated|unknown
);

-- Plans are documents. Note: no state beyond "exported", and no op kind that removes.
CREATE TABLE plan (id INTEGER PRIMARY KEY, created_at TEXT, name TEXT,
                   policy_snapshot TEXT, standards_version TEXT,
                   objective_json TEXT, status TEXT);   -- draft|exported|superseded
CREATE TABLE plan_step (
  id INTEGER PRIMARY KEY, plan_id INTEGER, seq INTEGER,
  kind TEXT,          -- copy|organize|describe|deposit|verify|review-candidate|acquire
  rationale TEXT,     -- why this step exists, in plain language
  src_volume_id INTEGER, src_path TEXT,
  dst_volume_id INTEGER, dst_path TEXT,
  content_id INTEGER, collection_id INTEGER, bytes INTEGER,
  depends_on TEXT,    -- JSON list of plan_step.id
  risk_delta REAL     -- change in expected bytes lost per year if performed
);

-- Append-only, hash-chained audit log (FAIR R1.2, DMS oversight).
CREATE TABLE audit (
  id INTEGER PRIMARY KEY, at TEXT, actor TEXT, action TEXT, detail_json TEXT,
  prev_hash BLOB, entry_hash BLOB
);
```

**Identity notes that matter in practice**

- A drive is identified by `serial`, a volume by `fs_uuid` — never by mount point or drive
  letter, which change between sessions.
- Hardlinks: files sharing `(volume_id, inode)` are one physical copy. Counting them as two
  would inflate redundancy — a correctness bug, not a cosmetic one.
- APFS/Btrfs/ReFS clones look independent but share blocks: one copy for *durability*,
  separate files for *space accounting*. Flagged distinctly.
- Windows: open paths with the `\\?\` prefix to survive >260-character paths.
- Case-insensitive volumes: normalize for matching, store as observed.

---

## 6. Scanning, fixity, and the durability model

### 6.1 Tiered hashing

Three tiers keep a multi-terabyte scan tractable:

1. **Tier 0 — stat only.** `os.scandir` yields name, size, mtime, inode with no extra
   syscalls. If `(volume, dir, name, size, mtime_ns, inode)` matches the previous scan,
   reuse the existing `content_id`. Re-scanning an unchanged drive becomes a metadata walk:
   minutes, not hours.
2. **Tier 1 — quick hash.** Only for sizes appearing more than once anywhere in the catalog.
   Hash size + first 64 KiB + last 64 KiB + a midpoint block. A unique size implies unique
   content, so the body is never read at all.
3. **Tier 2 — full hash.** Only where quick hashes collide. This is the only tier that reads
   whole files, and it runs over a small fraction of total bytes.

Concurrency is per-device, because the bottleneck is physical: **1–2 readers for a spinning
disk** (seek thrash otherwise), **4–8 for SSD/NVMe**, selected from the discovered `media`
field. Scans checkpoint continuously, so an unplugged cable costs seconds, not a pass.

### 6.2 Fixity (NDSA Integrity)

Full hashes recorded once become the baseline for scheduled re-verification. A file whose
content hash changed while its mtime did not is **silent corruption**, and it is reported as
an integrity incident with the affected collection, the last-known-good scan, and which
other drives hold a verified copy that could be used to repair it — a repair *you* perform.
Fixity results feed PREMIS events in exports.

### 6.3 Turning `l`, `p`, `o` into a number

For each drive `d`, estimate an annualized failure probability:

```
f_d = base_afr(model_family)                # from published fleet statistics, user-editable
      × age_factor(now − purchase_date)     # bathtub curve: elevated year 0, low 1–3, rising 4+
      × duty_factor(power_on_hours, media)  # HDD: hours; SSD: TBW against endurance rating
      × smart_factor(reallocated, pending, uncorrectable, crc)
      × bus_factor(bus, enclosure)          # USB bridges fail too, and take the drive with them
```

A pending sector is a step change, not a nudge; that term dominates when non-zero. Where
SMART is unavailable (common over USB bridges), the estimate degrades to age and capacity
heuristics and is labeled `confidence = estimated` rather than silently guessing.

For content held on drive set `D`, loss requires all of `D` to fail:

```
P(loss) = Π f_d × corr(D)
```

`corr(D) > 1` when drives share a `batch_key` (same manufacturer + model + purchase month)
or a `disaster_zone` (one flood, one surge, one theft). **This is what makes `l` and `p`
first-class:** two copies from the same purchase order are worth measurably less than two
copies across manufacturers bought a year apart, and the tool says so in bytes rather than
in vibes. It is also exactly the reasoning behind NDSA's geographic-separation levels and
the 3-2-1 rule, so the durability model and the preservation rubric agree by construction.

Portfolio metric, on the dashboard and used as the planner objective:

```
expected_bytes_lost_per_year = Σ_over_content ( size × P(loss) )
```

Reports translate it back into English: *"1.4 TB of Project Alpha — 12,300 files — sits on
a single 2019 drive with 4 pending sectors, and that collection's DMS Plan commits to
sharing it in March. Expected loss: 380 GB/yr. Copying it to ARCHIVE-2 costs 1.4 TB and
drops that to 3 GB/yr."*

---

## 7. Analysis, scoring, and the NIH layer

### 7.1 Redundancy and duplication

Derived views, computed on demand:

- **`copies(content)`** — distinct *drives* holding it; not paths, not hardlinks.
- **Under-protected set** — `copies < policy.min_copies`, ranked by `size × P(loss)`. This is
  the queue that actually matters, and it is the first screen you should look at.
- **Reclamation candidates** — content over the copy target, or duplicated *within* one
  drive (which buys zero durability and costs real space). Presented with full evidence and
  reclaimable byte counts. **A report, never an action** (§2.3).
- **Version sets** — near-duplicates grouped by perceptual/text similarity, with a *proposed*
  preferred member under configurable keep-rules (highest resolution, longest duration,
  newest mtime, most descriptive name). Proposed, previewed, and exported as a
  recommendation — never applied.
- **Integrity drift** — fixity mismatches, unreadable files, and content that has vanished
  since a prior scan.

### 7.2 FAIR scorecard

Scored per collection, each failing criterion naming the specific gap and its remedy:

| | Criterion | How the tool scores and remediates it |
|---|---|---|
| **F1** | Globally unique, persistent identifiers | Every collection gets a stable `local_pid`; content hashes are permanent identifiers that survive renames and moves. External DOI/accession recorded once deposited. Missing PID → finding. |
| **F2** | Rich metadata | Metadata completeness percentage against the collection's profile; missing required fields itemized. |
| **F3** | Metadata explicitly include the data identifier | Exported manifests bind PID ↔ checksums ↔ paths ↔ holding drives. |
| **F4** | Registered in a searchable resource | Local FTS5 index over all paths, plus DataCite / schema.org `Dataset` JSON-LD export for repository indexing. Never deposited in a repository → finding. |
| **A1** | Retrievable by identifier over an open protocol | Catalog answers "where is this" for offline media by drive nickname and location; exports use open, documented formats. |
| **A1.2** | Authentication/authorization where required | Controlled-access collections flagged; restricted paths excluded from shareable exports by default. |
| **A2** | Metadata persist when data are unavailable | Tombstone records (`vanished_at`, `retired_at`) keep descriptions and checksums for content on failed or retired media — the catalog outlives the bits. |
| **I1** | Formal, shared representation | RO-Crate / JSON-LD / DataCite exports, not bespoke CSV only. |
| **I2** | FAIR vocabularies | Controlled vocabularies for data type, format (PRONOM), and sensitivity; mapping to NIH Common Data Elements where one applies. |
| **I3** | Qualified references | `collection_relation` records typed links (`IsVersionOf`, `IsDerivedFrom`, `IsPartOf`, `References`); detected version sets propose `IsVersionOf` links. |
| **R1** | Plurality of accurate attributes | Completeness scoring as F2, weighted toward reuse-critical fields. |
| **R1.1** | Clear, accessible usage license | `license` is required; NULL is a top-line finding, because unlicensed data is not reusable. |
| **R1.2** | Detailed provenance | Scan history, fixity events, and the hash-chained audit log export as PROV-O / PREMIS. |
| **R1.3** | Domain-relevant community standards | Format identification flags proprietary and at-risk formats and recommends open preservation alternatives — a *recommendation*; the tool never converts anything. |

### 7.3 Preservation levels and NIH DMS support

**NDSA scorecard.** Each collection is scored across Storage, Integrity, Control, Metadata,
and Content, and reported as the level it currently meets versus its `ndsa_target`. Because
copy count, geographic separation, fixity history, and metadata completeness are all already
in the catalog, this scores automatically. Policy targets are expressed as a level ("Project
Alpha must reach Level 3"), and the planner treats that as the constraint to satisfy.

**DMS Plan support export.** The tool drafts the evidence-backed portions of an NIH Data
Management and Sharing Plan in the **2026 format** — one page, seven Elements, Yes/No/NA for
Elements 1–3, 5, and 7, narrative for Element 4 (≤300 words) and Element 6 (≤100 words) —
populating from catalog facts rather than from recollection:

- the **data-type × repository table** the format requires, with actual types, file counts,
  formats, and total volume per type;
- proposed answers for the Yes/No commitments, each annotated with the catalog evidence
  behind it, so a "Yes" is defensible;
- a draft Element 4 justification stub wherever the catalog shows a collection flagged as
  identifiable or controlled-access, since that is where limits on sharing get justified.

The form definition lives in `standards/dmsp-2026.yaml`, versioned with its source and
retrieval date. **The tool drafts; you review, edit, and submit.** It never files anything.

**Progress reporting.** Because the 2026 update removed the prior-approval requirement for
modifying an approved plan and moved change reporting into the RPPR (Section C.5.c),
Drive Fusion also emits a *DMS progress summary* per award — what was generated, what has
been deposited, what remains, and any deviation from the plan of record — sized to paste
into an RPPR.

### 7.4 Sensitivity flagging

Heuristics, clearly labeled as heuristics, flag collections likely to carry human-subjects
data: filename and path patterns suggesting direct identifiers (names, MRN, DOB, SSN
patterns), file types that commonly embed PHI (DICOM, whole-slide images, VCF/BAM/FASTQ),
and structural signals. Flagged collections:

- default to **excluded from shareable exports**, with paths and filenames redacted in any
  export marked shareable;
- prompt for consent basis, IRB determination, Certificate of Confidentiality status, and
  controlled-access repository designation under the GDS Policy;
- surface in the DMS export as candidates for an Element 4 justification.

The tool **reads filename and metadata signals only** — it does not scan file contents for
identifiers, since doing so would mean reading and buffering PHI to detect PHI. Detection is
an advisory prompt for human review, never a determination.

### 7.5 Retention and timelines

Collections carry a retention basis and clock (e.g. 2 CFR 200.334 / 45 CFR 75.361: three
years from final expenditure report submission) and an expected sharing date. Reports show
what is approaching a sharing commitment, what is past its retention floor and *eligible*
for review, and what is under a hold. Consistent with §2, "eligible for review" is a report
line, never a deletion.

---

## 8. The planner

### 8.1 Placement units

Solving per-file across 10M files is intractable and, worse, unreviewable. The planner works
over **placement units** — a collection, a directory subtree, or a version set — typically
hundreds to a few thousand. Units are what a human can actually approve, and they preserve
locality: one study's imaging stays together, and a collection destined for one repository
stays intact.

### 8.2 Formulation

Binary `x[u][d]` = unit `u` is stored on drive `d`.

```
minimize   α · Σ_u size_u · P(loss | drives holding u)      # durability
         + β · Σ bytes copied                               # work
         + γ · Σ drives touched                             # human effort: plugging things in
         + δ · imbalance(free space across drives)           # headroom
         + ε · Σ unmet FAIR / NDSA findings                  # stewardship

subject to Σ_u size_u · x[u][d] ≤ cap_d − reserve_d          for all d
           Σ_d x[u][d] ≥ k_u                                 min copies for u's NDSA target
           Σ_{d ∈ batch b} x[u][d] ≤ k_u − 1                 manufacturer/batch diversity
           Σ_{d ∈ zone z} x[u][d] ≤ k_u − 1                  geographic separation
           x[u][d] = 1                                       existing copies are never assumed removed
           x[u][d] = 0                                       for excluded drives (role/sensitivity)
```

The constraint `x[u][d] = 1` for every current location is the formal expression of
read-only operation: the solver may only ever **add** placements. It cannot propose a state
in which an existing copy is gone, so no plan can imply a deletion. Capacity shortfalls
therefore surface honestly as *"you need another N TB"* — an `acquire` step in the plan —
rather than being resolved by quietly proposing removals.

Two solvers behind one interface: a greedy seed (place the riskiest, largest units first,
then hill-climb over swap/add neighborhoods) and CP-SAT when `ortools` is installed, warm
-started from the greedy solution so the optimizer can only improve on an explainable baseline.

### 8.3 From assignment to a plan document

The target assignment is diffed against reality into a dependency-ordered checklist of
recommended steps — `copy`, `organize`, `describe`, `verify`, `deposit`, `review-candidate`,
`acquire` — each carrying a plain-language rationale and its `risk_delta`. The export
includes:

- an ordered checklist grouped by drive, stating which drives to connect and in what order;
- a before/after simulation: per-drive free space, total bytes to copy, wall-clock estimate
  from measured throughput, and the change in expected bytes lost per year;
- FAIR and NDSA scorecards before and after;
- an old-path → new-path mapping table for any proposed reorganization;
- optional copy-only command previews, subject to the §2.2 denylist;
- a separate, clearly segregated **reclamation candidates** appendix — evidence only.

Plans export to JSON as well as human formats, so they can be diffed against a later plan,
archived as provenance, and cited in a DMS progress report.

---

## 9. Findability — the "traceable order" requirement

Consolidation without a scheme just relocates the mess. Four mechanisms, all of them
read-only:

1. **Naming templates** per collection, e.g. `{project}/{data_type}/{yyyy}/{yyyy-mm-dd}_{sample}/{original_name}`,
   rendered as a reviewable old → new mapping table you execute yourself, with the inverse
   mapping included so the move is always traceable backward.
2. **Per-drive manifest**, generated into the export bundle as `_DRIVE-FUSION.json`
   (machine) and `_CATALOG.md` (human): drive identity, contents by collection, counts,
   sizes, checksums, and where the sibling copies live. Copy it onto the drive and a shelved
   drive becomes self-describing without the app. That copy is your step, not the tool's.
3. **Global index** — the SQLite catalog, searchable by name, path, collection, tag, size,
   date, drive, copy count, or sensitivity, with FTS5 over paths. It answers "which drive is
   this on" for drives that are in a box in a closet.
4. **Stable identifiers** — the content hash is the permanent name of a piece of content and
   the collection `local_pid` the permanent name of a dataset; renames and relocations never
   orphan history, and both bind cleanly to an external DOI or accession at deposit time.

---

## 10. GUI

Eight screens, all reading the same core. Note that no screen has an "Apply," "Move," or
"Delete" control, because no such capability exists behind them.

1. **Dashboard** — drives with capacity/free bars colored by health; headline numbers: total
   unique bytes, duplicate overhead, under-protected bytes, expected loss/yr, FAIR and NDSA
   distribution across collections.
2. **Drives** — inventory; edit purchase date, price, vendor, warranty, location, disaster
   zone, role, nickname; SMART detail with history sparklines; "connected / last seen 12 days ago."
3. **Catalog** — tree plus virtualized table; columns for size, copies, holding drives,
   riskiest holder, collection, sensitivity; instant search; saved filters.
4. **Collections** — the curation surface: title, creators, license, data type, sensitivity,
   retention, target repository, relations; metadata completeness meter per collection.
5. **Duplicates & Versions** — groups with reclaimable bytes, side-by-side compare (image
   preview / text diff), proposed preferred member with its rationale. Output is a report.
6. **Scorecards** — FAIR and NDSA per collection, each finding expandable into "what's
   missing, why it matters, what to do."
7. **Plan** — proposed steps grouped by drive, each with rationale and risk delta;
   simulation panel; "requires: connect ARCHIVE-3, then ARCHIVE-7."
8. **Exports** — DMS Plan draft, RPPR progress summary, RO-Crate/BagIt bundles, DataCite
   metadata, per-drive manifests, CSV/HTML reports; every export timestamped into its own
   folder, with the standards versions it was scored against recorded inside.

Non-negotiables: no modal blocking on I/O; every long operation cancellable; the read-only
guarantee stated in the UI where a user would expect a destructive action to be.

---

## 11. Packaging

- **PyInstaller one-folder** per OS, built in CI on `windows-latest`, `macos-latest`,
  `ubuntu-latest`. One-file mode unpacks to temp on every launch — slow and antivirus-hostile.
- App data (catalog, logs, config) in `platformdirs.user_data_dir("DriveFusion")`, with a
  **portable mode** that keeps the catalog beside the executable, so the tool and its catalog
  can travel on a thumb drive to whichever machine the drives are attached to.
- **Catalog encryption at rest** (SQLCipher, or documented OS-level FDE) as a first-class
  option, because a catalog of human-subjects data can be sensitive in its filenames alone.
- Migrations run on launch; the catalog is backed up (copied, never replaced) before any
  schema change.
- Bundle `smartctl` where licensing allows; otherwise detect and prompt, and degrade to
  "health unknown" rather than failing.
- Signing: Authenticode on Windows and notarization on macOS are the difference between "it
  runs" and "users can run it." Budget for certificates before any external release.

---

## 12. Testing

- **The no-touch test (§2.2) is the highest-priority test in the suite** and gates every
  commit. Merkle hash of the fixture tree before and after a full cycle; byte and metadata identity.
- **AST lint** for forbidden calls, as a required CI status check.
- **Synthetic filesystem fixtures** with known duplicate, version, hardlink, clone, and
  corruption structure; analysis assertions are exact, not approximate.
- **Loopback volumes** (Linux/macOS) for genuine multi-volume flows, including unplug-mid-scan
  and read-only mount verification.
- **Property tests on the planner**: over randomized drive and content populations, no plan
  may ever propose a state with fewer copies than currently exist, and no plan may exceed any
  drive's capacity.
- **Scorecard golden tests** pinned to a specific `standards/` version, so a rubric update is
  visible as an intentional diff rather than silent drift.
- **Scale test** at 10M catalog rows to keep query plans and UI paging honest.

---

## 13. Milestones

| # | Deliverable | Scope |
|---|---|---|
| **M0** | Skeleton **and guardrails** | repo layout, `core/fsio.py` gateway, AST lint in CI, the no-touch test, packaging config, build matrix. *The safety mechanism ships before any code that reads user media.* |
| **M1** | Catalog core | discovery, schema + migrations, scanner with tiered hashing, resume, `scan`/`find` CLI |
| **M2** | Analysis | duplicate groups, copies-per-drive, under-protected report, reclamation-candidate report, fixity baseline, CSV/JSON export |
| **M3** | GUI shell | PySide6 app: dashboard, drives, catalog browser over M1–M2, threaded workers |
| **M4** | Health & risk | SMART collection, AFR tables, purchase/usage metadata, expected bytes lost per year |
| **M5** | Curation & standards | collections, controlled vocabularies, sensitivity flagging, `standards/` rubric files, FAIR + NDSA scorecards |
| **M6** | Planner | policy engine (copy targets, diversity, geographic separation, NDSA targets), placement solver, plan documents with simulation |
| **M7** | Exports & polish | DMS Plan draft (2026 format), RPPR progress summary, RO-Crate/BagIt/DataCite, per-drive manifests, naming-template mappings, signed builds |

M1–M2 answer use cases 1 and 2 from the CLI alone; M3 makes them usable; M5 adds the
stewardship layer; M6–M7 deliver use case 3. Curation lands before planning deliberately —
the planner's constraints are preservation targets, so the rubric has to exist before the
solver can optimize against it.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hashing many TB is slow | Tiered hashing (§6.1); unique sizes never read; stat-reuse makes re-scans metadata-only |
| Removable drives change mount points and letters | Identity via serial + fs_uuid, never path |
| Hardlinks and CoW clones inflate apparent redundancy | Detect via inode/nlink and clone flags; count physical copies, not paths |
| **A future change reintroduces a write path** | Structural enforcement (§2.2): the gateway exposes no writer, CI lint blocks the calls, the no-touch test fails the build. A regression must defeat three independent mechanisms |
| Published standards change mid-project | Exactly what happened with the May 2026 DMS format. All rubrics are versioned data files with sources and dates; reports name the version scored against |
| The tool is mistaken for a compliance determination | Explicit disclaimer in-app and in every export (§3); findings are phrased as advisory, and sensitivity detection prompts for human review |
| A catalog or export leaks PHI via filenames | No network by default; catalog encryption option; shareable exports redact flagged paths; sensitivity defaults to excluded, not included |
| SMART unavailable over some USB bridges | Degrade to age and capacity heuristics; label confidence explicitly rather than inventing a number |
| Optimizer output is unreviewable | Placement units, per-step rationale, plain-language simulation |
| Antivirus flags the packed executable | One-folder build, signed binaries, published hashes |

---

## 15. Open questions

1. **Scale** — roughly how many drives and how many total files? Under ~2M files the pure
   SQLite path is comfortable; well beyond that, M1 should budget for extra indexing work.
2. **Platforms** — Windows only, or macOS and Linux too? This affects discovery and SMART
   (§4) more than anything else in the plan.
3. **NIH context** — is this supporting an active award with a DMS Plan of record, or is the
   FAIR alignment aspirational for a personal/lab archive? If there is an award, M5–M7 should
   be prioritized ahead of M4, and the DMS export should target your actual plan of record.
4. **Human-subjects data** — is any of this identifiable or controlled-access (dbGaP/GDS)? If
   so, sensitivity flagging and catalog encryption move from M5 into M1, because the catalog
   itself becomes sensitive the moment it is created.
5. **Repositories** — do you have designated target repositories, or should the tool rank
   candidates against TRUST criteria and NIH's repository-selection guidance?
6. **Offsite and cloud** — should an offsite or cloud copy count toward copy targets and
   geographic separation? It changes the policy model but not the read-only guarantee: the
   tool would record and score such copies, never create them.

Defaults if you would rather not decide now: all three platforms, local drives only,
FAIR/NDSA scoring on by default, sensitivity flagging on and defaulting to exclusion.

---

## 16. Sources

- [NOT-OD-21-013: Final NIH Policy for Data Management and Sharing](https://grants.nih.gov/grants/guide/notice-files/NOT-OD-21-013.html)
- [NOT-OD-21-014: Elements of an NIH Data Management and Sharing Plan](https://grants.nih.gov/grants/guide/notice-files/NOT-OD-21-014.html)
- [NOT-OD-26-100: Implementation Update: NIH Data Management and Sharing Plan Requirements](https://grants.nih.gov/grants/guide/notice-files/NOT-OD-26-100.html)
- [NOT-OD-22-189: Implementation Details for the NIH Data Management and Sharing Policy](https://grants.nih.gov/grants/guide/notice-files/NOT-OD-22-189.html)
- [NOT-OD-22-213: Protecting Privacy When Sharing Human Research Participant Data](https://grants.nih.gov/grants/guide/notice-files/not-od-22-213.html)
- [2026 Pilot Data Management and Sharing Plan Format Available](https://grants.nih.gov/news-events/nih-extramural-nexus-news/2026/04/2026-pilot-data-management-and-sharing-plan-format-available)
- [NIH Data Sharing: Statements & Notices](https://sharing.nih.gov/data-management-and-sharing-policy/resources/statement-and-notices)
- [NIH Genomic Data Sharing Policy: Statements & Notices](https://sharing.nih.gov/genomic-data-sharing-policy/resources/statements-and-notices)

Rubric details for NDSA Levels of Digital Preservation, BagIt (RFC 8493), RO-Crate,
DataCite, PREMIS, and the TRUST and CARE Principles are to be pinned from their primary
sources into `standards/` at M5, each with version and retrieval date.
