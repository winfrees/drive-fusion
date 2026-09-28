"""Incremental rescan driven by the NTFS change journal.

This is the payoff of M2: confirming an unchanged 10M-file volume in seconds
rather than re-walking it (docs/PLAN.md §6.3).

The strategy is deliberately simple, because the alternative is fragile. A
journal record carries a *reason* bitmask — created, deleted, renamed, data
overwritten, and a dozen more — and reconstructing filesystem state by
replaying those transitions is easy to get subtly wrong, especially for
renames and for files that were created and deleted between two scans.

So the journal is used only to answer one question: **which directories should
I look at?** The directory listing is then the source of truth for what is
actually there. That makes a delta a strict subset of a full scan rather than a
different algorithm, and it is why the two can be asserted to agree.

Everything here takes its records and its lister as arguments, so the whole
orchestration is testable without Windows and without a real journal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from drivefusion.core.enum.batchwalk import DEFAULT_WORKERS, ParallelWalker
from drivefusion.core.enum.records import DirEntry, UsnRecord
from drivefusion.core.errors import UnreadableError
from drivefusion.core.scope.registry import ExclusionSet
from drivefusion.core.store.catalog import Catalog, ScanStats

#: If the journal names more distinct directories than this share of the
#: catalogued directories, a delta has stopped being cheaper than a full pass.
FULL_SCAN_THRESHOLD = 0.5


class DeltaNotApplicable(RuntimeError):
    """Raised when a delta cannot or should not be used; fall back to a walk."""


@dataclass
class DeltaCounters:
    records: int = 0
    touched_dirs: int = 0
    listed_dirs: int = 0
    new_subtrees: int = 0
    files: int = 0
    bytes: int = 0
    unreadable: int = 0
    dehydrated: int = 0
    excluded: int = 0
    unknown_parents: int = 0
    vanished_dirs: int = 0
    unreadable_paths: list[str] = field(default_factory=list)

    def coverage_line(self) -> str:
        parts = [
            f"{self.records:,} journal records",
            f"{self.listed_dirs:,} directories re-read",
            f"{self.files:,} files recorded",
        ]
        if self.new_subtrees:
            parts.append(f"{self.new_subtrees:,} new subtrees walked")
        if self.unknown_parents:
            parts.append(f"{self.unknown_parents:,} changes outside scope")
        if self.unreadable:
            parts.append(f"{self.unreadable:,} unreadable")
        if self.vanished_dirs:
            parts.append(f"{self.vanished_dirs:,} directories gone")
        return "; ".join(parts)


def touched_parent_frns(records: Iterable[UsnRecord]) -> tuple[set[int], int, int]:
    """Collect the directories whose contents may have changed.

    A record names the changed item and its parent. The parent is what needs
    re-reading: it holds the current truth about whether the item still exists,
    what it is called now, and how big it is.

    A record for a *directory* also contributes its own frn, because a
    directory that was renamed or moved changes the paths of everything beneath
    it, and re-reading it is how those are picked up.
    """
    parents: set[int] = set()
    count = 0
    dir_records = 0
    for record in records:
        count += 1
        parents.add(record.parent_frn)
        if record.is_dir:
            dir_records += 1
            parents.add(record.frn)
    return parents, count, dir_records


def _entry_state(entry: DirEntry) -> tuple[str, bool, bool]:
    """Classify an entry: (read_state, is_dehydrated, is_unreadable)."""
    from drivefusion.core import fsio

    if entry.stat_failed:
        return "unreadable", False, True
    if fsio.is_dehydrated(entry.attributes):
        return "skipped-dehydrated", True, False
    return "ok", False, False


def apply_delta(
    catalog: Catalog,
    *,
    root_path: str,
    root_id: int | None,
    volume_id: int,
    records: Sequence[UsnRecord] | Iterable[UsnRecord],
    lister: Callable[[str], Sequence[DirEntry]] | None = None,
    excludes: ExclusionSet | None = None,
    next_usn: int | None = None,
    journal_id: int | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict:
    """Apply a journal delta to the catalog.

    Raises :class:`DeltaNotApplicable` when the change set is large enough that
    a full pass is the better choice, so the caller can fall back rather than
    do more work than a re-walk would have cost.
    """
    from drivefusion.core.enum.batchwalk import fsio_lister
    from drivefusion.core.scan.scanner import stage_row_for

    lister = lister or fsio_lister
    excludes = excludes or ExclusionSet.for_root(())
    counters = DeltaCounters()

    parents, record_count, _ = touched_parent_frns(records)
    counters.records = record_count
    counters.touched_dirs = len(parents)

    catalogued_dirs = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM dir WHERE volume_id = ? AND vanished_at IS NULL",
        (volume_id,),
    ).fetchone()["n"]
    if catalogued_dirs and len(parents) > catalogued_dirs * FULL_SCAN_THRESHOLD:
        raise DeltaNotApplicable(
            f"{len(parents):,} of {catalogued_dirs:,} directories changed; "
            "a full pass is cheaper than a delta this large"
        )

    # Resolve journal parent references to directories the catalog knows. An
    # unresolved parent is a change outside the scanned scope, or in a
    # directory created and removed between scans; either way there is nothing
    # to re-read, and inventing a location would be worse than counting it.
    targets: list[tuple[int, str]] = []
    for frn in sorted(parents):
        row = catalog.dir_by_frn(volume_id, frn)
        if row is None:
            counters.unknown_parents += 1
            continue
        targets.append((int(row["id"]), catalog.dir_path(int(row["id"]), os.sep)))

    scan_id = catalog.begin_scan(volume_id, root_id, "usn-delta", elevated=True)
    conn = catalog.conn
    pending: list[tuple] = []
    staged_dirs: list[int] = []

    conn.execute("BEGIN")
    try:
        for dir_id, dir_path in targets:
            try:
                entries = list(lister(dir_path))
            except (UnreadableError, OSError) as exc:
                # Could not read it: the directory may be gone, or merely
                # locked. Not staging it means nothing inside is tombstoned,
                # which under-reports deletions rather than inventing them.
                counters.unreadable += 1
                if len(counters.unreadable_paths) < 50:
                    counters.unreadable_paths.append(f"{dir_path}: {exc}")
                continue

            counters.listed_dirs += 1
            staged_dirs.append(dir_id)
            catalog.conn.execute(
                "UPDATE dir SET last_seen_scan = ?, vanished_at = NULL WHERE id = ?",
                (scan_id, dir_id),
            )

            seen_child_dirs: set[str] = set()
            for entry in entries:
                entry_path = os.path.join(dir_path, entry.name)
                relpath = os.path.relpath(entry_path, root_path)
                if excludes.excludes(
                    name=entry.name, relpath=relpath, absolute=entry_path
                ):
                    counters.excluded += 1
                    continue

                from drivefusion.core import fsio

                if fsio.is_reparse_point(entry.attributes):
                    continue

                if entry.is_dir:
                    seen_child_dirs.add(entry.name)
                    child = conn.execute(
                        "SELECT id FROM dir WHERE volume_id = ? AND parent_id = ? "
                        "AND name = ?",
                        (volume_id, dir_id, entry.name),
                    ).fetchone()
                    if child is None:
                        # A directory the catalog has never seen: the journal
                        # named its creation, but everything beneath it is new
                        # too, so the whole subtree has to be walked.
                        counters.new_subtrees += 1
                        _walk_new_subtree(
                            catalog,
                            conn=conn,
                            scan_id=scan_id,
                            volume_id=volume_id,
                            root_id=root_id,
                            root_path=root_path,
                            parent_dir_id=dir_id,
                            parent_path=dir_path,
                            name=entry.name,
                            frn=entry.file_id,
                            depth=_depth_of(conn, dir_id) + 1,
                            excludes=excludes,
                            lister=lister,
                            workers=workers,
                            counters=counters,
                            pending=pending,
                            staged_dirs=staged_dirs,
                        )
                    else:
                        conn.execute(
                            "UPDATE dir SET last_seen_scan = ?, frn = COALESCE(?, frn), "
                            "vanished_at = NULL WHERE id = ?",
                            (scan_id, entry.file_id, int(child["id"])),
                        )
                    continue

                read_state, dehydrated, unreadable = _entry_state(entry)
                counters.dehydrated += dehydrated
                counters.unreadable += unreadable
                counters.files += 1
                counters.bytes += entry.size
                pending.append(
                    stage_row_for(scan_id, volume_id, dir_id, entry, read_state)
                )

            # Child directories the listing no longer contains are gone, along
            # with everything beneath them.
            counters.vanished_dirs += _tombstone_missing_children(
                catalog, volume_id, dir_id, seen_child_dirs
            )

        if pending:
            catalog.stage_files(pending)
        catalog.stage_dirs(scan_id, staged_dirs)
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        catalog.finish_scan(
            ScanStats(scan_id=scan_id), status="failed", error=str(exc)
        )
        raise

    merged = catalog.merge_scan(
        scan_id, volume_id, root_id, vanish_scope="staged-dirs"
    )
    catalog.rebuild_rollups(volume_id)
    catalog.finish_scan(
        ScanStats(
            scan_id=scan_id,
            dirs_seen=counters.listed_dirs,
            files_seen=counters.files,
            bytes_seen=counters.bytes,
            unreadable=counters.unreadable,
            dehydrated_skipped=counters.dehydrated,
            excluded=counters.excluded,
        )
    )

    # Only now, after the merge committed. Advancing the cursor earlier would
    # mean a crash between reading and recording silently skipped changes.
    if next_usn is not None:
        catalog.set_journal_cursor(volume_id, journal_id, next_usn)

    return {
        "scan_id": scan_id,
        "counters": counters,
        "merged": merged,
        "coverage": counters.coverage_line(),
    }


def _depth_of(conn, dir_id: int) -> int:
    row = conn.execute("SELECT depth FROM dir WHERE id = ?", (dir_id,)).fetchone()
    return int(row["depth"]) if row else 0


def _tombstone_missing_children(
    catalog: Catalog, volume_id: int, parent_id: int, seen: set[str]
) -> int:
    """Tombstone child directories absent from a fresh listing, and their trees."""
    rows = catalog.conn.execute(
        "SELECT id, name FROM dir WHERE volume_id = ? AND parent_id = ? "
        "AND vanished_at IS NULL",
        (volume_id, parent_id),
    ).fetchall()

    gone = 0
    for row in rows:
        if row["name"] in seen:
            continue
        catalog.tombstone_subtree(int(row["id"]))
        gone += 1
    return gone


def _walk_new_subtree(
    catalog: Catalog,
    *,
    conn,
    scan_id: int,
    volume_id: int,
    root_id: int | None,
    root_path: str,
    parent_dir_id: int,
    parent_path: str,
    name: str,
    frn: int | None,
    depth: int,
    excludes: ExclusionSet,
    lister,
    workers: int,
    counters: DeltaCounters,
    pending: list,
    staged_dirs: list[int],
) -> None:
    """Enumerate a directory the catalog has never seen, in full."""
    from drivefusion.core import fsio
    from drivefusion.core.scan.scanner import stage_row_for

    subtree_root = os.path.join(parent_path, name)
    root_dir_id = catalog.intern_dir(
        volume_id=volume_id,
        root_id=root_id,
        parent_id=parent_dir_id,
        name=name,
        depth=depth,
        scan_id=scan_id,
        frn=frn,
    )
    staged_dirs.append(root_dir_id)

    def prune(parent: str, entry) -> bool:
        entry_path = os.path.join(parent, entry.name)
        return excludes.excludes(
            name=entry.name,
            relpath=os.path.relpath(entry_path, root_path),
            absolute=entry_path,
        )

    walker = ParallelWalker(lister, workers=workers, prune=prune)
    dir_ids = {subtree_root: (root_dir_id, depth)}

    for result in walker.walk(subtree_root):
        known = dir_ids.pop(result.path, None)
        if known is None:
            continue
        current_dir_id, current_depth = known

        if not result.ok:
            counters.unreadable += 1
            continue
        counters.listed_dirs += 1

        for entry in result.entries:
            entry_path = os.path.join(result.path, entry.name)
            relpath = os.path.relpath(entry_path, root_path)
            if excludes.excludes(
                name=entry.name, relpath=relpath, absolute=entry_path
            ):
                counters.excluded += 1
                continue
            if fsio.is_reparse_point(entry.attributes):
                continue

            if entry.is_dir:
                child_id = catalog.intern_dir(
                    volume_id=volume_id,
                    root_id=root_id,
                    parent_id=current_dir_id,
                    name=entry.name,
                    depth=current_depth + 1,
                    scan_id=scan_id,
                    frn=entry.file_id,
                )
                dir_ids[entry_path] = (child_id, current_depth + 1)
                staged_dirs.append(child_id)
                continue

            read_state, dehydrated, unreadable = _entry_state(entry)
            counters.dehydrated += dehydrated
            counters.unreadable += unreadable
            counters.files += 1
            counters.bytes += entry.size
            pending.append(
                stage_row_for(scan_id, volume_id, current_dir_id, entry, read_state)
            )
