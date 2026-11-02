"""Bind scope roots to the volume they were catalogued from.

A root is stored as a path such as ``E:\\Research``, and a path names whatever
happens to be mounted there *now*. Unplug the catalogued drive and plug in its
backup, and Windows may hand the backup the same letter: a scan would then
record the backup's files under the original's volume, and a hash or verify
would read the backup's bytes as the original's content (docs/REVIEW.md F-01).

So every pass that reads through a root first asks which volume is at that path
today and compares its ``volume_guid`` with the one recorded for the root. A
root that does not match, or whose volume cannot be identified at all, is
refused for that pass rather than read as something it is not.

A root that does match also refreshes what the catalog knows of its volume's
space: capacity, free space and cluster size, as discovery reports them now.
Free space is otherwise recorded only when a root is first added, and the
planner sizes every copy against it (ROADMAP M7.3), so a drive that has filled
up since would be planned onto room it no longer has.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from drivefusion.core import discovery
from drivefusion.core.store.catalog import Catalog, ScopeRoot


@dataclass(frozen=True)
class RootBinding:
    """Which roots may be read in this pass, and why the others may not."""

    bound: list[ScopeRoot] = field(default_factory=list)
    refused: list[tuple[ScopeRoot, str]] = field(default_factory=list)

    @property
    def root_ids(self) -> frozenset[int]:
        return frozenset(root.id for root in self.bound)

    def refusal_lines(self) -> list[str]:
        return [f"{root.path}: {reason}" for root, reason in self.refused]


def _describe(label: str | None, guid: str) -> str:
    return f"{label} ({guid})" if label and label != guid else guid


def check_root(catalog: Catalog, root: ScopeRoot) -> str | None:
    """Why ``root`` must not be read now, or ``None`` if it may be."""
    return _check(catalog, root)[0]


def _check(catalog: Catalog, root: ScopeRoot):
    """(why ``root`` must not be read, or None; the volume discovered there, if any)."""
    recorded = catalog.conn.execute(
        "SELECT volume_guid, label FROM volume WHERE id = ?", (root.volume_id,)
    ).fetchone()
    if recorded is None:
        return f"{root.path} has no catalogued volume to check it against", None

    expected = _describe(recorded["label"], recorded["volume_guid"])
    # Checked first because Windows resolves a missing folder to the volume
    # holding its parent, which would pass the GUID test below.
    if not os.path.isdir(root.path):
        return f"{root.path} is not there; is {expected} attached?", None

    try:
        current = discovery.identify_volume(root.path)
    except (discovery.DiscoveryError, OSError) as exc:
        return (
            f"cannot identify the volume at {root.path} ({exc}); "
            f"is {expected} attached?"
        ), None

    if current.volume_guid != recorded["volume_guid"]:
        return (
            f"{root.path} is now on volume "
            f"{_describe(current.label, current.volume_guid)}, not the catalogued "
            f"volume {expected}; "
            "another drive may have been given this letter"
        ), current
    return None, current


def _refresh_space(catalog: Catalog, volume_id: int, current) -> None:
    """Record the volume's space as discovery sees it now; a value it could not
    read leaves the recorded one as it was."""
    catalog.conn.execute(
        "UPDATE volume SET capacity_bytes = COALESCE(?, capacity_bytes), "
        "free_bytes = COALESCE(?, free_bytes), cluster_bytes = COALESCE(?, cluster_bytes) "
        "WHERE id = ?",
        (getattr(current, "capacity_bytes", None), getattr(current, "free_bytes", None),
         getattr(current, "cluster_bytes", None), volume_id),
    )


def bind_roots(catalog: Catalog, roots: Iterable[ScopeRoot]) -> RootBinding:
    """Check each root against its recorded volume."""
    binding = RootBinding()
    refreshed = False
    for root in roots:
        problem, current = _check(catalog, root)
        if problem is None:
            binding.bound.append(root)
            if current is not None:
                _refresh_space(catalog, root.volume_id, current)
                refreshed = True
        else:
            binding.refused.append((root, problem))
    if refreshed:
        catalog.conn.commit()
    return binding
