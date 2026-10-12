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

**M6 complete — the archive can now describe itself.** On top of cataloging, redundancy and
risk, Drive Fusion proposes collections out of the directory tree, lets you confirm or decline
each one, and scores the confirmed ones against FAIR and the NDSA preservation levels.

| Milestone | State |
|---|---|
| **M0** Skeleton and guardrails | **done** — gateway, lint, no-touch test, volume fixtures, build |
| **M1** Catalog core | **done** — scope, discovery, walker, interned schema, merge, CLI |
| **M2** Fast enumeration | **done** — parsers, journal logic, parallel walker, `dfscan-helper`, incremental rescan |
| **M3** Identity & analysis | **done** — tiered hashing, duplicate groups, copies-per-drive, fixity |
| **M4** GUI shell | **done** — Scope, Dashboard, Drives, Catalog; paged views, cancellable scans, UAC prompt |
| **M5** Health & risk | **done** — smartctl parsing, versioned AFR table, durability model, expected bytes lost/yr |
| **M6** Curation & FAIR | **done** — collection inference and review queue, membership rules, FAIR + NDSA scorecards |
| M7–M8 | see [docs/PLAN.md](docs/PLAN.md) §15 |

```
drivefusion scope add D:\Research
drivefusion scope preview 1        # counts what a scan would cover, writes nothing
drivefusion scan
drivefusion find "*.psd"
drivefusion hash                   # content identity; the only verb that reads file bodies
drivefusion report                 # duplication, durability, and fixity findings
drivefusion verify                 # re-hash and report silent corruption
drivefusion status
drivefusion health collect         # read SMART; falls back to age estimates
drivefusion health show            # per-drive failure risk, with its evidence
drivefusion health set 1 --purchase-date 2021-03-04 --disaster-zone home-office
drivefusion report risk            # expected bytes lost per year
drivefusion collections propose    # proposes groupings; confirms nothing
drivefusion collections accept 1   # confirmation is always an explicit act
drivefusion score                  # FAIR and NDSA scorecards, with remedies
drivefusion gui                    # the desktop window (needs the [gui] extra)
```

**Risk figures always carry their evidence.** A drive's failure probability is labelled
`measured` (from its own SMART counters), `estimated` (age and media class only — common over
USB bridges, which pass no SMART through) or `unknown`. A drive that reported nothing scores
*worse* than one verified clean, because silence and a clean bill of health are not the same
state. Failure rates live in [`standards/afr/`](standards/README.md) as a versioned file that
declares itself generic rather than borrowing a citation it has not earned, and every report
names the table version it used.

The under-protected queue is ranked by **size × P(loss)**. Ranked by size alone the top of the
list is whatever is biggest, usually video on a healthy drive; ranked by expected loss it is
whatever sits on the drive with pending sectors. Only one of those is a work queue.

**Inference proposes; you decide.** There is no directory convention to import, so collections
are scored out of the tree — size, file-type homogeneity, how tightly the modification dates
cluster, whether sibling folders follow a dated or numbered pattern — and every proposal shows
that reasoning, with sample paths and counts, before you accept, rename, merge or decline it.
A decline is remembered. Confirmed membership is a *rule* over the subtree, so files that land
there later join without you revisiting the decision.

**The scorecards are built not to flatter.** Criteria the tool cannot assess are excluded from
the denominator and listed, so a high percentage over a few criteria cannot read as a high
score. A missing licence caps the grade at `incomplete` however well everything else scores,
because unlicensed data is not reusable by anyone else — and "not for sharing" is accepted as
the clear answer that a blank is not. NDSA rows about written policy and staff training are
reported as *not assessable from a catalog* rather than quietly passed. The unfiled bucket is
scored separately, because a grade for the curated 12% of an archive is not a grade for the
archive.

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

M5 added a second clause, because reading drive health meant running an external program for
the first time: **starting a process is confined to modules that allowlist their own
arguments** (lint code DF008). `smartctl -t long` begins a multi-hour self-test on a user's
drive. That is not a filesystem write, so the gateway, the mutating-call rules and the
no-touch test would all have missed it.

## Documentation

[docs/PLAN.md](docs/PLAN.md) — the full build plan: read-only guarantees, scan scope, Windows
enumeration strategy, data model and performance budgets at 50M files, durability scoring,
FAIR/NDSA scorecards, the placement planner, and milestones M0–M8.
