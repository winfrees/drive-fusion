"""Parallel batch directory enumeration — the exFAT path.

Half the fleet has no MFT and no change journal, so every rescan of those
volumes is a full re-walk (docs/PLAN.md §6.4). That cost cannot be removed, so
the goal is to make the walk as cheap as a walk can be:

* **Batch reads.** ``GetFileInformationByHandleEx`` returns many entries per
  call against an open directory handle, instead of a ``FindNextFile`` round
  trip plus a ``stat`` for every file.
* **Parallel directory reads.** Metadata reads are small and latency-bound, so
  a pool of 4–8 threads helps *even on spinning disks* — queue depth lets the
  drive reorder requests. This is deliberately the opposite of the 1–2 reader
  limit used for bulk data hashing, where large sequential reads are hurt by
  contention. Conflating the two is the usual way tools end up slow on HDDs.

The lister is injected, so the traversal, concurrency, and error handling are
all testable without Windows.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from drivefusion.core import fsio
from drivefusion.core.enum.records import DirEntry

WINDOWS = sys.platform == "win32"

#: Directory reads are latency-bound rather than bandwidth-bound, so depth
#: helps even on HDDs. Kept modest: past this, queueing dominates.
DEFAULT_WORKERS = 6

#: Cap on directories submitted but not yet consumed. Without it, a wide tree
#: would queue millions of futures and defeat the streaming design.
DEFAULT_MAX_INFLIGHT = 256

Lister = Callable[[str], Sequence[DirEntry]]


@dataclass(frozen=True, slots=True)
class DirectoryResult:
    """One directory's listing, or the error that prevented it."""

    path: str
    entries: tuple[DirEntry, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fsio_lister(path: str) -> list[DirEntry]:
    """Portable lister over the read-only gateway.

    Used on non-Windows for development and tests, and as the fallback when the
    Win32 batch call is unavailable. Metadata already rides on the gateway's
    entries, so this costs no extra syscalls.
    """
    out: list[DirEntry] = []
    for entry in fsio.scandir(path):
        attributes = entry.attributes
        if entry.is_dir:
            attributes |= fsio.FILE_ATTRIBUTE_DIRECTORY
        if entry.is_symlink:
            # A POSIX symlink is a reparse point in Windows terms. Mapping it
            # here keeps one traversal rule for both platforms instead of two
            # notions of "do not follow this".
            attributes |= fsio.FILE_ATTRIBUTE_REPARSE_POINT
        out.append(
            DirEntry(
                name=entry.name,
                attributes=attributes,
                size=entry.size,
                alloc_size=0,
                mtime_ns=entry.mtime_ns,
                ctime_ns=entry.ctime_ns,
                file_id=entry.file_id,
                stat_failed=entry.stat_failed,
            )
        )
    return out


def default_lister() -> Lister:
    """The best available directory lister for this platform."""
    if WINDOWS:
        from drivefusion.core.enum import dirinfo

        return dirinfo.list_directory
    return fsio_lister


class ParallelWalker:
    """Breadth-first directory traversal across a small thread pool."""

    def __init__(
        self,
        lister: Lister | None = None,
        *,
        workers: int = DEFAULT_WORKERS,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        follow_links: bool = False,
        prune: Callable[[str, DirEntry], bool] | None = None,
    ) -> None:
        self.lister = lister or default_lister()
        self.workers = max(1, workers)
        self.max_inflight = max(1, max_inflight)
        self.follow_links = follow_links
        #: Called with (parent_path, entry); returning True stops descent.
        #: The caller's exclusion rules have to reach the traversal, not just
        #: the recording step — otherwise an excluded directory is still
        #: listed, and its children arrive with no parent the caller knows.
        self.prune = prune

    def _should_descend(self, parent: str, entry: DirEntry) -> bool:
        if not entry.is_dir:
            return False
        if entry.name in (".", ".."):
            return False
        if not self.follow_links and fsio.is_reparse_point(entry.attributes):
            # A junction loop must not turn a bounded volume into an
            # unbounded walk.
            return False
        if self.prune is not None and self.prune(parent, entry):
            return False
        return True

    def walk(self, root: str) -> Iterator[DirectoryResult]:
        """Yield one result per directory, in completion order.

        Order is not deterministic — that is the price of parallelism, and the
        catalog does not care, because directories are interned by name rather
        than by visit order.
        """
        backlog: deque[str] = deque([root])
        inflight: dict = {}

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while backlog or inflight:
                while backlog and len(inflight) < self.max_inflight:
                    path = backlog.popleft()
                    inflight[pool.submit(self._list, path)] = path

                if not inflight:
                    continue

                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for future in done:
                    path = inflight.pop(future)
                    result = future.result()
                    yield result
                    if not result.ok:
                        continue
                    for entry in result.entries:
                        if self._should_descend(path, entry):
                            backlog.append(os.path.join(path, entry.name))

    def _list(self, path: str) -> DirectoryResult:
        try:
            return DirectoryResult(path=path, entries=tuple(self.lister(path)))
        except Exception as exc:
            # An unreadable directory is data, not a failure — but it is
            # reported, never silently dropped: a scan that omits what it could
            # not read would claim complete coverage of a partial tree.
            return DirectoryResult(path=path, error=str(exc))
