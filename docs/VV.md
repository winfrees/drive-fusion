# Drive Fusion — Verification & Validation

How to show, with evidence, that a given version of Drive Fusion does what
[PLAN.md](PLAN.md) says it does, on drives that look like the ones it is for.

- **Verification:** did we build it right? Every requirement in PLAN.md that can be
  checked by observation is restated as a testable requirement (§3). Each run is judged
  against an **oracle** that knows the true answer because it planted it.
- **Validation:** did we build the right thing? The datasets are realistic and different
  on every run (§4). They can live on **real, physically separate devices**: USB‑C thumb
  drives, spare spinners, the mix of NTFS, exFAT and FAT32 the tool will meet.

The bench is `python -m vv` (package [`vv/`](../vv)). It is test equipment. It **writes**:
it builds drives, then edits, deletes, moves and corrupts files between scans. So it
lives outside `drivefusion/`, is not shipped, and is never imported by the product. That
last point is asserted in `tests/test_vv.py`.

---

## 1. Quick start

```bash
# Simulated: N folders on one disk, each registered as its own physical drive
python -m vv all --out D:\vv\run1 --drives 6

# Simulated, each drive capped at 2 GB (one at 500 MB) so the host disk is not crowded
python -m vv all --out D:\vv\run2 --drives 6 --drive-size 2GB --drive-size D03=500MB

# Physical: one empty folder per real device (one --drive-path per drive)
python -m vv all --out C:\vv\phys1 ^
    --drive-path D01=E:\DriveFusionVV  --drive-path D02=F:\DriveFusionVV ^
    --drive-path D03=G:\DriveFusionVV
```

`all` = `generate` + `run`. Each run writes `report.md`, `results.json` and `junit.xml`
under `<out>/runs/<timestamp>/`. The exit status is 0 when every applicable test passes,
1 when any test fails or errors, and 2 when the bench itself was misused (a non-empty
target folder, a drive that is missing).

| Command | What it does |
|---|---|
| `generate --out DIR [--drives N [--drive-size [Dnn=]SIZE …] \| --drive-path [Dnn=]PATH …] [--seed S] [--files-per-drive K] [--large-files L]` | Build a fresh randomized dataset and its ground-truth `manifest.json` |
| `run DIR [--drivefusion-src CHECKOUT] [--drive-path Dnn=PATH] [--min-copies 2]` | Run a Drive Fusion version through the dataset and judge it |
| `evaluate DIR [--observed observed.json]` | Re-judge the latest run, or any `observed.json` |
| `reset DIR [--drive-path Dnn=PATH]` | Restore a used dataset to exactly its generated state |
| `clean DIR` | Delete the dataset and its drive folders |

A dataset is **single-use**: a run edits and corrupts it on purpose. A second `run` is
refused until you `reset` it. `reset` rebuilds every byte from the manifest, which is how
two versions are measured against **the same** data (§6).

## 2. Simulated and physical drives

| | Simulated (default) | Physical (`--drive-path`, once per drive) |
|---|---|---|
| Where the drives are | `<out>/drives/D01 … Dnn` on one disk | One empty folder per real device |
| How Drive Fusion sees them | Each folder is registered as its own volume, with a mock maker, model and serial. With 3+ drives, two of them are registered as partitions of one device (one serial); with 4+, one other sits behind a bridge that reports no identity at all (R-RED-06). The choice comes from its own random stream, so the rest of a seed's dataset is unchanged. Each identified device is also given a purchase month and disaster zone (recorded as the user would, with `drives edit`) and mock smartctl output, which the adapter hands to Drive Fusion's own SMART parser in place of running smartctl (R-HL-01…04; SMART reads are shelved in the product, D11, but the code still runs here) | Drive Fusion's real discovery identifies each one, exactly as `drivefusion scope add` does |
| Filesystems | Labelled NTFS or exFAT, half each as §1 specifies (with an odd drive count the extra one is either, at random). Any set of two or more drives therefore has at least one of each, so the hardlink and symlink scenarios always have an NTFS drive. Which drive gets which follows from the seed. exFAT-labelled drives get no hardlinks or symlinks, as on real exFAT | Whatever the device really is: NTFS, exFAT, FAT32, ext4… |
| Size | Unlimited by default, or set with `--drive-size` (§2.1). Registered as the drive's capacity | What Windows reports for the volume |
| Guard | — | **Refused** at `run` if Drive Fusion can't identify a device (no serial, no partition table): the bench couldn't know its true copy counts. **Refused** at `generate` and again at `run` if two roots resolve to the same physical device. Two folders on one thumb drive are one copy, so Drive Fusion would be right to count one, and every cross-drive expectation would be wrong |

### 2.1 Drive sizes and disk space

**`--drive-size` (simulated only)** gives every simulated drive a capacity:
`--drive-size 2GB` for all of them, `--drive-size D03=500MB` for one. An override wins
whatever order the flags come in. KB/MB/GB/TB are decimal, as drive makers label them;
KiB/MiB/GiB/TiB are binary.

- **The dataset is planned to fit.** Each drive's peak footprint is computed: the baseline
  files plus everything added or grown between scans, every file rounded up to whole clusters.
  If any drive is over its size, all file sizes are scaled down and the plan is rebuilt, until
  every drive fits. Copies share content across drives, so one scale applies to all of them,
  and the smallest drive sets it. The generator prints the scale it used; `size_scale` is in
  the manifest, and the reproduce command in every report rebuilds the same dataset at the
  same scale.
- **Shrinking never removes a test.** Sizes meant to exercise the tier-2 full hash never shrink
  below twice the quick hash's small-file threshold (384 KiB). A smaller drive gets smaller
  large files, never none.
- **Too small is refused, cleanly.** File and folder *counts* set a floor: each takes at least
  one cluster. Below it, `generate` says how much the drive needs, suggests a larger size or a
  smaller `--files-per-drive`, and removes the folders it created.
- **Drive Fusion sees a real-looking drive.** The size is registered as the drive's capacity,
  free space as that size minus the baseline data, and the cluster size as Windows' format
  default for that filesystem and size: NTFS 4 KiB; exFAT 4 KiB up to 256 MB, 32 KiB up to
  32 GB, 128 KiB above. A simulated exFAT drive therefore carries the cluster slack a real one
  would (PLAN §6.7).

Without `--drive-size`, simulated drives have no capacity in the catalog. The Drives screen
shows them as unknown rather than inventing a number.

**Free space is checked in both modes, before anything is written.** Each location's peak
host usage is summed per filesystem; simulated drives all share the host disk. Every
filesystem must keep a reserve free afterwards: a tenth of its size, capped at 1 GiB. A
dataset that would break that reserve on the host disk, or on a physical thumb drive, is
refused, and the folders created for it are removed.

**Where the dataset folder goes.** Drive Fusion won't keep its catalog on a drive it
scans, unless that's the system drive. In physical mode the catalog lives under `--out`, so
put `--out` on the system drive or on a device that is not one of the `--drive-path`
devices, as in the quick-start example (`C:\vv\phys1` with drives on `E:`, `F:`, `G:`).

**Preparing physical drives.** Give each device an empty folder, such as
`E:\DriveFusionVV`; the drive root itself already holds `System Volume Information`. The
generator refuses any folder that has anything in it, so it cannot overwrite data. A
default dataset is roughly 70–90 MB and ~150 files per drive (`--files-per-drive` and
`--large-files` scale it). All drives must be connected for `generate` and for `run`. If a USB drive
comes back under a different letter, pass `--drive-path D02=H:\DriveFusionVV` to `run`
or `reset`; the manifest is updated.

**What each filesystem can hold.** Each drive's filesystem is probed before planning, and
scenarios that need a capability are placed only where the probe succeeds:

| Capability | NTFS | exFAT / FAT32 | Linux ext4/tmpfs | Test where absent |
|---|---|---|---|---|
| Hardlinks | yes | no | yes | VV-RED-02 → N/A |
| Symlinks | admin or Developer Mode | no | yes | VV-SC-03 → N/A |
| Unreadable file | no (ACLs not set by the bench) | no | yes, unless running as root | VV-CAT-06 → N/A |

N/A means "this dataset could not test it", never "passed". Use at least one NTFS drive
in a physical set, and run once as a normal user on Linux/macOS, to cover all three.

## 3. Requirements under test

Source of truth: [`vv/requirements.py`](../vv/requirements.py). Each requirement is the
testable form of a statement in PLAN.md. Where the plan only implies a rule, the
derivation is marked so a reviewer can dispute the reading rather than the code.

| ID | PLAN | Requirement |
|---|---|---|
| R-RO-01 | §3.1, §3.2, §14 | Never modifies catalogued data: trees are byte- and metadata-identical across every scan, hash and verify pass |
| R-SC-01 | §4 | Nothing outside the scan scope is catalogued |
| R-SC-02 | §4 | Default exclusions are not catalogued, and the scan reports how many it excluded |
| R-SC-03 | §4, §6.7 | Links are reported, never traversed; a link loop cannot make the walk unbounded |
| R-CAT-01 | §2, §7.1 | Every in-scope file is catalogued with drive, path and size |
| R-CAT-02 | §7.1 | Every directory is catalogued, including empty ones and nested empty chains |
| R-CAT-03 | §7.1 *(derived)* | Zero-byte files are catalogued, and never presented as duplicates of each other |
| R-CAT-04 | §6.7 | Names stored as observed: Unicode, spaces, dots, no extension, leading dot, punctuation, long names, case variants |
| R-CAT-05 | §6.7 | Paths over 260 characters are catalogued |
| R-CAT-06 | §6.7 | Unreadable files are recorded, not fatal, and get no unestablished identity |
| R-CAT-08 | §6.4, §11, REVIEW F-12 | Each volume's "last scanned" time is when its last scan finished |
| R-CAT-07 | §2, §5, §8.1 | Each physical device is a distinct device in the catalog, and the volumes of one device are that one device |
| R-ID-01 | §6.6, §10.4 | Byte-identical files share one identity regardless of name, case, folder, drive or mtime |
| R-ID-02 | §6.6 | Different bytes never share an identity, including same name, same size, and differences outside the quick-hash samples (tier 2) |
| R-ID-03 | §6.6, §8.1 | Only shared-size files (or unchanged previously identified ones) are hashed; shared-size files not yet identified are reported as unhashed |
| R-RED-01 | §8.1 | Copies are counted over distinct drives, never paths |
| R-RED-02 | §6.7, §8.1 | A hardlink is not a second copy |
| R-RED-03 | §8.1, REVIEW SR-12 | The under-protected set is exactly the content on fewer drives than the policy minimum, including every non-empty unique-size file as a known single copy |
| R-RED-04 | §8.1, §3.3 | Reclamation is exactly same-drive duplication; reclaimable = (physical − drives) × size |
| R-RED-05 | §8.1, §11, REVIEW SR-12 | Headline numbers agree with the ground truth, single-copy files included |
| R-RED-06 | §8.1, REVIEW F-05 | Two volumes on one device are one copy; a volume whose drive nothing identifies adds no proven copy and is never counted as same-drive waste |
| R-INC-01 | §6.3, §6.4 | A rescan reflects additions and modifications; a modified file loses its old identity |
| R-INC-02 | §7.1 (FAIR A2) | Vanished files and folders are tombstoned with metadata kept, not removed |
| R-INC-03 | §6.4 | Moved/renamed content is recognized as the same content at a new path |
| R-FIX-01 | §8.1 | Silent corruption (bytes changed, size and mtime not) is reported |
| R-FIX-02 | §8.1 | Verification has no false positives and covers both hash tiers |
| R-FIX-03 | §8.1 | Each fixity incident states how many drives hold the content |
| R-FIX-04 | §8.1, REVIEW F-06 | A file edited since the last scan (new modification time) is reported as modified, never as corruption |
| R-HL-01 | §8.2, ROADMAP M5.3 | A drive that returns no SMART data is recorded as unknown, never as zeros, and its failure rate is never labelled measured; one that does is recorded with exactly the counts it reported |
| R-HL-02 | §8.2 | Two copies on drives from one purchase batch have a higher P(loss) than the same drives from different batches; drives that can fail together (one batch, one disaster zone) are correlated as specified |
| R-HL-03 | §8.2, ROADMAP M5.4–M5.5 | Each drive's failure rate, each drive set's P(loss) and the expected bytes lost a year equal independent arithmetic over the planted metadata and the shipped rate and factor tables |
| R-HL-04 | §3.2, ROADMAP M5.3 | smartctl is never given a state-changing argument |
| R-COL-01 | §8.4 | Inference only proposes: after it runs, no collection or membership rule exists that a person did not create |
| R-COL-02 | §8.4 | Every proposal carries its evidence, and the evidence is true: file count, byte count and date span of the proposed subtree |
| R-COL-03 | §8.4 | Proposals never nest, and no scan root is proposed |
| R-COL-04 | §8.4 | Each folder of a planted project series is proposed as itself, not as an enclosing or enclosed folder |
| R-COL-05 | §8.4 | A confirmed collection's members are exactly the live files under its folders, including files that appear there after it was confirmed |
| R-VER-01 | §8.1 | *Planned (M6):* near-duplicates grouped into version sets. Test data is planted now; the test reports DEFERRED |

Requirements for milestones not yet built (the rest of M6, the planner, exports) are not
listed. Each new milestone should add its requirements and tests here
(§9).

## 4. Dataset design

A dataset is fully described by its `manifest.json`: drives, every file with its
content, every folder, links, excluded entries, mutations, corruptions, and the
**scenario** each item belongs to. File bodies are never stored. Each distinct content is
a recipe (type + size, or base + edits) whose bytes are a pure function of
`(seed, content key)`. That gives the two properties V&V needs together:

- **Different every run.** The seed is drawn fresh (48 random bits) unless `--seed` is
  given. Folder trees, names, sizes, types, timestamps, which drives get which copies,
  and how many instances of each scenario appear all come from it.
- **Reproducible on demand.** The report prints the seed and the exact command that
  rebuilds the dataset; `reset` restores a used one byte for byte.

Bodies carry real magic numbers: JPEG, PNG, TIFF, CR2/NEF, PDF, Office/ZIP, 7z, MP4/MOV,
WAV, MP3, HDF5, NPY, FITS, SQLite, plus CSV/JSON/XML/HTML/Markdown/code text. A dataset
looks like an archive drive to anything that sniffs formats, although Drive Fusion is
only ever asked about bytes. Timestamps fall on 2-second boundaries so FAT32 stores them
exactly.

### 4.1 Scenarios

*What* each trap is stays fixed; *where*, *how big*, *what type* and *how many* are
randomized. Every dataset therefore tests every requirement its drives can physically
support. `tests/test_vv.py` asserts that.

| Scenario | What is planted | Requirements |
|---|---|---|
| filler | Random project-style folder tree per drive (Projects/PRJ‑0042/raw, Photos/2019, Users/alice …) with mixed-type files; ~15% reuse an existing size with different bytes | R-CAT-01 |
| cross-drive-copy | One content on 2…N drives; half mirrored at the same path, half scattered with different names and mtimes | R-ID-01, R-RED-01 |
| all-drives-copy | One content on every drive | R-ID-01, R-RED-01, R-RED-03 |
| same-drive-dupes | 2–4 copies on one drive only: many paths, one copy, under-protected | R-RED-01, R-RED-04 |
| partition-copy | Content on the two partitions of one simulated device and nowhere else: one copy, and the second is same-drive waste | R-RED-06 |
| unverified-copy | Content on an identified drive and on the drive nothing identifies: one proven copy, and no waste | R-RED-06 |
| mixed-dupes | 2 copies on drive A + 1 on drive B | R-RED-01, R-RED-04 |
| renamed-copy | Same bytes, different name *and* extension (`.jpg` → `.jpeg`, `.csv` → `.txt`) | R-ID-01 |
| case-variant | `Report_1234.PDF` vs `report_1234.pdf` | R-ID-01, R-CAT-04 |
| same-name-diff-size | Same relative path on two drives, different content and size | R-ID-02 |
| same-name-head-diff / -tail-diff / -one-byte | Same path, **same size**, bytes differ at the start, at the end, or in one random byte | R-ID-02 |
| same-name-hidden-diff | Same path, same size (>700 KB), one byte differs **between the quick hash's head and midpoint samples**: only the tier-2 full hash can separate them | R-ID-02 |
| same-size-diff-name | Unrelated contents of identical size | R-ID-02 |
| unique-size | A size reserved so no other file can ever have it: must never be opened, and must be reported as a single copy | R-ID-03, R-RED-03 |
| empty-files | Zero-byte files, some with the same name on several drives (`__init__.py`, `.keep`) | R-CAT-03 |
| empty-dirs | Empty folders and nested empty chains (`TODO/nested/deeper`) | R-CAT-02 |
| hardlinks | One inode at 2–3 paths, sometimes also copied to another drive | R-RED-02 |
| symlinks | File link, folder link, and a folder link that points at its own parent (a loop) | R-SC-03 |
| excluded | `$RECYCLE.BIN\S‑1‑5‑21…\$R….docx`, `System Volume Information`, `.Trash‑1000`, `pagefile.sys`, `~$report.docx`, `cache_….tmp`, mostly holding copies of real content so a leak would move the counts | R-SC-02 |
| unscoped | Copies of catalogued content in a folder outside every scope root | R-SC-01 |
| odd-names | `résumé – final (v2).docx`, `数据分析 结果.csv`, `notes 📓 2024.md`, `data.v2.final.reviewed.csv`, `README`, `.hidden_settings`, `O'Brien & Sons #3 [draft].pdf`, a 199-character name … | R-CAT-04 |
| deep-path | A file more than 280 characters below the drive root, with a copy elsewhere | R-CAT-05 |
| large-copy | 1.5–6 MB files copied across drives: the full-hash path | R-ID-01, R-ID-02 |
| version-chain | v1; v2 = v1 + appended bytes; v3 = v2 with an edit | R-ID-02, R-VER-01 |
| unreadable | A permission-denied file whose readable twin shares its size, so hashing *tries* to open it | R-CAT-06 |
| drive-health | Per simulated device (not files): a purchase month, a disaster zone or none recorded, and what its SMART says: healthy, failing (48 reallocated, 8 pending, 2 uncorrectable) or hidden behind a bridge. Partitions of one device share it; an unidentified drive has none. Two identified devices are made one purchase batch (same maker, model and month). From its own random stream | R-HL-01, R-HL-03, R-HL-04 |
| batch-copy | Content on the two batch drives and nowhere else | R-HL-02, R-HL-03 |
| project-series | On one drive, `Projects/<topic> series/` holding 3–4 folders named `<year>-<month> <topic>`, each with 6–9 files of one type dated within 20 days of the folder's month, and one more file added to the first folder between scans (unless an earlier change removes it). Planned last, so the rest of a seed's dataset is unchanged. The adapter accepts every proposal after the baseline, as a person would | R-COL-01…05 |
| cross-batch-copy | Content on one batch drive and a drive from another batch | R-HL-02, R-HL-03 |

### 4.2 Between scans (the rescan phase)

| Mutation | Requirements |
|---|---|
| New files with new content | R-INC-01 |
| A copy of single-drive content added to a second drive: it must leave the under-protected list | R-INC-01, R-RED-01, R-RED-03 |
| A new copy of **large content that was already fully hashed** | R-INC-01, R-ID-01 |
| Two files edited in place (same size or grown), with a new mtime | R-INC-01, R-ID-02 |
| One of exactly two cross-drive copies deleted: the content must *join* the under-protected list; plus random deletions | R-INC-02, R-RED-03 |
| A file in a copy set moved and renamed into a new folder | R-INC-03 |
| A whole folder subtree deleted | R-INC-02 |
| A new empty folder | R-CAT-02 |

### 4.3 Before verification

Up to four files (2–4 in practice) from duplicate groups the mutations did not touch get
one byte inverted.
Size and modification time are restored exactly; nothing in the metadata says the file
changed. At least one target is a small file (≤ 192 KiB, exact quick hash) and one a
large file (full hash) when the dataset has both.

Then 1–2 files from other stable duplicate groups are edited the ordinary way: a byte
changes and the modification time moves on two seconds. They are edits, not corruption
(R-FIX-04), and are left out of VV-FIX-02's verifiable floor.

## 5. Test procedure

```
snapshot ─ BASELINE: register drives · scan · hash · record ─ snapshot   (VV-RO-01@baseline)
bench applies mutations
snapshot ─ RESCAN:   scan · hash · record                   ─ snapshot   (VV-RO-01@rescan)
bench corrupts files silently
snapshot ─ VERIFY:   fixity pass · record                   ─ snapshot   (VV-RO-01@verify)
evaluate observed.json against the oracle → report.md / results.json / junit.xml
```

The snapshot hashes every byte and records kind, size, mtime and mode of every entry
under every drive root and the unscoped folder. It is a separate implementation from
`drivefusion.core.fsio`, so the instrument is not the thing it checks. Access times are
not compared, since a filesystem without `noatime` may update them on read; the
product's own no-touch test covers those.

**The oracle** ([`vv/oracle.py`](../vv/oracle.py)) imports nothing from `drivefusion`.
It derives expectations from the ground truth and from PLAN's rules: identity by true
bytes, coverage by shared size (§6.6), tier 0 carrying identity across an unchanged
rescan, copies over distinct physical devices with only identified ones proven, hardlinks
once (§8.1). An oracle built from the
tool's code would agree with the tool's bugs.

| Test | Phases | Requirements | Passes when |
|---|---|---|---|
| VV-RO-01 | baseline, rescan, verify | R-RO-01 | The before/after snapshots of the phase are identical |
| VV-SC-01 | baseline | R-SC-01 | No catalogued path lies outside a scope root |
| VV-SC-02 | baseline, rescan | R-SC-02 | No excluded entry or anything beneath it is catalogued, and each drive's excluded count equals the planted count |
| VV-SC-03 | baseline, rescan | R-SC-03 | Nothing is catalogued at or through a link, and each drive's links-not-traversed count is exact |
| VV-CAT-01 | baseline | R-CAT-01 | The live file set equals the expected set, with exact sizes |
| VV-CAT-02 | baseline, rescan | R-CAT-02, R-INC-02 | The live directory set is exact; deleted folders are tombstoned |
| VV-CAT-03 | baseline | R-CAT-03 | Every empty file is present at size 0 and in no multi-path group |
| VV-CAT-04 | baseline | R-CAT-04 | Every odd-name and case-variant file is present under its exact name |
| VV-CAT-05 | baseline | R-CAT-05 | Every file beyond 260 characters is present |
| VV-CAT-06 | baseline | R-CAT-06 | Unreadable files are present and carry no identity |
| VV-CAT-07 | baseline | R-CAT-07 | Every drive is registered; drives on different true devices resolve to different devices, and the partitions of one device to one |
| VV-ID-01 | baseline, rescan | R-ID-01 | Every set of identical files carries exactly one identity |
| VV-ID-02 | baseline, rescan | R-ID-02 | No identity covers two different true contents |
| VV-ID-03 | baseline, rescan | R-ID-03 | The set of identified files equals the spec's coverage set exactly |
| VV-RED-01 | baseline, rescan | R-RED-01 | Each content's reported drive count is its true distinct-drive count |
| VV-RED-02 | baseline, rescan | R-RED-02 | Hardlinked content reports its true physical copy count |
| VV-RED-03 | baseline, rescan | R-RED-03 | The under-protected list equals the expected list, compared as path sets; single-copy files are matched by their one path |
| VV-RED-04 | baseline, rescan | R-RED-04 | The reclamation list and each reclaimable byte count are exact |
| VV-RED-05 | baseline, rescan | R-RED-05 | Every headline number in `redundancy_summary` is exact, including `single_copy_files` |
| VV-RED-06 | baseline, rescan | R-RED-06 | For every content on a shared device or an unidentified drive: proven and possible drive counts are exact, and it is reported as same-drive waste exactly when it is. N/A with fewer than three drives |
| VV-INC-01 | rescan | R-INC-01, R-CAT-01 | The rescan file set and sizes are exact; no edited file keeps its old identity |
| VV-INC-02 | rescan | R-INC-02 | Every deleted file is tombstoned, none removed, none tombstoned wrongly |
| VV-INC-03 | rescan | R-INC-03 | A moved file's old path is tombstoned and its new path shares the identity of the content's other copies |
| VV-FIX-01 | verify | R-FIX-01 | Every corrupted file is reported as `mismatch` |
| VV-FIX-02 | verify | R-FIX-02 | No intact file is reported, and at least every identified small file plus every corrupted large file was checked |
| VV-FIX-03 | verify | R-FIX-03 | Each incident's drive count equals the drives truly holding the content |
| VV-CAT-08 | baseline, rescan | R-CAT-08 | Every drive's "last scanned" is set, not before the run began, and not moved backwards by the rescan. N/A for versions that don't report it |
| VV-FIX-04 | verify | R-FIX-04 | No edited file appears as an incident, and each is in the tool's `modified` list where the tool reports one |
| VV-HL-01 | baseline (simulated) | R-HL-01 | Every hidden-SMART drive is recorded `unknown` with no numbers and a rate not labelled measured; every healthy or failing one is `measured` with exactly the planted counts and power-on hours |
| VV-HL-02 | baseline | R-HL-02 | Each multi-drive set's correlation equals the oracle's (same batch ×5, same zone ×10 per extra drive, as shipped), and the batch pair's P(loss) is higher than the oracle's for the same two drives from different batches. N/A with fewer than two identified devices |
| VV-HL-03 | baseline | R-HL-03 | Each drive's AFR and confidence, each drive set's P(loss) and bytes, and the total expected loss equal the oracle's independent arithmetic. The oracle shares only the data (the tool's shipped rate and factor JSON), not the code |
| VV-HL-04 | baseline | R-HL-04 | Every smartctl command the tool built for the planted reads uses no state-changing option (the bench's own list) |
| VV-COL-01 | baseline | R-COL-01 | No collection and no rule exists after inference |
| VV-COL-02 | baseline | R-COL-02 | Each proposal's file count, bytes and date span equal the manifest's for that subtree, and it has reasons and sample paths |
| VV-COL-03 | baseline | R-COL-03 | No proposal is a drive root or inside another proposal |
| VV-COL-04 | baseline | R-COL-04 | Every planted series folder is proposed exactly (an enclosing or enclosed proposal is named in the failure) |
| VV-COL-05 | rescan | R-COL-05 | Each confirmed collection's member files and bytes equal the rescan truth under its folders, the late file included |
| VV-VER-01 | — | R-VER-01 | DEFERRED until M6 |

Identity is always compared as **a partition of paths**, never by catalog id. "These three
paths are one content" is the claim a user acts on. When a redundancy number is wrong
*because* an identity was split or merged, the failure says so and points to
VV-ID-01/02, so one root cause does not read as five unrelated defects.

### 5.1 Verdicts

| Verdict | Meaning |
|---|---|
| PASS | The test examined at least one item and every item matched |
| FAIL | At least one item did not match; the report lists the first 15, each tagged with its scenario |
| N/A | Nothing in this dataset could exercise it (for example, no drive can hold a hardlink). **Never counted as a pass** |
| ERROR | The phase raised; the traceback is in `observed.json` and the phase's tests are unverified |
| DEFERRED | The requirement belongs to a later milestone; its data is planted already |

The run passes when there is no FAIL and no ERROR. The report's traceability table rolls
every test up to its requirement, so the answer to "is R-RED-03 met by this build?" is one
row.

## 6. Comparing Drive Fusion versions

`run --drivefusion-src <checkout>` tests the Drive Fusion in that directory instead of the
installed one. With `reset`, two versions are measured against **byte-identical** data:

```bash
python -m vv generate --out ds --drives 5
python -m vv run ds --drivefusion-src ../drive-fusion-v0.1.0     # baseline build
python -m vv reset ds
python -m vv run ds --drivefusion-src ../drive-fusion-candidate  # candidate build
diff ds/runs/*/results.json                                       # or compare report.md
```

[`vv/adapter.py`](../vv/adapter.py) is the only module that knows how Drive Fusion is
invoked and how its catalog is laid out. When a later version changes its API or schema,
the adapter changes, and datasets, oracle and evaluator stay as they are. A different
front end (the frozen `drivefusion.exe`, say) can be judged by any adapter that writes the
same `observed.json`, then `python -m vv evaluate ds --observed that.json`.

`observed.json` (schema `drivefusion-vv-observed/1`) holds, per phase: `files` as
`[drive, path, size, content_id|null, vanished, read_state]`; `dirs` as
`[drive, path, vanished]`; the tool's own `groups_all`, `duplicates`, `under_protected`
and `reclamation` groups (`content_id`, `size`, `paths`, `drives`, `physical_copies`,
`reclaimable_bytes`); `summary`; per-drive `scan` counters (`excluded`, `links`, …); and
for `verify`, `checked`, `mismatched` and `incidents` as
`[{drive, path, result, other_copies}]`. Paths are relative to the drive root with `/`
separators. The harness adds `no_touch` diffs and any `errors`.

## 7. First results: Drive Fusion 0.1.0 (defect since fixed)

Sweeps of 20 fresh datasets (1–10 simulated drives) and a physical-mode run across two
real filesystems give the same picture:

- **Baseline and verify: all applicable tests pass.** Cataloguing, exclusions, links,
  odd names, deep paths, identity (tier-2 hidden differences included), coverage,
  drive/hardlink counting, under-protection, reclamation, headline numbers,
  tombstoning, corruption detection, and the no-touch guarantee.
- **Rescan: VV-ID-01 fails on every dataset**, with consequences in VV-RED-01/03/04/05,
  and VV-INC-03 when the moved file is large.

**The defect.** A large file (> 192 KiB) whose duplicate group was fully hashed in an
earlier pass gets a *new* copy, or is moved, before the next pass. That file is split
from its twins and reported as separate content. The `report` then states one copy too
few: content that is safe is listed as under-protected, and real same-drive waste is
missed.

**Cause.** The full pass reassigns a group's files from the quick-hash-only content row to
a full-hash row, leaving the quick-only row with no files. On the next pass,
`Catalog.upsert_content(full_hash=None)` matches the new file to that orphaned row. Then
`Catalog.quick_hash_collisions()` only looks at quick-only rows holding more than one live
file, so the lone file is never fully hashed and never meets its twins.

**Evidence that this is the tool and not the bench.** Before any fix, the same datasets
(restored with `reset`) were run against a scratch copy of Drive Fusion carrying only the
candidate change below. Every test passed.

**Fixed.** `quick_hash_collisions()` now also treats a quick-only row as ambiguous when
another content row with live files shares its size and quick hash, so the late file is
fully read and meets its twins. A file with the same sampled blocks but different bytes
still gets content of its own. The extra check is two indexed lookups, made only for
single-file groups. Regression tests: `tests/test_identity.py`
(`test_a_new_copy_…`, `test_a_moved_copy_…`, `test_a_late_lookalike_…`) and
`test_a_full_run_passes_the_rescan` in `tests/test_vv.py`.

## 8. The bench's own safety

Physical mode runs on real devices, so the bench guards against itself:

- It writes only beneath the drive folders and the unscoped folder named in the manifest.
- `generate` refuses any target that is not new or empty, and refuses two drives on one
  device *before* writing anything.
- `reset` and `clean` walk each drive folder first and refuse to delete it if it holds
  anything the manifest did not create.
- `run` checks that the dataset still matches its manifest before trusting it, and
  refuses a dataset that was already run.

`tests/test_vv.py` checks the bench as an instrument. Two fresh datasets must differ, and
a seed must reproduce its dataset byte for byte. Every requirement must be planted. The
oracle must apply PLAN's counting rules to a hand-checkable dataset. The evaluator must
catch planted defects: a merged identity, a missing file, a wrong headline number, a
touched tree, missed corruption. And a test that examined nothing must never pass.

## 9. Limitations and extending

- **Scale.** Datasets are sized for minutes, not the §13 budgets; performance is
  `tools/benchmark.py`'s job.
- **Enumeration path.** Scans use the unprivileged walker. The elevated USN
  path and journal-delta rescans need an elevated NTFS VHDX (PLAN §14) and are not yet
  driven by the bench.
- **Not planted:** cloud placeholders (need OneDrive), sparse/compressed files, ADS,
  dirty-volume flags. Unreadable files are N/A on Windows and as root.
- **Session model.** All drives are connected for a run, unlike the one-at-a-time use in
  PLAN §17; per-drive sessions are a natural next step.
- **Readings worth reviewing:** R-CAT-03 (empty files are never duplicates) is derived,
  not stated. R-FIX-03 counts the drives holding the content *including* the damaged
  copy, which matches what `drivefusion report` prints; PLAN §8.1 asks for incidents to
  *name* repair sources, which would need a stronger test.

**Adding coverage.** A new requirement goes in `vv/requirements.py`; a new trap is a
`Builder` method in `vv/generator.py` plus an entry in `SCENARIO_REQUIREMENTS`; its
expectation goes in `vv/oracle.py`, its check in `vv/evaluate.py`. Requirements for
milestones not yet built go in as `status="planned"`, with data planted, so the next
version can be measured the day it lands.
