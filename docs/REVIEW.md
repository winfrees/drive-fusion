# Drive Fusion — Safety, File-Integrity and Security Review

| | |
|---|---|
| **Scope** | Requirements in [`PLAN.md`](PLAN.md) and [`README.md`](../README.md), and the code at `main` (`ca6c4e0`, milestones M0–M4) that implements them |
| **Focus** | (1) safety of user data on catalogued media, (2) correctness of the file and redundancy facts the tool reports, (3) security of the privileged and packaging surface |
| **Method** | Every source module read in full; lint and full test suite run on Linux (291 passed, 5 Windows-only skipped) and checked against the latest Windows CI log (293 passed, 3 skipped); every finding below reproduced with [`tools/review_repro.py`](../tools/review_repro.py) or shown directly in code. Each claim was checked three times: against the code, against a running reproduction, and against the test that would be expected to catch it |
| **Date** | 2026-10-27 |
| **Status update** | 2026-10-28: F-01 to F-04 fixed, each with regression tests in `tests/`; R1–R4 now print `NOT FOUND`. Status columns below record it. The narrative in §1 and §2.3 describes the code as reviewed. Re-checked at `ce03f64` the same day: R5–R12 still `FOUND`, so F-05 … F-16 stayed open. [ROADMAP.md](ROADMAP.md) Track A schedules them. 2026-10-28, Track A item A2: F-08, F-10, F-14 and F-16 fixed, each with regression tests; R8 (all seven probes), R9, R10 and the new R13 (F-16) now print `NOT FOUND` / `caught` |

---

## 1. Summary

Drive Fusion's central promise, *"never modifies, moves, or deletes your data"*, **holds for
every code path that exists today**. User files are opened through a single gateway with
`GENERIC_READ` and `OPEN_EXISTING`; volume handles are read-only; outside the catalog store
(`core/store`, which creates and backs up its own database), nothing calls a mutating
filesystem API; and a no-touch regression test confirms that a fixture tree stays
byte-identical through scan, hash, verify, report and browse on both Linux and Windows CI.

The weaknesses are elsewhere. They sit in **the facts the tool reports** and in **the edges of
the safety argument**:

* **Wrong answers that look like correct ones.** Four defects can make a report show
  *more* protection or *less* data than really exists. That is the dangerous direction for a
  preservation tool, because a user may act on it, for example by retiring a drive they think
  is redundant ([F-01](#f-01), [F-02](#f-02), [F-04](#f-04), [F-05](#f-05)).
* **One way to write to scanned media.** The catalog database may be placed inside a scanned
  tree, and nothing prevents it ([F-03](#f-03)).
* **A safety argument stronger than its mechanisms.** The documents described the four
  read-only layers as *independent*. In practice the lint has blind spots that no other layer
  covers ([F-08](#f-08)).
* **Documentation ahead of the code.** Several features described as done are libraries with
  no user entry point, or are not implemented: the per-scan UAC prompt, incremental rescan,
  the audit log, and first-run disclaimer acknowledgement ([F-09](#f-09)). This change corrects
  the documents. It does not change any code.

Figure 1 shows where each finding sits in the data flow.

```mermaid
flowchart LR
    subgraph media["User media (must never change)"]
        V1[("NTFS / exFAT volumes")]
    end
    subgraph app["Drive Fusion process (as invoker)"]
        GW["core/fsio.py<br/>read-only gateway"]
        EN["enum/ walker,<br/>dirinfo, winio"]
        SC["scan/ scanner<br/>+ merge"]
        ID["identity/<br/>hash + verify"]
        AN["analysis/<br/>reports"]
        ST[("core/store<br/>catalog.db")]
    end
    subgraph priv["Elevated (not yet wired)"]
        HE["dfscan-helper.exe"]
    end
    V1 -- "GENERIC_READ" --> GW
    V1 -- "GENERIC_READ" --> EN
    GW --> SC
    EN --> SC
    SC --> ST
    GW --> ID
    ID --> ST
    ST --> AN
    HE -. "raw volume handle<br/>(F-07)" .- V1
    ST -. "catalog path inside<br/>scanned tree (F-03)" .-> V1
    SC -. "path not bound to<br/>volume GUID (F-01, F-02)" .- V1
    RP(["Report you act on"])
    AN -- "counts overstate<br/>protection (F-04, F-05)" --> RP
```

*Figure 1 — Data flow and where each finding attaches. Solid lines are reads; dotted lines are
findings.*

### Findings at a glance

| ID | Severity | Area | One-line statement | Reproduced | Status |
|---|---|---|---|---|---|
| [F-01](#f-01) | **High** | File facts | Scans, hashes and verifies follow a stored drive-letter path, not the catalogued volume's identity | R1 | **Fixed** |
| [F-02](#f-02) | **High** | File facts | A directory that cannot be listed, or a root whose drive is unplugged, gets its whole subtree tombstoned as "vanished" | R1, R2 | **Fixed** |
| [F-03](#f-03) | **High** | Safety | The catalog database (plus `-wal`/`-shm`) can be created inside a scanned tree | R3 | **Fixed** for scope roots, and for the export root (ROADMAP M8.1) |
| [F-04](#f-04) | **High** | File facts | Files of unique size, which by definition have exactly one copy, are missing from the under-protected report | R4 | **Fixed** |
| [F-05](#f-05) | Medium | File facts | Volumes whose drive serial can't be read each count as a separate drive, which can overstate copies | R6 | **Fixed** |
| [F-06](#f-06) | Medium | File facts | `verify` reports an ordinary edit as a fixity mismatch, because it never compares size or mtime | R5 | **Fixed** |
| [F-07](#f-07) | Medium | Security | The elevation launcher checks only the helper's filename and existence: no signature or hash check, and parameters are joined unquoted | code | **Fixed** |
| [F-08](#f-08) | Medium | Safety | `ro_lint` misses `subprocess`, `os.write`, `io.open`, numeric `os.open` flags, Win32 mutators via ctypes, `sqlite3.connect`, and `getattr` indirection | R8 | **Fixed** |
| [F-09](#f-09) | Medium | Accuracy | Docs describe unwired or unimplemented features as done (corrected by this change) | code | Docs corrected |
| [F-10](#f-10) | Medium | Catalog integrity | The pre-migration backup is a file copy taken while the database is in WAL mode, so it can miss committed data | R10 | **Fixed** |
| [F-11](#f-11) | Medium | File facts | Cloud placeholders that expose the reparse attribute are dropped from the catalog instead of being recorded by metadata | R11 | **Fixed** |
| [F-12](#f-12) | Low | Accuracy | "Last scanned" shows when the volume was added, not when it was last scanned | R12 | **Fixed** |
| [F-13](#f-13) | Low | Usability | Cancel is checked every 10,000 staged files, not per directory | R7 | **Fixed** |
| [F-14](#f-14) | Low | Robustness | `CreateFileW`/`FindFirstVolumeW` have no `restype`, so `INVALID_HANDLE_VALUE` is never matched | R9 | **Fixed** |
| [F-15](#f-15) | Low | Accuracy | The coverage line counts unlistable directories as unread *files* | R1 | **Fixed** |
| [F-16](#f-16) | Low | Safety | `open_read` checks attributes without following links, then opens the path *with* link following | R13 | **Fixed** |

**Severity scale.** *High*: can write to user media, or can lead a user to believe data is
safer than it is. *Medium*: a wrong or misleading result in a narrower case, or a security
weakness that is latent today. *Low*: misleading presentation or degraded diagnostics with no
wrong safety conclusion.

---

## 2. User needs, requirements and specification

The table traces each **user need** (what the person relying on the tool needs to be true) to
**system requirements** (testable statements derived from the plan) and to the
**specification as built** (the mechanism in code), with how it is **verified** and an
assessed **status**.

Status legend: **Met**, meaning implemented and verified · **Partial**, meaning implemented
with a defect or gap · **Not met**, meaning implemented but the requirement is violated ·
**Planned**, meaning not implemented, scheduled for a later milestone · **Unwired**, meaning
implemented as a library with no user-facing entry point.

### 2.1 User needs

| ID | User need |
|---|---|
| **UN-1** | My data is never modified, moved, renamed or deleted, and the tool writes nothing onto my drives. |
| **UN-2** | I can find out what exists and on which physical drive, even when that drive is unplugged. |
| **UN-3** | I can trust the number of real copies the tool reports, counted over distinct physical drives. |
| **UN-4** | I am told about silent corruption, and not shown false alarms. |
| **UN-5** | Only what I explicitly choose is scanned. |
| **UN-6** | The tool does not need, or misuse, administrator rights. |
| **UN-7** | The catalog itself survives crashes and upgrades. |
| **UN-8** | Nothing leaves my machine, and cloud-only files are never downloaded. |
| **UN-9** | What the tool does not know is shown as unknown: coverage, staleness and unhashed files. |
| **UN-10** | Long operations stay responsive and can be cancelled. |
| **UN-11** | I am not misled into reading a scorecard as certification: it reports how far my records are consistent with FAIR and NDSA, nothing more. |

### 2.2 Requirements and specification traceability

| Req | Need | Requirement | Specification as built | Verification | Status |
|---|---|---|---|---|---|
| SR-01 | UN-1 | No module outside `core/store` and `core/export` may invoke a filesystem mutation. | `core/fsio.py` exposes only `scandir`, `stat`, `open_read`; `tools/ro_lint.py` rules DF001–DF007 run as a CI step. | `test_ro_lint.py`; CI "Read-only lint" step; audit-hook guard in `test_no_touch.py` | **Met** ([F-08](#f-08)). Residual: a computed name on an arbitrary `ctypes` receiver |
| SR-02 | UN-1 | User files are opened read-only, without blocking other processes. | `CreateFileW(GENERIC_READ, FILE_SHARE_READ\|WRITE\|DELETE, OPEN_EXISTING)`; `ReadOnlyBinaryFile` raises `ReadOnlyViolation` on `write`/`truncate`. `open_read` refuses links (`O_NOFOLLOW` on POSIX) and checks the opened handle's identity and attributes against the path's ([F-16](#f-16)). | `test_handles_refuse_to_write`; `test_no_touch.py`; `test_fsio.py` | **Met** |
| SR-03 | UN-1 | Volume, directory and device handles cannot write. | `winio.open_volume` / `open_directory`: `GENERIC_READ`, `OPEN_EXISTING`; discovery opens with zero access; DF006 rejects write-access names in raw-handle modules. | Code review; DF006; `test_win32_signatures.py` | **Met**. DF006 matches names only, not numeric literals or creation dispositions. Handle signatures declared ([F-14](#f-14)) |
| SR-04 | UN-1 | The tool writes nothing inside a scope root or on a catalogued volume. | `Catalog()` refuses to open a catalog inside any enabled scope root, checking with an `immutable=1` read before connecting so no `-wal`/`-shm` is created; `Catalog.add_scope_root` (and `scope add` / the GUI before they record anything) refuses a root that contains the catalog (`CatalogLocationError`). Since 2026-10-28 the same checks refuse a catalog on any volume that holds an enabled root, and a root on the catalog's volume, unless that volume is the system drive. The export root follows the same rule (`Catalog.check_export_root`, ROADMAP M8.1). | `test_store.py` (`test_a_root_containing_the_catalog_is_refused`, `test_a_catalog_inside_a_scope_root_is_not_opened`, `test_a_root_on_the_catalogs_data_drive_is_refused`, `test_a_catalog_moved_onto_a_scanned_data_drive_is_not_opened`, and the system-drive cases), `test_cli.py`, `test_gui_shell.py`; R3, `test_export_writer.py` | **Met** for the catalog and the export root ([F-03](#f-03)) |
| SR-05 | UN-1 | An end-to-end regression shows media unchanged after every stage. | `test_no_touch.py` snapshots a fixture tree, runs scan ×2, hash, verify, reports and browse, then re-snapshots. | Linux + Windows CI; the cycle runs under the audit-hook guard | **Partial**: access times asserted only on POSIX with `O_NOATIME`; helper, USN and delta paths not exercised |
| SR-06 | UN-8 | Cloud placeholders are never opened, and are catalogued by metadata. | `open_read` refuses `OFFLINE`/`RECALL_ON_OPEN`/`RECALL_ON_DATA_ACCESS` (`fsio.py:368`); scanner marks `skipped-dehydrated`; hash candidates require `read_state='ok'`. | `test_cloud_placeholders_are_never_opened`, `test_dehydrated_files_are_catalogued_but_not_opened`, `test_hashing_refuses_cloud_placeholders`, `test_reparse_files_are_catalogued_unless_they_are_links` | **Met** ([F-11](#f-11)) |
| SR-07 | UN-8 | No outbound network connections. | No network modules imported anywhere in `drivefusion/`. | Inspection only (no lint rule) | **Met**, not enforced |
| SR-08 | UN-5 | Only explicitly added roots are scanned; nested roots refused; links not traversed; OS churn excluded. | `scope/registry.py` (`validate_new_root`, `ExclusionSet`); walker refuses to descend reparse points. | `test_scope.py`, `test_enum_batchwalk.py` | **Met** |
| SR-09 | UN-2, UN-3 | A scan, hash or verify acts on the catalogued **volume identity**, never on whatever is now mounted at a drive letter. | Root still stored as a path, but `core/scope/binding.bind_roots` resolves the volume at each root (`discovery.identify_volume`) and refuses any root whose `volume_guid` differs or can't be identified. `cmd_scan`, `cmd_hash`, `cmd_verify`, the GUI `scan_body` and `request_hash` scan only bound roots, and `hash_pass`/`verify_pass` take `root_ids` so they read only files under them. Checked once at the start of each pass. | `test_scope.py`, `test_cli.py`, `test_identity.py`, `test_gui_shell.py` | **Met** ([F-01](#f-01)) |
| SR-10 | UN-2, UN-9 | A path that was not examined is *unknown*, not *gone*. | Delta merges use `vanish_scope="staged-dirs"`. Full scans stage every directory they listed in `stage_dir` and merge with `vanish_scope="listed-dirs"`: only files missing from a listed directory, and child directories missing from a listing (with their subtrees), are tombstoned. If the root itself can't be listed, nothing is merged and the scan is recorded as failed. | Delta: `test_enum_delta.py`. Full scan: `test_scanner.py`, `test_cli.py`; R1, R2 | **Met** ([F-02](#f-02)) |
| SR-11 | UN-3 | Copies are counted over distinct physical drives; hardlinks are one physical copy. | Drives are identified by serial or partition table (`drive.identity`). `DRIVES_PROVEN = MAX(COUNT(DISTINCT v.drive_id), 1)` for protection; `DRIVES_POSSIBLE = COUNT(DISTINCT COALESCE(v.drive_id, -v.id))` for reclamation; physical copies = distinct `(volume, frn)`. | `test_analysis.py`, `test_store.py`, `test_discovery_layout.py`; V&V R-RED-06 | **Met** ([F-05](#f-05)) |
| SR-12 | UN-3, UN-9 | The under-protected report includes every item with fewer copies than the target. | `under_protected` adds every live, non-empty file whose size no other live file shares, as one copy on one drive (`single_copy_files`), without hashing it. `redundancy_summary` counts them in its totals and reports them as `single_copy_files`; `unhashed_files` now counts only files whose size is shared. | `test_analysis.py`, `test_cli.py`; R4 | **Met** ([F-04](#f-04)) |
| SR-13 | UN-4 | Fixity separates corruption (bytes changed, metadata unchanged) from modification. | `verify_pass` compares mtime first: changed → `modified`, not hashed, not an incident; unchanged → re-hash and compare. | `test_identity.py`; V&V R-FIX-04 | **Met** ([F-06](#f-06)) |
| SR-14 | UN-6 | The main application runs as invoker and never silently elevates. | `drivefusion.manifest`: `requestedExecutionLevel="asInvoker"` for all three executables. | CI build smoke test | **Met** |
| SR-15 | UN-6 | Before anything is elevated, the binary is proven to be the genuine helper and its arguments can't be reinterpreted. | The build pins the helper's SHA-256 in a module packed inside the main executables; `elevation.request` hashes the helper through a handle that denies write and delete sharing, held until `ShellExecuteW` returns, and refuses a mismatch; an unpinned build refuses a helper in a user-writable folder; parameters via `subprocess.list2cmdline`. CI checks the pin against the shipped helper. | `test_elevation.py` (incl. a Windows-only lock test); CI build step | **Met** ([F-07](#f-07)). Authenticode waits for a signing certificate |
| SR-16 | UN-6 | Elevation is offered per scan, and consent is recorded. | `elevation.request` exists; **no GUI or CLI code calls it**; `scan.elevated` is always written as 0. | n/a | **Unwired** ([F-09](#f-09)) |
| SR-17 | UN-2 | An NTFS rescan reads only what changed (USN delta). | `core/enum/delta.apply_delta`, journal validity logic, helper protocol, all unit-tested; **not called by `cmd_scan` or the GUI**; `decide()` returns `walk` whenever not elevated. | `test_enum_delta.py`, `test_enum_journal.py`, `test_enum_helper.py` | **Unwired** |
| SR-18 | UN-7 | A schema migration first takes a consistent backup of the catalog. | `schema.backup_catalog` snapshots through `Connection.backup()` on the open connection, to a temporary name renamed when complete. | `test_store.py` (`test_backup_includes_transactions_still_in_the_wal`); R10 | **Met** ([F-10](#f-10)) |
| SR-19 | UN-7 | A failed or cancelled scan leaves the previous catalog state. | Staging + one-transaction merge; rollback on exception. | `test_scanner.py`, `test_store.py` | **Met**. Staged rows of a cancelled scan are left behind in `stage_file` |
| SR-20 | UN-9 | Per-volume staleness is shown truthfully. | `Catalog.volumes()` and `root_last_scanned()` give the last finished scan per volume and per root; `scope list`, `status` and the Scope and Drives screens show those. | `test_scanner.py`, `test_cli.py`; V&V R-CAT-08 | **Met** ([F-12](#f-12)) |
| SR-21 | UN-9 | Unknown copy counts are shown as unknown. | GUI renders "—" for files without `content_id`; CLI report prints the unhashed count. | `test_unhashed_files_show_copies_as_unknown` | **Met** |
| SR-22 | UN-9 | Scan coverage (unreadable, excluded, dehydrated, links) is reported. | `ScanCounters.coverage_line()`, with unlistable folders counted apart from files. | `test_scanner.py` | **Met** ([F-15](#f-15)) |
| SR-23 | UN-10 | Cancel takes effect within one directory. | `scan_root(cancel=)` per directory; `hash_pass` / `verify_pass(cancel=)` per file; the GUI passes `Cancellation.raised`. | `test_scanner.py`, `test_identity.py`, `test_gui_shell.py` (a real scan body); R7 | **Met** ([F-13](#f-13)) |
| SR-24 | UN-1 | The UI offers no destructive control. | Widget-tree test bans destructive verbs on every control. | `test_no_control_offers_a_destructive_action` | **Met** |
| SR-25 | UN-11 | First-run disclaimer, acknowledged and recorded. | `core/disclaimer.py`; CLI gate on every command that reads a drive (prompt, `--accept-disclaimer`, env); GUI dialog before the window and on each reading action; acceptance audited with the text's SHA-256. | `test_cli.py`, `test_gui_shell.py` | **Met** |
| SR-26 | UN-7, UN-11 | Append-only, hash-chained audit log. | `core/store/audit.py`: SHA-256 chain over every entry; schema-v6 triggers refuse UPDATE/DELETE; appends under `BEGIN IMMEDIATE`; `drivefusion audit verify`. Truncation of the newest entries is not detectable by the chain alone; the head hash is printed for that. | `test_audit.py` (tamper, deletion, recomputed hash, concurrent writers) | **Met** |
| SR-27 | UN-1 | Exports go only to a new, timestamped folder inside a chosen root. | `core/export/` is a docstring. | n/a | **Planned** (M8) |
| SR-28 | UN-6 | The elevated helper has a minimal, read-only surface. | `helper/__main__.py`: argparse, `query`/`enum`/`delta` modes, `GENERIC_READ` volume handle, frames to stdout. | `test_enum_helper.py` (stand-in child) | **Met** for the code; no real-volume test |

```mermaid
pie showData
    title Requirement status (28 requirements)
    "Met" : 23
    "Partial" : 2
    "Not met" : 0
    "Unwired" : 2
    "Planned" : 1
```

*Figure 2 — Status across the 28 requirements. SR-03 and SR-07 count as Met, with the notes
given in the table.*

### 2.3 Discussion

**The read-only guarantee (UN-1) holds, and the reasons are concrete.** Every handle that
touches user media requests read access only, and uses `OPEN_EXISTING`, so it can't even
create a file. The only writers are the SQLite catalog and a backup copy next to it. The
no-touch test is well built: it fails if its own snapshot observes nothing, and it checks
named landmark paths, so a broken fixture can't pass silently. Its main gap is coverage of
future paths. The elevated helper and the USN delta never run inside it, because they need a
real NTFS volume handle.

What the guarantee does *not* cover is **where the catalog lives** ([F-03](#f-03)). In a
cataloguing tool whose own plan recommends a "portable mode" on a thumb drive, putting the
catalog on a drive that is also catalogued is a realistic mistake. If it happens, every scan
writes `catalog.db-wal` into the tree being observed, and catalogues it.

**The reported file facts (UN-2, UN-3, UN-9) are where the real risk lies.** A preservation
tool can be wrong in two directions. Under-reporting copies is annoying but safe: you make an
extra copy. Over-reporting copies, or failing to list a single-copy file, is unsafe: you
retire a drive believing the data lives elsewhere. Four findings push in the unsafe
direction:

* **[F-01](#f-01)**: if a different drive comes up as `E:`, its files are recorded under the
  old drive.
* **[F-04](#f-04)**: the files most certainly at risk, those whose size is unique, are absent
  from the at-risk list.
* **[F-05](#f-05)**: two partitions on one serial-less USB disk read as two drives.
* **[F-02](#f-02)**: the reverse failure. Data that was merely unreadable is marked gone,
  which then silently removes those copies from every redundancy count.

All four come from the same principle that the code applies well elsewhere but not here:
*identity and absence must be established, not inferred.* The delta path already follows
that principle, through `vanish_scope="staged-dirs"`, and the full-scan path should be
brought into line with it.

**Security (UN-6, UN-8) is in good shape for what is wired, because very little privileged
code is wired.** The main process runs as invoker; the helper is small and read-only; no
network code exists. The elevation launcher ([F-07](#f-07)) is the one component that would
hand out Administrator, and it is not yet connected. It should be hardened before it is: verify
the helper's Authenticode signature or a pinned hash, keep it out of user-writable
directories, quote its parameters, and give it an ACL-protected pipe back to the parent.
Today `SW_HIDE` + `runas` would start a helper whose stdout reaches no one.

**Documentation accuracy (all needs).** The project documents are unusually candid about
limits, which makes the overstated places easier to spot. [F-09](#f-09) lists each one; this
change fixes them in `README.md` and `PLAN.md`.

---

## 3. Findings in detail

Each finding gives the evidence (file and line), a concrete failure scenario, the
reproduction, and a recommended fix. Reproduction IDs (R1–R12) refer to
[`tools/review_repro.py`](../tools/review_repro.py).

<a id="f-01"></a>
### F-01 · High · Scope roots bind to a drive-letter path, not to the volume

**Evidence.** `scope_root.path` stores an absolute path such as `E:\Research`. `cmd_scan`
(`cli/main.py:291`) and the GUI `scan_body` (`gui/screens/scope.py`) pass that text straight
to `scan_root`. `hash_pass` and `verify_pass` rebuild file paths from the same text
(`identity/pipeline.py:62`). Nothing checks that the volume now mounted at `E:` has the
`volume_guid` recorded for the root.

**Failure scenarios.**
1. Drive A is unplugged and `E:` is empty. The root can't be listed, so the scan tombstones
   every file on it ([F-02](#f-02)).
2. Drive B, a backup of A, is plugged in and receives letter `E:`. The scan records B's files
   under **A's** `volume_id`, and therefore under A's drive. A now looks current; B's copies
   are attributed to A, and copy counts merge two physical drives into one, or split one
   into two.
3. `hash` or `verify` with drive B at `E:` reads B's bytes and records them as A's content or
   fixity.

**Reproduction.** R1 (missing root → 5 of 5 files tombstoned).

**Fix.** At the start of every scan, hash and verify pass, resolve the root's current volume
GUID (`discovery.identify_volume`) and refuse to proceed unless it matches the recorded one.
Better still, store roots relative to the volume and open through the GUID path
(`\\?\Volume{…}\Research`), which survives letter changes, as PLAN §4 intends.

**Status: fixed (2026-10-28).** `core/scope/binding.py` (`bind_roots`, `check_root`)
resolves the volume at each root and refuses a root whose `volume_guid` differs from the
recorded one, whose directory is missing, or whose volume can't be identified, naming both
volumes in the message.
`cmd_scan`, `cmd_hash`, `cmd_verify`, the GUI `scan_body` and the dashboard's `request_hash`
call it first; `hash_pass` and `verify_pass` take the bound `root_ids` so no file under a
refused root is read. Other roots in the same pass still run, and the pass exits 1 (CLI) or
raises `RootRefusedError` (GUI) naming what was skipped. Roots are still stored as
letter paths; opening through the GUID path remains the better long-term fix. Tests:
`test_scope.py`, `test_cli.py`, `test_identity.py`, `test_gui_shell.py`.

<a id="f-02"></a>
### F-02 · High · Unlisted directories are tombstoned as vanished

**Evidence.** In `scanner.py:153`, a directory whose listing failed is counted and skipped.
`merge_scan` then calls `_mark_vanished` with `vanish_scope="root"` (`catalog.py:479`), which
marks every file and directory under the root that this scan didn't see. Files under the
unlistable directory weren't seen, so they are recorded as vanished. The CLI then prints
"N entries no longer present (recorded, not removed)".

**Failure scenario.** A permissions change, a transient I/O error or an unplugged drive makes
a subtree unreadable for one scan. Its files drop out of `copies()`, out of the duplicate
groups and out of the browser. The next successful scan revives them, but anything decided in
between was based on false data.

**Reproduction.** R1 (whole root) and R2 (one subdirectory, 3 of 3 files tombstoned).

**Fix.** Record the directories a full scan actually listed (`stage_dir`, as the delta path
already does) and tombstone only inside them. Never tombstone anything beneath a directory
whose listing failed. Refuse the merge outright if the root itself couldn't be listed.

**Status: fixed (2026-10-28).** `scan_root` stages each directory whose listing succeeded
(`Catalog.stage_dirs`) and merges with the new `vanish_scope="listed-dirs"`: files missing
from a listed directory are tombstoned, and so is a child directory missing from a listing,
with its whole subtree. Nothing under an unlistable directory is touched. If the root can't
be listed, `scan_root` merges nothing, records the scan as `failed`, and returns
`root_listed=False` with a coverage line saying so; the CLI exits 1 and the GUI reports it.
Tests: `test_scanner.py`, `test_cli.py`.

<a id="f-03"></a>
### F-03 · High · The catalog can be written inside a scanned tree

**Evidence.** `DEFAULT_CATALOG` is `~/.drivefusion/catalog.db`, but it can be overridden by
`--catalog` or `DRIVEFUSION_CATALOG` (`cli/main.py:34`). Neither `validate_new_root` nor
`Catalog()` checks whether the catalog path lies inside a scope root, or a scope root on the
catalog's volume.

**Failure scenario.** A user follows the plan's "portable mode" idea and keeps the catalog on
an external drive that is itself in scope. Every scan then creates or changes
`catalog.db`, `catalog.db-wal` and `catalog.db-shm` inside the observed tree, and catalogues
them. This breaks UN-1 and makes that volume's catalog self-referential.

**Reproduction.** R3: the tree is `['data.bin']` before the scan and
`['catalog.db', 'catalog.db-shm', 'catalog.db-wal', 'data.bin']` after it.

**Fix.** Refuse to open a catalog inside any enabled scope root. Refuse `scope add` for a root
that contains the catalog or the export root. Exclude the catalog's directory from every
scan as a final defence.

**Status: fixed for scope roots (2026-10-28).** `Catalog()` refuses (`CatalogLocationError`)
to open a catalog inside any enabled scope root. It checks first with a lock-free
`immutable=1` read, so a refused catalog gets no `-wal`/`-shm` files, then again after
connecting. `Catalog.add_scope_root` refuses a root that contains the catalog, and
`scope add` and the GUI check before they record a volume. Both sides are resolved with
`realpath`, so a link can't hide the nesting. The export root got the same check in ROADMAP M8.1
(`Catalog.check_export_root`). Not done: the scan-time exclusion of the catalog's directory. Tests: `test_store.py`,
`test_cli.py`, `test_gui_shell.py`. R3 now reports the refused `add_scope_root` instead of
scanning.

**Extended to the whole drive (2026-10-28, ROADMAP D8).** The catalog may not be on a volume
that holds any enabled root, except the system drive, where it may sit anywhere outside the
roots. `Catalog()` compares its own volume (`discovery.volume_guid_of`) with each enabled
root's recorded `volume_guid` before connecting and again after. `add_scope_root`, `scope add`
and the GUI refuse a root on the catalog's volume, and the CLI and GUI do so before recording
the volume. If the system drive can't be identified, no exception is made. A catalog whose own
volume can't be identified is allowed: every root is on an identified local volume, so the
catalog can't be on one of them. Tests: `test_store.py` (7 cases on two simulated drives),
`test_cli.py`, `test_gui_shell.py`.

<a id="f-04"></a>
### F-04 · High · Single-copy files of unique size are missing from "under-protected"

**Evidence.** `hash_candidates` deliberately skips files whose size occurs only once. That is
correct, because they can't be duplicates. But `_group_rows` and `under_protected`
(`analysis/duplicates.py:50`, `:93`) inner-join `content`, so a file with no `content_id` is
never considered. A file whose size is unique in the catalog is, by construction, held exactly
once, so it is certainly under-protected for any `min_copies ≥ 2`.

**Failure scenario.** A user runs `drivefusion report` and sees "under-protected: 2 items".
The largest single-copy video, the only file of its size, isn't among them. The headline
`unhashed_files` note says these files "are never opened". It doesn't say they are single
copies.

**Reproduction.** R4: under-protected = 2 items; the unique 999-byte file is not listed.

**Fix.** Treat files of unique size as one copy each, which needs no hashing, and include
them in `under_protected` and `redundancy_summary`. Keep "unhashed" for files whose size *is*
shared but which haven't been hashed yet, because only those are genuinely unknown.

**Status: fixed (2026-10-28).** `single_copy_files` returns each live, non-empty file whose
size no other live file shares (in any read state), as a `ContentGroup` with
`content_id=None`, one path, one drive and one physical copy. `under_protected` includes
these whenever `min_copies > 1`. `redundancy_summary` adds them to its totals, reports
`single_copy_files`, and limits `unhashed_files` to files whose size is shared. They are
never opened. `group_paths` lists their path for the CLI report, and the CLI note and GUI
coverage line say they are counted as single copies. Tests: `test_analysis.py`,
`test_cli.py`.

<a id="f-05"></a>
### F-05 · Medium · A volume with no readable serial counts as its own drive

**Evidence.** `DRIVE_KEY = COALESCE(v.drive_id, -v.id)` (`duplicates.py:27`). `drive_id` is
set only when `IOCTL_STORAGE_QUERY_PROPERTY` returns a serial (`cli/main.py:190`), which the
plan itself notes is often missing over USB bridges.

**Failure scenario.** One USB disk with no reported serial carries two partitions. Content on
both partitions is reported as "2 copies on distinct drives" when it is on one device.
Separately, some bridges report the *bridge's* serial, which would merge different disks into
one. That direction is safe but still wrong.

**Reproduction.** R6: two partitions, no serial → 0 under-protected groups.

**Fix.** Group serial-less volumes by `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` disk number,
together with disk signature or GUID (`IOCTL_DISK_GET_DRIVE_LAYOUT_EX`), at discovery time.
Report "drive identity unverified" next to any copy count that depends on such a volume, and
leave those volumes out of the "distinct drives" total until a user confirms them.

**Status: fixed (2026-10-28, ROADMAP A1).** Three parts:

1. **Identify more drives.** Discovery reads the disk's partition table
   (`IOCTL_DISK_GET_DRIVE_LAYOUT_EX`, allowed on a zero-access handle) and records its MBR
   signature or GPT disk GUID as `drive.layout_id` (schema v5, with `drive.identity` =
   `serial`, `disk-layout` or `user`). A serial-less disk is then one drive across its
   partitions and reconnections, and gains its serial if a dock later reports one.
2. **Count safely what still isn't identified.** A volume with no drive adds no *proven*
   copy (`DRIVES_PROVEN`), so the under-protected list errs towards listing; for reclamation
   it counts as its own drive (`DRIVES_POSSIBLE`), so a possibly separate copy is never called
   waste. Where the two differ the group is `identity_uncertain`, the CLI report says "up to
   N", the summary counts `unverified_drive_items`, and the browser shows "1?".
3. **Let the user settle it.** `drivefusion drives list | confirm VOLUME | assign VOLUME
   DRIVE` and two buttons on the Drives screen. A drive identified by serial or partition
   table can't be overruled by hand. (Recording these decisions in the audit log waits for
   ROADMAP A4.)

The bridge-serial case in the failure scenario (different disks behind one bridge serial)
is not addressed; it errs safe. Tests: `test_discovery_layout.py`, `test_store.py`,
`test_analysis.py`, `test_cli.py`, `test_gui_shell.py`, `test_gui_models.py`; V&V R-RED-06,
which fails against the previous code. R6 now prints `NOT FOUND`.

<a id="f-06"></a>
### F-06 · Medium · `verify` reports legitimate edits as fixity mismatches

**Evidence.** The docstring and schema define silent corruption as "hash changed while its
modification time did not". `verify_pass` (`pipeline.py:197`) never stats the file or compares
size or mtime with the catalog. Any change since the last scan is recorded as `mismatch`,
shown under "fixity failures", and makes `verify` exit 1.

**Failure scenario.** A user edits a document, then runs `verify` before rescanning. The
report calls it a fixity failure. False alarms of this kind train people to ignore the real
ones.

**Reproduction.** R5: the file is edited (mtime changes) → `mismatched: 1`.

**Fix.** Before hashing, `fsio.stat` the file. If size or mtime differs from the catalog row,
record `modified` (or `stale`) rather than hashing. Report `mismatch` only when the metadata
is unchanged.

**Status: fixed (2026-10-28, ROADMAP A3).** `verify_pass` stats each file first. A changed
mtime means an edit: recorded as `modified`, not hashed, left out of `integrity_incidents`,
and reported by `verify` as "edited since the last scan" without failing it. The fix
compares mtime only, not size as suggested above: software that writes a file updates its
mtime, while truncation by filesystem damage keeps it, so a size change with the date kept
is still hashed and reported as a mismatch (`test_verify_flags_a_small_file_that_changed_length`).
Tests: `test_identity.py`; V&V R-FIX-04, which fails against the previous code. R5 now
prints `NOT FOUND`.

<a id="f-07"></a>
### F-07 · Medium · The elevation launcher doesn't authenticate what it elevates

**Evidence.** `gui/elevation.py`:
* `helper_path()` (`:72`) takes the directory of `sys.executable` or of `sys.argv[0]`.
* `request()` (`:130`) refuses only when the resolved name isn't `dfscan-helper.exe` or the
  file doesn't exist. The module docstring says the path is "verified", but there is no
  signature or hash check.
* `parameters = " ".join(arguments)` (`:149`). `ShellExecuteW` takes a single command-line
  string, so an argument containing a space or a quote would be split or reinterpreted.
  (The docstring says this can't happen.)
* The helper is launched with `SW_HIDE` and there is no pipe back, so its framed stdout would
  reach no one.

**Failure scenario (once wired).** In portable mode, or in any per-user install, the
application directory is writable by the user. Any unprivileged process can replace
`dfscan-helper.exe`. The next time the user accepts a UAC prompt for a "fast scan", the
replacement runs as Administrator. The binaries are currently unsigned
(`codesign_identity=None`), so the prompt shows "Unknown publisher" either way and gives the
user no signal.

**Status today.** Latent. `request()` has no caller (see [F-09](#f-09)).

**Fix (before wiring).**
1. Install to a directory that only administrators can write, or verify the helper's
   Authenticode signature (`WinVerifyTrust`) or a hash pinned at build time immediately before
   the `runas` launch.
2. Build the parameter string with `subprocess.list2cmdline`.
3. Carry output over a named pipe whose ACL admits only the launching user's SID.
4. Sign all three executables.

**Status: fixed (2026-10-29, ROADMAP B1).** Fixes 1 and 2 are done; 3 (the pipe) is ROADMAP
B2 and 4 (signing) needs a certificate. `packaging/drivefusion.spec` builds the helper first,
hashes it, and packs the SHA-256 into a module inside the main executables' archive, so
swapping the helper can't swap the pin. `elevation.request` opens the helper with
`FILE_SHARE_READ` only, hashes it through that handle, compares with the pin, and launches
while still holding the handle, so the file can't be changed or renamed over between the
check and `ShellExecuteW`. A build with no pin (a source checkout) refuses to elevate a
helper in a folder the user can write to. Parameters go through `subprocess.list2cmdline`.
The module docstring's "no command line" claim is corrected. A CI step fails the build
if the pin doesn't match the shipped `dfscan-helper.exe`. Tests: `test_elevation.py`.

<a id="f-08"></a>
### F-08 · Medium · `ro_lint` blind spots

**Evidence.** `tools/ro_lint.py` bans named filesystem mutators on `os`, `shutil` and
`pathlib`, write-intent `os.O_*` names, non-`"rb"` modes on builtin `open` and `Path.open`, and
raw-handle APIs outside an allowlist. Each probe below passes the lint with **zero findings**
(R8):

| Probe | Why it matters |
|---|---|
| `subprocess.run([...])`, `os.system`, `os.startfile` | Runs any program, including `del`, `format` or `diskpart` |
| `os.open(path, 0x241)` (numeric flags) | `O_WRONLY \| O_CREAT \| O_TRUNC` by value |
| `io.open(path, "wb")`, `io.FileIO(path, "w")` | Same as `open()`, different name |
| `os.write(fd, …)` | Writes through any descriptor |
| `ctypes…kernel32.DeleteFileW / MoveFileExW / SetFileTime / SetFileAttributesW / WriteFile` | DF007 restricts only `CreateFileW/A` and `DeviceIoControl` |
| `sqlite3.connect(path_on_media)` | Creates a database file and its WAL wherever it is pointed |
| `getattr(os, "re" + "move")(…)` | Name indirection |

The lint's docstring is honest that AST analysis "cannot resolve arbitrary receivers". It
argues that the other layers cover the gap. For these probes they don't. The gateway is a
convention, not a sandbox. OS-level read-only enforcement applies only to handles the gateway
opened. The no-touch test only exercises code paths it calls. So the PLAN's statement that a
regression "must defeat four independent mechanisms" overstated the case. The mechanisms are
complementary, and for these classes the lint is the only one.

**Fix.** Add `subprocess`, `os.system`, `os.startfile`, `os.popen`, `os.write`, `io.open`,
`io.FileIO`, `os.open` with a non-literal or write-capable flag expression, `sqlite3.connect`
outside `core/store`, and a deny-list of Win32 mutator names to the rules. Flag `getattr` on
filesystem modules with a non-literal name. Optionally add a runtime audit hook
(`sys.addaudithook`) in tests that fails on `open` events with write modes, and on
`os.remove`, `os.rename` and `subprocess.Popen`, outside the catalog directory.

**Status: fixed (2026-10-28).** `tools/ro_lint.py` gains rules DF008–DF013:
process launches outside `PROCESS_LAUNCH_MODULES`, an allowlist whose entries each state why
they may start a process (today `helper/client.py` and `gui/elevation.py`); descriptor
writes; `io.open`/`io.FileIO`/`os.fdopen`/`codecs.open` held to `"rb"`; `os.open` flags
that can't be proven read-only; `sqlite3.connect` outside `core/store`; Win32 mutators by
name and as string literals; and `getattr`/`__import__` indirection. Every R8 probe is now
caught. The no-touch cycle also runs under a `sys.addaudithook` guard
(`tests/fixtures/audit_guard.py`) that refuses and records any write, rename, remove, metadata
change or `sqlite3.connect` under the fixture tree, and any process launch, on every thread.
Tests: `test_ro_lint.py` (each rule both ways), `test_no_touch.py` (the guard stops each kind
of change before it happens, and records one that is swallowed). Residual: a computed name on
an arbitrary receiver (`getattr(dll, name)`), and `ctypes` calls, which raise no audit event.

<a id="f-09"></a>
### F-09 · Medium · Documentation described unwired or unimplemented features as done

Every row below was verified against the code at `ca6c4e0` and **has been corrected in this
change**.

| Claim (where) | Fact |
|---|---|
| "UAC prompt" and "UAC elevation offered per scan" as M4 deliverables (README, PLAN §6.5, §15) | `gui/elevation.request` exists and is unit-tested; nothing calls it. The GUI shows only a status-bar line. |
| "rescan incrementally" from CLI or desktop (README); M2 "incremental rescan" | `core/enum/delta.apply_delta` is implemented and tested, but neither `cmd_scan` nor the GUI calls it. Every user-initiated scan is a full walk. |
| Elevation consent and first-run disclaimer "recorded in the audit log" (PLAN §1, §6.5) | The `audit` table exists; nothing writes to it, and there is no first-run disclaimer. |
| Helper "covered by the same AST lint **and no-touch test**" (PLAN §6.5) | Lint: yes. No-touch: no, because the helper needs a real NTFS volume handle. |
| No-touch assertion "including access times" (PLAN §3.2, §14) | Asserted only on POSIX where `O_NOATIME` is granted; skipped on Windows. |
| Cancel "takes effect within a directory" (README, PLAN §11) | Checked every 10,000 staged files ([F-13](#f-13)). |
| Catalog in `platformdirs.user_data_dir`, with a portable mode (PLAN §12) | `~/.drivefusion/catalog.db`, overridable; no portable mode, and `platformdirs` isn't a dependency. |
| Default exclusions include `C:\Program Files*`, `AppData\Local\Temp` and package caches (PLAN §4) | Code excludes `C:\Program Files`, `C:\Program Files (x86)`, `C:\ProgramData\Package Cache` and named patterns including `*.tmp` and `~$*`; there is no `AppData\Local\Temp` rule. |
| Silent corruption = "hash changed while mtime did not" (PLAN §8.1) | `verify` doesn't compare mtime ([F-06](#f-06)). |
| "A regression must defeat four independent mechanisms" (PLAN §16) | Complementary, not independent ([F-08](#f-08)). |
| Elevation path "derived and then verified"; "no command line for a quoting mistake to escape from" (`gui/elevation.py` docstring) | Name check only; parameters are a joined string ([F-07](#f-07)). *Code comment, not changed here.* |

<a id="f-10"></a>
### F-10 · Medium · Pre-migration backup copies only the main file of a WAL database

**Evidence.** `Catalog.__init__` connects (setting `journal_mode=WAL`) and then calls
`migrate`, which runs `backup_catalog`, which runs `shutil.copy2(path, target)`
(`schema.py:322–330`). Committed transactions that are still in `catalog.db-wal` (for
example after a crash, or while another process holds the catalog open) aren't in the main
file, so the backup is stale. It may not even contain the schema.

**Reproduction.** R10: three committed rows still in the WAL → the copied backup reports "no such
table".

**Fix.** Use `sqlite3.Connection.backup()` or `VACUUM INTO 'target'`. Both produce a
transactionally consistent snapshot that includes WAL content.

**Status: fixed (2026-10-28).** `backup_catalog(conn, path)` uses SQLite's online backup
API on the open connection, writing to `<name>.partial` and renaming when complete. It still
never replaces an existing backup. Tests: `test_store.py`
(`test_backup_includes_transactions_still_in_the_wal`,
`test_backup_never_replaces_an_existing_backup`). R10 now exercises `backup_catalog` itself.

<a id="f-11"></a>
### F-11 · Medium · Placeholders with a visible reparse attribute are dropped, not catalogued

**Evidence.** `scanner.py:169` tests `is_reparse_point` *before* `is_dehydrated`
(`scanner.py:191`). Cloud Files API placeholders are reparse points. Whether
`FILE_ATTRIBUTE_REPARSE_POINT` is visible to this process depends on the process's
placeholder-compatibility mode. Where it is visible, the placeholder is counted as "links not
traversed" and never recorded. The existing test injects only `RECALL_ON_DATA_ACCESS`, so it
doesn't cover this combination.

**Reproduction.** R11: `REPARSE_POINT | RECALL_ON_DATA_ACCESS` → "0 of 0 files read; 1 links not
traversed"; 0 files catalogued.

**Impact.** Safe, because the file is never opened, but contrary to SR-06. Cloud-only content
disappears from the catalog, and so does the only record that it exists locally only as a
placeholder.

**Fix.** For non-directories, test dehydration first. Distinguish cloud reparse tags from
symlinks and junctions (the reparse tag is available from `FileAttributeTagInfo` or
`FILE_ID_BOTH_DIR_INFO.EaSize`). Add a test with both attributes set, and verify the behaviour
on a real OneDrive folder.

**Status: fixed (2026-10-28, ROADMAP A3).** Classified by reparse *tag*, not attribute:
only a name-surrogate tag (symlink, junction, mount point) is a link (`fsio.skip_as_link`,
shared by the scanner, the delta path and the preview). The tag comes from `EaSize` in
`FILE_FULL_DIR_INFO` / `FILE_ID_BOTH_DIR_INFO`, or `st_reparse_tag`. A placeholder with the
reparse attribute is catalogued as `skipped-dehydrated` and never opened. Going further than
this finding: downloaded cloud files, deduplicated files and WOF-compressed files were also
dropped as "links", and are now catalogued as the real files they are. Directories that are
reparse points of any kind are still not traversed. Not done: checking the behaviour on a
real OneDrive folder. Tests: `test_fsio.py`, `test_enum_records.py`, `test_scanner.py`.

<a id="f-12"></a>
### F-12 · Low · "Last scanned" is the time the volume was added

**Evidence.** `scope list`, `status` and the GUI Scope and Drives screens all show
`describe_age(volume.last_seen_at)`. `last_seen_at` is written only by `upsert_volume`, which
runs at `scope add`. A scan never updates it.

**Reproduction.** R12: `last_seen_at` is unchanged by a scan. Through the CLI, `scope list`
run straight after a scan printed "last scanned: 9 months ago".

**Fix.** Show the latest `scan.finished_at` with `status='done'` for each root, or update a
`last_scanned_at` column in `finish_scan`.

**Status: fixed (2026-10-28, ROADMAP A3).** "Last scanned" now means the last scan that
finished, taken from the `scan` table per volume (`Catalog.volumes()`) and per root
(`root_last_scanned()`), with no schema change. A failed or cancelled scan doesn't count.
`last_seen_at` keeps its own meaning. Tests: `test_scanner.py`, `test_cli.py`; V&V R-CAT-08.

<a id="f-13"></a>
### F-13 · Low · Cancel granularity is 10,000 files, not one directory

**Evidence.** The GUI checks cancellation inside the scanner's `progress` callback. The scanner
calls `progress` only after staging each batch of `BATCH_ROWS` (10,000) files
(`scanner.py:214`). A root with fewer files, or a deep tree of mostly empty directories, can't
be cancelled until it finishes. The full-hash pass checks once per duplicate group, so one
group of large files can't be interrupted either.

**Reproduction.** R7: a 201-directory scan made 0 progress callbacks.

**Fix.** Pass a cancellation callable into `scan_root` and `hash_pass`, and check it per
directory result and per file.

**Status: fixed (2026-10-28, ROADMAP A3).** `scan_root`, `hash_pass` and `verify_pass` take a
`cancel` callback, called per directory and per file; the GUI passes `Cancellation.raised`.
Its `Cancelled` is now a core `OperationCancelled`, so a cancelled scan is rolled back and
recorded as `aborted`, not `failed`. One very large file still reads to the end before the
next check. Tests: `test_scanner.py`, `test_identity.py`, `test_gui_shell.py`; R7 counts
201 checks for a 201-directory scan.

<a id="f-14"></a>
### F-14 · Low · Missing `restype` defeats the `INVALID_HANDLE_VALUE` check

**Evidence.** `winio.py:55`, `:82` and `discovery/windows.py:104`, `:345` call
`kernel32.CreateFileW` or `FindFirstVolumeW` from a `ctypes.WinDLL` without setting `restype`,
so the return value is a C `int`. Failure returns `-1`, but the code compares against
`ctypes.c_void_p(-1).value`, which is 18446744073709551615 on 64-bit, so failures are never
detected. The `-1` "handle" then goes to `DeviceIoControl` or `GetFileInformationByHandleEx`,
which fail with `ERROR_INVALID_HANDLE`. (`-1` is the current-process pseudo-handle; the
volume IOCTLs reject it, so nothing harmful happens.) `fsio.py` sets `restype` correctly.

**Impact.** Access-denied failures surface as WinError 6, not 5, so the helper's
"administrator rights required" message and the `access` status code are never produced.
Valid handles are also truncated to 32 bits. That is harmless in practice, but it is
undefined behaviour.

**Reproduction.** R9 (the comparison itself).

**Fix.** Declare `argtypes` and `restype = wintypes.HANDLE` for every Win32 call, as
`fsio._open_fd_windows` already does.

**Status: fixed (2026-10-28).** `winio.kernel32()` is one cached `WinDLL` with `argtypes`
and `restype` declared for `CreateFileW`, `CloseHandle`, `DeviceIoControl` and
`GetFileInformationByHandleEx`; `dirinfo.py` uses it rather than its own undeclared instance.
`discovery/windows.py` declares the same, plus `FindFirstVolumeW`, `FindNextVolumeW` and
`FindVolumeClose`. Tests: `test_win32_signatures.py`, which runs on any platform against a
stand-in `WinDLL`. R9 now checks the declaration instead of the general `ctypes` fact.

<a id="f-15"></a>
### F-15 · Low · Coverage line counts directories as files

`ScanCounters.coverage_line` (`scanner.py:53`) adds unreadable *directories* to the file
total, which yields "0 of 1 files read; 1 unreadable" for a missing root (R1). Report
unreadable directories and unreadable files separately, and say that the unreadable
directories' contents are unknown.

**Status: fixed (2026-10-28, ROADMAP A3).** `ScanCounters` keeps unlistable folders
(`unreadable_dirs`) apart from unreadable files: "N of M files read; K folder(s) could not
be listed, so what they hold is unknown". A file whose metadata failed was also counted
twice in the old total; it is now counted once. The preview reports "unlistable folders".

<a id="f-16"></a>
### F-16 · Low · `open_read` checks attributes without following links, then opens following them

`open_read` (`fsio.py:362`) checks dehydration with `stat(path, follow_symlinks=False)`, then
opens the path with `CreateFileW` without `FILE_FLAG_OPEN_REPARSE_POINT` (or `os.open` without
`O_NOFOLLOW`), which follows links. The scanner never catalogues links, so this is reachable
only if a path is replaced by a link between the scan and the hash, a time-of-check to
time-of-use race. If it happens, the read can leave the scope or hydrate a placeholder the
link points to. Fix: after opening, check the attributes of the *handle*
(`GetFileInformationByHandle`, or `fstat`), and refuse if they show a reparse point or a
recall attribute.


**Status: fixed (2026-10-28).** `open_read` refuses a path whose own `lstat` says it is a
link, opens with `O_NOFOLLOW` on POSIX, then `fstat`s the opened descriptor and closes it
unread if it is a directory, shows recall attributes, or is not the same `(device, inode)`
as the file checked before opening. On Windows `os.fstat` reads the handle's attributes,
volume serial and file index, so the identity check covers a path swapped for a link there
too. Reparse points as such are *not* refused, because hydrated cloud files and
deduplicated files carry one (F-11). Tests: `test_fsio.py` (a link; a file swapped for a
link after the check; a handle with a different identity, closed; a handle showing recall
attributes). Reproduction: R13.
---

## 4. Strengths worth keeping

* **Defence in depth at the handle level.** Every `CreateFileW` uses `GENERIC_READ` (or zero
  access) together with `OPEN_EXISTING`, and DF006 rejects write-access names inside
  raw-handle modules.
* **Self-checking tests.** The no-touch snapshot refuses to be empty and checks landmark paths;
  CI asserts that Qt starts, so the GUI tests can't skip silently; the widget-tree test bans
  destructive verbs.
* **Careful SQL.** NULL-in-UNIQUE traps are handled explicitly for root directories and for
  quick-hash-only content. The merge is one set-based transaction, and the journal cursor
  advances only after a merge.
* **Honest delta design.** The journal only says *which* directories to re-read; the listing
  stays the source of truth, and tombstoning is scoped to the directories examined. The
  full-scan path should adopt the same rule ([F-02](#f-02)).
* **Unknown kept as unknown in the GUI.** "—", not "1", for unhashed copy counts.
* **A small, read-only elevated helper** that forwards raw buffers so there is one parser, a
  framed protocol that rejects truncation, and structured error status on stderr.

---

## 5. Recommended order of work

```mermaid
flowchart TD
    A["F-01 bind roots to volume GUID"] --> B["F-02 tombstone only listed dirs"]
    B --> C["F-04 count unique-size files as 1 copy"]
    C --> D["F-05 group serial-less volumes by disk"]
    A --> E["F-03 refuse catalog inside scope"]
    E --> F["F-08 close lint blind spots<br/>+ test-time audit hook"]
    D --> G["F-06 verify: stat before hash"]
    G --> H["F-10 consistent backup"]
    H --> I["F-11 placeholder ordering"]
    F --> J["F-07 harden elevation<br/>before wiring SR-16/SR-17"]
    I --> K["Low: F-12 to F-16"]
```

*Figure 3 — Suggested order. Fix the high-severity file-fact and safety findings first,
before M5 builds durability scoring on top of copy counts. Harden elevation before wiring the
UAC prompt and the delta rescan.*

---

## 6. Reproduction

```bash
pip install -e ".[dev]"
python tools/review_repro.py     # prints R1–R12 with FOUND / NOT FOUND
```

The script runs in temporary directories only. As each defect is fixed, its line should flip
to `NOT FOUND`; at that point the corresponding check belongs in the regular test suite.
R1–R4 print `NOT FOUND` as of 2026-10-28, and their checks are in `tests/`. R3 now expects
`add_scope_root` to refuse the root and reports that refusal. After Track A items A2 and A1, R6,
R8 (every probe), R9, R10 and R13 also print `NOT FOUND` or `caught`. After A3, so do R5,
R7, R11 and R12: every reproduction is now `NOT FOUND`.

<!-- abbreviations: generated by tools/doc_abbreviations.py; edit the definitions there -->

## Abbreviations Used in this Document

| Abbreviation | Meaning |
|---|---|
| ACL | Access control list: who may open a file or object, and how |
| API | Application programming interface |
| AST | Abstract syntax tree: Python source parsed into its structure, as ro_lint reads it |
| CI | Continuous integration: the automated checks and builds run on GitHub for every push |
| CLI | Command-line interface: the drivefusion command |
| FAIR | Findable, Accessible, Interoperable, Reusable: the FAIR Guiding Principles for research data |
| GPT | GUID Partition Table: the modern disk partition layout |
| GUI | Graphical user interface: the Drive Fusion window |
| GUID | Globally unique identifier, as Windows gives each volume |
| ID | Identifier |
| IOCTL | Input/output control: a request sent directly to a device or filesystem driver |
| MBR | Master Boot Record: the older disk partition layout |
| NDSA | National Digital Stewardship Alliance, which publishes the Levels of Digital Preservation |
| NTFS | New Technology File System: Windows' main filesystem |
| OS | Operating system |
| POSIX | Portable Operating System Interface: the Unix system-call standard |
| SHA-256 | Secure Hash Algorithm 2 with a 256-bit digest: the content hash Drive Fusion uses |
| SID | Security identifier: Windows' ID for a user or group |
| SQL | Structured Query Language |
| UAC | User Account Control: Windows' prompt before a program runs as administrator |
| UI | User interface |
| USB | Universal Serial Bus |
| USN | Update Sequence Number: the NTFS change journal, which records every change on a volume |
| V&V | Verification and validation: checking the tool was built right and does the right thing |
| VV | V&V (verification and validation), as in the vv bench and VV- test IDs |
| WAL | Write-ahead log: SQLite's journal mode that keeps recent changes in a -wal file |
| WOF | Windows Overlay Filter: Windows' transparent file compression |

| Identifier | Meaning |
|---|---|
| R-…-nn | A testable requirement (docs/VV.md §3). Families: CAT catalog, COL collections, EXP exports, FAIR FAIR and NDSA scorecards, FIX fixity, HL drive health, ID content identity, INC incremental rescans, PLAN planner, RED redundancy, RO read-only, SC scope, VER versions |
| SR-nn | A system requirement in docs/REVIEW.md §2.2 |
| UN-n | A user need in docs/REVIEW.md §2.1 |
| DFnnn | A rule of the read-only lint, tools/ro_lint.py |
| Mn, Mn.n | A milestone of docs/ROADMAP.md (M8), or a task within one (M8.8) |
| Dn | A decision recorded in the decisions table of docs/ROADMAP.md |
| F-nn | A finding of the safety and integrity review, docs/REVIEW.md §3 |
| An, Bn, Cn | A work item on Track A, B or C of docs/ROADMAP.md |
| Rn | A reproduction probe of docs/REVIEW.md §6 |

<!-- /abbreviations -->
