# drive-fusion

A Windows desktop tool for cataloging the drives you point it at — internal, external,
connected or shelved — confirming how many real copies of each file exist, and building
reviewable plans to consolidate that content into a traceable, FAIR-aligned order.

> **Drive Fusion never modifies, moves, or deletes your data.** It is a planning and
> reporting instrument. It reads the drives you add to its scan scope and writes only to its
> own catalog and to an export folder you choose. This is enforced structurally — a read-only
> I/O gateway, read-only volume handles, a CI lint that bans mutating calls, and a regression
> test asserting scanned trees are byte-identical before and after a full run — not by policy
> alone.

Python 3.11+ · PySide6 GUI · SQLite catalog · packaged as a signed 64-bit Windows executable.

**Designed for:** ~20 drives (~50/50 NTFS and exFAT), 10–50 million files, local drives only,
user-defined scan scope. Scale drives the design — NTFS MFT/USN bulk enumeration for fast scans
and near-instant incremental rescans, a parallel batch walker for exFAT volumes that have
neither, layout-ordered hashing, interned paths, and fully virtualized views.

**Not for identifiable human-subjects data, PHI, or otherwise regulated data.** See the
disclaimer in the plan.

## Status

**M4 complete — there is a window now.** The tool answers use cases 1 and 2 from either the
command line or a desktop app: register scan scope, enumerate it, rescan incrementally, search
it, establish content identity, browse the catalog, and get redundancy, duplication and fixity
reports.

| Milestone | State |
|---|---|
| **M0** Skeleton and guardrails | **done** — gateway, lint, no-touch test, volume fixtures, build |
| **M1** Catalog core | **done** — scope, discovery, walker, interned schema, merge, CLI |
| **M2** Fast enumeration | **done** — parsers, journal logic, parallel walker, `dfscan-helper`, incremental rescan |
| **M3** Identity & analysis | **done** — tiered hashing, duplicate groups, copies-per-drive, fixity |
| **M4** GUI shell | **done** — Scope, Dashboard, Drives, Catalog; paged views, cancellable scans, UAC prompt |
| M5–M8 | see [docs/PLAN.md](docs/PLAN.md) §15 |

```
drivefusion scope add D:\Research
drivefusion scope preview 1        # counts what a scan would cover, writes nothing
drivefusion scan
drivefusion find "*.psd"
drivefusion hash                   # content identity; the only verb that reads file bodies
drivefusion report                 # duplication, durability, and fixity findings
drivefusion verify                 # re-hash and report silent corruption
drivefusion status
drivefusion gui                    # the desktop window (needs the [gui] extra)
```

The window has four screens — Scope, Dashboard, Drives and Catalog. Scans and hash passes run
on a worker thread with a Cancel button that takes effect within a directory rather than at the
end of a root, the catalog browser pages through a directory 200 rows at a time by keyset seek
rather than `OFFSET`, and file counts are computed off-thread and shown as "counting…" until
they land. Screens that belong to later milestones are listed and visibly disabled rather than
present and inert.

There is no delete, move, or apply control anywhere in it, and a test walks the whole widget
tree on every CI run to assert there never is one.

Redundancy is counted **over distinct physical drives, not over paths**. Two copies on one
drive are one copy — if that drive dies they go together — and a hardlink is the same file
seen twice, not a second copy of anything. Both rules exist because getting them wrong does
not raise an error; it produces a confident number that is false.

Measured at 3M files: **222 bytes per file** (≈10.4 GB projected at 50M), 192k rows/s merge,
555 MB peak RSS — all inside the [§13 budgets](docs/PLAN.md). Run `python tools/benchmark.py`
to reproduce.

## Development

```bash
pip install -e ".[dev,gui]"

python tools/ro_lint.py        # the read-only guard; exits non-zero on any finding
python -m pytest -q            # full suite
python tools/benchmark.py      # catalog benchmark against the §13 budgets
python -m drivefusion --help

pyinstaller packaging/drivefusion.spec --noconfirm --clean
```

The VHDX volume tests need Windows and administrator rights (they call `diskpart`) and skip
everywhere else. The GUI tests run headless under `QT_QPA_PLATFORM=offscreen` on both
platforms; CI installs Qt's runtime libraries and then asserts Qt can actually start, because a
GUI suite that skips itself would otherwise turn a green run into a statement about nothing.

CI runs the lint and tests on Windows and Linux, then builds and smoke-tests the Windows
executables — a windowed `DriveFusion.exe` and a console `drivefusion.exe` from one build.

### The one rule

Nothing outside `drivefusion/core/store/` and `drivefusion/core/export/` may reference a
mutating filesystem call. `tools/ro_lint.py` enforces it, the test suite asserts it, and CI
gates on it. If you find yourself needing to write to a catalogued volume, the answer is that
the tool does not do that — it emits a plan describing what *you* might do.

## Documentation

[docs/PLAN.md](docs/PLAN.md) — the full build plan: read-only guarantees, scan scope, Windows
enumeration strategy, data model and performance budgets at 50M files, durability scoring,
FAIR/NDSA scorecards, the placement planner, and milestones M0–M8.
