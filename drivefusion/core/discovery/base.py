"""Volume and drive discovery, behind one interface.

Windows is the supported platform; the POSIX provider exists so the catalog,
scope, and scan pipeline can be developed and tested on any machine. Which
provider ran is recorded, so a report can always say how its facts were
obtained.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class DriveInfo:
    """A physical device. ``serial`` is the identity anchor."""

    serial: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    bus: str | None = None
    media: str | None = None
    capacity_bytes: int | None = None


@dataclass(frozen=True)
class VolumeInfo:
    """A filesystem. ``volume_guid`` is the identity anchor, not a letter."""

    volume_guid: str
    label: str | None = None
    fs_type: str | None = None
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    cluster_bytes: int | None = None
    last_letter: str | None = None
    dirty_flag: bool | None = None
    supports_hardlink: bool | None = None
    supports_usn: bool | None = None
    supports_file_ids: bool | None = None
    drive: DriveInfo | None = field(default=None)
    provider: str = "unknown"

    @property
    def rescan_cost(self) -> str:
        """How expensive it is to confirm this volume is current.

        NTFS can be confirmed from the change journal in seconds; exFAT has no
        journal, so every rescan is a full re-walk. The UI shows this so the
        two are never averaged into one misleading number (docs/PLAN.md §6.4).
        """
        return "delta" if self.supports_usn else "full-walk"

    def as_volume_fields(self) -> dict:
        """Column values for ``Catalog.upsert_volume``."""
        return {
            "label": self.label,
            "fs_type": self.fs_type,
            "capacity_bytes": self.capacity_bytes,
            "free_bytes": self.free_bytes,
            "cluster_bytes": self.cluster_bytes,
            "last_letter": self.last_letter,
            "dirty_flag": None if self.dirty_flag is None else int(self.dirty_flag),
            "supports_hardlink": _as_int(self.supports_hardlink),
            "supports_usn": _as_int(self.supports_usn),
            "supports_file_ids": _as_int(self.supports_file_ids),
            "rescan_cost": self.rescan_cost,
        }


def _as_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


class DiscoveryError(RuntimeError):
    """Raised when a volume cannot be identified."""


def identify_volume(path: str) -> VolumeInfo:
    """Identify the volume containing ``path``."""
    if WINDOWS:
        from drivefusion.core.discovery import windows

        return windows.identify_volume(path)
    from drivefusion.core.discovery import posix

    return posix.identify_volume(path)


def list_volumes() -> list[VolumeInfo]:
    """Every volume currently attached. Not the same as volumes in scope."""
    if WINDOWS:
        from drivefusion.core.discovery import windows

        return windows.list_volumes()
    from drivefusion.core.discovery import posix

    return posix.list_volumes()
