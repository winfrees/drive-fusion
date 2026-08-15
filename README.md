# drive-fusion

A desktop tool for cataloging every drive you own — internal, external, connected or
shelved — confirming how many real copies of each file exist, and building reviewable
plans to consolidate that content into a traceable, FAIR-aligned order.

> **Drive Fusion never modifies, moves, or deletes your data.** It is a planning and
> reporting instrument. It reads your drives and writes only to its own catalog and to an
> export folder you choose. This is enforced structurally — a read-only I/O gateway, a CI
> lint that bans mutating calls, and a regression test asserting scanned trees are
> byte-identical before and after a full run — not by policy alone.

Python 3.11+ · PySide6 GUI · SQLite catalog · packaged as a standalone executable.

Aligned with the FAIR Principles, the NIH Data Management and Sharing Policy (including the
2026 DMS Plan format), and NDSA Levels of Digital Preservation.

**Status:** design phase. See [docs/PLAN.md](docs/PLAN.md) for the full build plan —
read-only guarantees, architecture, data model, durability and risk scoring, FAIR/NDSA
scorecards, NIH DMS exports, the placement planner, and milestones M0–M7.
