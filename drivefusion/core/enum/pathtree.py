"""Reconstruct paths from parent file-reference numbers.

A bulk NTFS enumeration returns a flat stream of records, each naming itself
and its parent. Paths are a join on that link. The scaling trick is that only
*directories* need to be held in memory — roughly 5% of entries — so a 50M-file
volume needs a ~2.5M-entry map rather than a 50M-entry one, and files stream
straight to the staging table carrying only a parent id (docs/PLAN.md §6.3).

This module is pure Python over integers and strings, so the interesting
failure modes — orphans, cycles from a corrupt journal, unreasonable depth —
are all testable without Windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

#: The NTFS volume root's file reference number. Its parent is itself.
NTFS_ROOT_FRN = 5

#: Depth beyond which a chain is treated as corrupt rather than deep. Windows
#: itself cannot create a path anywhere near this deep; reaching it means the
#: parent links are lying.
MAX_DEPTH = 512


class PathTreeError(RuntimeError):
    """Raised when the parent links cannot describe a tree."""


@dataclass(frozen=True, slots=True)
class DirNode:
    frn: int
    parent_frn: int
    name: str
    depth: int


class FrnTree:
    """Directory records keyed by file reference number.

    Deliberately stores only ``frn -> (parent_frn, name)``. Anything richer
    multiplied by millions of directories is memory the scan cannot spare.
    """

    __slots__ = ("_nodes", "_root_frns", "_depth_cache")

    def __init__(self, root_frns: tuple[int, ...] = (NTFS_ROOT_FRN,)) -> None:
        self._nodes: dict[int, tuple[int, str]] = {}
        self._root_frns = set(root_frns)
        self._depth_cache: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, frn: int) -> bool:
        return frn in self._nodes

    def add(self, frn: int, parent_frn: int, name: str) -> None:
        """Record one directory. A repeated frn replaces the earlier entry.

        Replacement matters for delta processing: a rename arrives as a new
        record for an frn already present, and the newer name is the truth.
        """
        self._nodes[frn] = (parent_frn, name)
        self._depth_cache.clear()

    def add_root(self, frn: int, name: str = "") -> None:
        """Mark an frn as a tree root, whatever its parent link says."""
        self._root_frns.add(frn)
        self._nodes.setdefault(frn, (frn, name))

    def is_root(self, frn: int) -> bool:
        if frn in self._root_frns:
            return True
        node = self._nodes.get(frn)
        # A directory whose parent is itself is the volume root.
        return node is not None and node[0] == frn

    def components(self, frn: int) -> tuple[str, ...] | None:
        """Path components from the root down, or None if the chain is broken.

        Returning None rather than raising is deliberate: an orphan is a normal
        outcome of a delta that references a directory created and removed
        between scans. Callers count orphans and report them; they must not
        treat one as fatal, and must not silently drop it either.
        """
        parts: list[str] = []
        current = frn
        seen: set[int] = set()

        for _ in range(MAX_DEPTH):
            if current in seen:
                raise PathTreeError(
                    f"cycle in parent links at frn {current}; the journal or "
                    "enumeration is inconsistent"
                )
            seen.add(current)

            node = self._nodes.get(current)
            if node is None:
                return None  # orphan: parent chain leaves the known set

            parent_frn, name = node
            if self.is_root(current):
                if name:
                    parts.append(name)
                parts.reverse()
                return tuple(parts)

            parts.append(name)
            current = parent_frn

        raise PathTreeError(
            f"parent chain from frn {frn} exceeded {MAX_DEPTH} levels; "
            "treating as corrupt rather than deep"
        )

    def path(self, frn: int, separator: str = "\\") -> str | None:
        parts = self.components(frn)
        return None if parts is None else separator.join(parts)

    def depth(self, frn: int) -> int | None:
        parts = self.components(frn)
        return None if parts is None else len(parts)

    def iter_topological(self) -> Iterator[DirNode]:
        """Yield directories parents-first, so they can be interned in order.

        Interning a directory requires its parent's row id, so the catalog can
        only consume this stream in an order where every parent precedes its
        children. Orphans are omitted here and reported separately by
        ``orphans()`` — inventing a placement for them would put real files
        under a fabricated path.
        """
        depths: dict[int, int] = {}
        for frn in self._nodes:
            computed = self.depth(frn)
            if computed is not None:
                depths[frn] = computed

        for frn in sorted(depths, key=lambda f: (depths[f], f)):
            parent_frn, name = self._nodes[frn]
            yield DirNode(
                frn=frn,
                parent_frn=parent_frn,
                name=name,
                depth=depths[frn],
            )

    def orphans(self) -> list[int]:
        """Directories whose parent chain does not reach a root."""
        return [frn for frn in self._nodes if self.components(frn) is None]


def build_dir_tree(
    records, *, root_frns: tuple[int, ...] = (NTFS_ROOT_FRN,)
) -> tuple[FrnTree, int]:
    """Consume an enumeration stream, keeping directories and counting files.

    Returns the directory tree and the number of file records seen. The file
    records themselves are intentionally not retained: at 50M files that is the
    whole point of the design.
    """
    tree = FrnTree(root_frns)
    files = 0
    for record in records:
        if record.is_dir:
            tree.add(record.frn, record.parent_frn, record.name)
        else:
            files += 1
    return tree, files
