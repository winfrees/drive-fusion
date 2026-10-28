"""The scan pipeline: walk a scope root, stage rows, merge, roll up.

This is the unprivileged walker. It is the only enumeration backend at M1 and
remains the only one available for exFAT volumes, which have no MFT and no
change journal (docs/PLAN.md §6.4). The NTFS fast path arrives at M2 behind the
same interface.

Two constraints shape the code more than anything else:

* **Nothing is materialised.** Entries stream into a bounded batch and are
  flushed; peak memory must not scale with the size of the volume.
* **Coverage is reported, never assumed.** Files skipped because they were
  unreadable, excluded, or dehydrated are counted and surfaced. A scan that
  quietly omitted part of a tree would produce a copy count that is confidently
  wrong rather than obviously incomplete.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from drivefusion.core import fsio
from drivefusion.core.enum.batchwalk import DEFAULT_WORKERS, ParallelWalker
from drivefusion.core.errors import OperationCancelled, UnreadableError
from drivefusion.core.scope.registry import ExclusionSet
from drivefusion.core.store.catalog import BATCH_ROWS, Catalog, ScanStats

#: Commit every this many staged rows, bounding the work an interrupted scan
#: loses. Scans are resumable in the sense that a partial scan is discarded and
#: re-run; the catalog is never left half-merged, because the merge is one
#: transaction.
CHECKPOINT_ROWS = 50_000


@dataclass
class ScanCounters:
    dirs: int = 0
    #: Files catalogued, including the ``unreadable`` ones.
    files: int = 0
    bytes: int = 0
    #: Files whose metadata could not be read; catalogued as unreadable.
    unreadable: int = 0
    #: Directories that could not be listed. What they hold is unknown, so
    #: they are not counted as files of any kind (docs/REVIEW.md F-15).
    unreadable_dirs: int = 0
    dehydrated: int = 0
    excluded: int = 0
    reparse_skipped: int = 0
    #: Listings that arrived for a directory the scan never interned. Should
    #: always be zero; non-zero means traversal and recording disagreed.
    unplaced: int = 0
    unreadable_paths: list[str] = field(default_factory=list)

    def coverage_line(self) -> str:
        """One line a human can act on, in the shape docs/PLAN.md §6.7 asks for."""
        parts = [f"{self.files - self.unreadable:,} of {self.files:,} files read"]
        if self.unreadable:
            parts.append(f"{self.unreadable:,} unreadable")
        if self.unreadable_dirs:
            parts.append(
                f"{self.unreadable_dirs:,} folder(s) could not be listed, so what "
                "they hold is unknown"
            )
        if self.dehydrated:
            parts.append(f"{self.dehydrated:,} cloud placeholders catalogued by metadata")
        if self.excluded:
            parts.append(f"{self.excluded:,} excluded")
        if self.reparse_skipped:
            parts.append(f"{self.reparse_skipped:,} links not traversed")
        if self.unplaced:
            parts.append(f"{self.unplaced:,} unplaced listings (report this)")
        return "; ".join(parts)


def _extension(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext or None


def stage_row_for(scan_id: int, volume_id: int, dir_id: int, entry, read_state: str):
    """Build one staging tuple. Shared with the delta path so a full scan and
    an incremental scan cannot drift into recording files differently."""
    return (
        scan_id, volume_id, dir_id, entry.name,
        _extension(entry.name), entry.size, entry.alloc_size or None,
        entry.mtime_ns, entry.ctime_ns, entry.file_id,
        1, entry.attributes, read_state,
    )


def scan_root(
    catalog: Catalog,
    *,
    root_path: str,
    root_id: int | None,
    volume_id: int,
    excludes: ExclusionSet | None = None,
    progress: Callable[[ScanCounters], None] | None = None,
    method: str = "walk",
    lister=None,
    workers: int = DEFAULT_WORKERS,
    supports_file_ids: bool | None = None,
    cancel: Callable[[], None] | None = None,
) -> dict:
    """Enumerate one scope root into the catalog. Returns a result summary.

    ``cancel`` is called once per directory listed and may raise
    :class:`~drivefusion.core.errors.OperationCancelled` to stop the scan,
    which is then rolled back and recorded as aborted (docs/REVIEW.md F-13).

    Only directories whose listing succeeded may have anything tombstoned
    beneath them, and if the root itself cannot be listed nothing is merged at
    all: an unplugged drive is not a drive whose files were deleted
    (docs/REVIEW.md F-02). ``result["root_listed"]`` says which case this was.
    """
    excludes = excludes or ExclusionSet.for_root(())
    counters = ScanCounters()
    scan_id = catalog.begin_scan(volume_id, root_id, method, elevated=False)
    conn = catalog.conn

    root_dir_id = catalog.intern_dir(
        volume_id=volume_id,
        root_id=root_id,
        parent_id=None,
        name=root_path,
        depth=0,
        scan_id=scan_id,
    )

    pending: list[tuple] = []
    # Directories whose listing succeeded, staged so the merge tombstones only
    # inside them; the same rule the delta path follows.
    listed_dirs: list[int] = []
    root_listed = False
    root_error: str | None = None
    since_checkpoint = 0

    def prune(parent: str, entry) -> bool:
        """Keep the traversal and the recording rules identical.

        The walker must not descend into anything the scan would exclude:
        otherwise the excluded directory is still listed and its children
        arrive attached to a parent the scanner never interned.
        """
        entry_path = os.path.join(parent, entry.name)
        return excludes.excludes(
            name=entry.name,
            relpath=os.path.relpath(entry_path, root_path),
            absolute=entry_path,
        )

    walker = ParallelWalker(
        lister, workers=workers, prune=prune, supports_file_ids=supports_file_ids
    )

    # Directory ids for paths that have been discovered but whose own listing
    # has not been consumed yet. Entries are popped on consumption, so this
    # holds only the in-flight frontier rather than every directory on the
    # volume — the walker submits a child only after its parent's result has
    # been processed, so the id is always present when needed.
    dir_ids: dict[str, tuple[int, int]] = {root_path: (root_dir_id, 0)}

    conn.execute("BEGIN")
    try:
        for result in walker.walk(root_path):
            if cancel:
                cancel()
            known = dir_ids.pop(result.path, None)
            if known is None:
                # A listing for a directory that was never interned. There is
                # no correct parent to file its contents under, and defaulting
                # to the root would silently misplace real files, so it is
                # counted and skipped rather than guessed at.
                counters.unplaced += 1
                continue
            current_dir_id, depth = known

            if not result.ok:
                if current_dir_id == root_dir_id:
                    root_error = result.error
                counters.unreadable_dirs += 1
                if len(counters.unreadable_paths) < 50:
                    counters.unreadable_paths.append(result.path)
                continue
            counters.dirs += 1
            if current_dir_id == root_dir_id:
                root_listed = True
            listed_dirs.append(current_dir_id)
            if len(listed_dirs) >= BATCH_ROWS:
                catalog.stage_dirs(scan_id, listed_dirs)
                listed_dirs.clear()

            for entry in result.entries:
                entry_path = os.path.join(result.path, entry.name)
                relpath = os.path.relpath(entry_path, root_path)
                if excludes.excludes(
                    name=entry.name, relpath=relpath, absolute=entry_path
                ):
                    counters.excluded += 1
                    continue

                if fsio.skip_as_link(
                    entry.attributes, entry.reparse_tag, is_dir=entry.is_dir
                ):
                    # Reported, never traversed: a junction loop must not turn
                    # a bounded volume into an unbounded walk.
                    counters.reparse_skipped += 1
                    continue

                if entry.is_dir:
                    child_id = catalog.intern_dir(
                        volume_id=volume_id,
                        root_id=root_id,
                        parent_id=current_dir_id,
                        name=entry.name,
                        depth=depth + 1,
                        scan_id=scan_id,
                        frn=entry.file_id,
                    )
                    dir_ids[entry_path] = (child_id, depth + 1)
                    continue

                if entry.stat_failed:
                    counters.unreadable += 1
                    read_state = "unreadable"
                elif fsio.is_dehydrated(entry.attributes):
                    # Catalogued from metadata; never opened (§3.2).
                    counters.dehydrated += 1
                    read_state = "skipped-dehydrated"
                else:
                    read_state = "ok"

                counters.files += 1
                counters.bytes += entry.size
                pending.append(
                    stage_row_for(
                        scan_id, volume_id, current_dir_id, entry, read_state
                    )
                )

                if len(pending) >= BATCH_ROWS:
                    catalog.stage_files(pending)
                    since_checkpoint += len(pending)
                    pending.clear()
                    if since_checkpoint >= CHECKPOINT_ROWS:
                        conn.execute("COMMIT")
                        conn.execute("BEGIN")
                        since_checkpoint = 0
                    if progress:
                        progress(counters)

        if pending:
            catalog.stage_files(pending)
            pending.clear()
        if listed_dirs:
            catalog.stage_dirs(scan_id, listed_dirs)
            listed_dirs.clear()
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        cancelled = isinstance(exc, OperationCancelled)
        catalog.finish_scan(
            ScanStats(scan_id=scan_id),
            status="aborted" if cancelled else "failed",
            error="cancelled" if cancelled else str(exc),
        )
        raise

    if not root_listed:
        return _refuse_merge(catalog, scan_id, root_path, root_error, counters)

    merged = catalog.merge_scan(
        scan_id, volume_id, root_id, vanish_scope="listed-dirs"
    )
    catalog.rebuild_rollups(volume_id)
    catalog.finish_scan(
        ScanStats(
            scan_id=scan_id,
            dirs_seen=counters.dirs,
            files_seen=counters.files,
            bytes_seen=counters.bytes,
            # Entries not read, files and folders alike.
            unreadable=counters.unreadable + counters.unreadable_dirs,
            dehydrated_skipped=counters.dehydrated,
            excluded=counters.excluded,
        )
    )
    catalog.analyze()

    return {
        "scan_id": scan_id,
        "counters": counters,
        "merged": merged,
        "coverage": counters.coverage_line(),
        "root_listed": True,
    }


def _refuse_merge(
    catalog: Catalog,
    scan_id: int,
    root_path: str,
    error: str | None,
    counters: ScanCounters,
) -> dict:
    """Discard a scan whose root could not be listed, leaving the catalog as it was."""
    reason = f"root could not be listed: {error or 'no listing returned'}"
    conn = catalog.conn
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM stage_file WHERE scan_id = ?", (scan_id,))
        conn.execute("DELETE FROM stage_dir WHERE scan_id = ?", (scan_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    catalog.finish_scan(
        ScanStats(
            scan_id=scan_id, unreadable=counters.unreadable + counters.unreadable_dirs
        ),
        status="failed",
        error=reason,
    )
    return {
        "scan_id": scan_id,
        "counters": counters,
        "merged": {"staged": 0, "inserted": 0, "updated": 0, "vanished": 0},
        "coverage": (
            f"{root_path}: {reason}; nothing was merged and nothing was marked "
            "vanished"
        ),
        "root_listed": False,
    }


def preview_root(
    root_path: str,
    excludes: ExclusionSet | None = None,
    *,
    max_entries: int | None = None,
) -> ScanCounters:
    """Count what a scan would cover, without writing anything.

    The dry run exists so a first scan of an unfamiliar drive is a decision
    rather than a surprise (docs/PLAN.md §4).
    """
    excludes = excludes or ExclusionSet.for_root(())
    counters = ScanCounters()
    stack = [root_path]

    while stack:
        current = stack.pop()
        try:
            entries = list(fsio.scandir(current))
        except UnreadableError:
            counters.unreadable_dirs += 1
            continue
        counters.dirs += 1

        for entry in entries:
            relpath = os.path.relpath(entry.path, root_path)
            if excludes.excludes(
                name=entry.name, relpath=relpath, absolute=entry.path
            ):
                counters.excluded += 1
                continue
            if entry.is_symlink or fsio.skip_as_link(
                entry.attributes, entry.reparse_tag, is_dir=entry.is_dir
            ):
                counters.reparse_skipped += 1
                continue
            if entry.is_dir:
                stack.append(entry.path)
                continue
            counters.files += 1
            counters.bytes += entry.size
            if max_entries is not None and counters.files >= max_entries:
                return counters
    return counters
