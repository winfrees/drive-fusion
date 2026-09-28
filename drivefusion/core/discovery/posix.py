"""Development-only volume provider for non-Windows machines.

Windows is the supported platform (docs/PLAN.md §1). This provider exists so
the catalog, scope, and scan pipeline are developable and testable anywhere; it
identifies a "volume" by the device id the kernel reports for a path, which is
stable enough for tests and honest about being a stand-in.
"""

from __future__ import annotations

import os
import shutil

from drivefusion.core.discovery.base import DriveInfo, VolumeInfo

PROVIDER = "posix-dev"


def _mount_point(path: str) -> str:
    current = os.path.abspath(path)
    device = os.stat(current).st_dev
    while True:
        parent = os.path.dirname(current)
        if parent == current or os.stat(parent).st_dev != device:
            return current
        current = parent


def identify_volume(path: str) -> VolumeInfo:
    absolute = os.path.abspath(path)
    stat = os.stat(absolute)
    mount = _mount_point(absolute)
    usage = shutil.disk_usage(mount)
    statvfs = os.statvfs(mount)

    return VolumeInfo(
        volume_guid=f"dev:{stat.st_dev}",
        label=os.path.basename(mount) or mount,
        fs_type="posix",
        capacity_bytes=usage.total,
        free_bytes=usage.free,
        cluster_bytes=statvfs.f_frsize,
        last_letter=None,
        dirty_flag=None,
        supports_hardlink=True,
        supports_usn=False,
        supports_file_ids=True,
        drive=DriveInfo(serial=f"dev-{stat.st_dev}", media="unknown", bus="unknown"),
        provider=PROVIDER,
    )


def list_volumes() -> list[VolumeInfo]:
    """Only the volume holding the current directory; enough for development."""
    return [identify_volume(os.getcwd())]
