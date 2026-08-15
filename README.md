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

**Designed for:** ~20 drives, 10–50 million files, local drives only, user-defined scan scope.
Scale drives the design — NTFS MFT/USN bulk enumeration for fast scans and near-instant
incremental rescans, interned paths, and fully virtualized views.

**Not for identifiable human-subjects data, PHI, or otherwise regulated data.** See the
disclaimer in the plan.

**Status:** design phase. See [docs/PLAN.md](docs/PLAN.md) for the full build plan —
read-only guarantees, scan scope, Windows enumeration strategy, data model and performance
budgets at 50M files, durability scoring, FAIR/NDSA scorecards, the placement planner, and
milestones M0–M8.
