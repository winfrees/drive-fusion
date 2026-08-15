"""Smoke tests for the real-volume fixtures (Windows + administrator only).

These exist so the VHDX harness is exercised rather than merely present: an
untested fixture is config that rots. They also pin two premises the plan rests
on — that a real exFAT volume is available to test against, and that cluster
slack is large enough to matter for capacity planning (docs/PLAN.md §9).

Everywhere else they skip, which is why no other test depends on them.
"""

from __future__ import annotations

import shutil
import sys

import pytest

from drivefusion.core import fsio

pytestmark = [pytest.mark.windows, pytest.mark.admin]


def _make_tree(volume) -> None:
    root = volume.root
    (root / "project").mkdir(exist_ok=True)
    (root / "project" / "a.txt").write_bytes(b"alpha\n")
    (root / "project" / "b.txt").write_bytes(b"beta\n")


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows")
def test_ntfs_volume_is_scannable(ntfs_volume) -> None:
    _make_tree(ntfs_volume)
    names = {e.name for e in fsio.walk(str(ntfs_volume.root))}
    assert {"project", "a.txt", "b.txt"} <= names


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows")
def test_exfat_volume_is_scannable(exfat_volume) -> None:
    _make_tree(exfat_volume)
    names = {e.name for e in fsio.walk(str(exfat_volume.root))}
    assert {"project", "a.txt", "b.txt"} <= names


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows")
def test_exfat_cluster_slack_is_real(exfat_volume) -> None:
    """A one-byte file consumes a whole cluster, and the plan depends on it.

    If this ever stops being true the planner's footprint model (§9) is
    over-engineered; while it is true, estimating from logical bytes would
    understate a destination's usage by orders of magnitude for small files.
    """
    cluster = exfat_volume.cluster_bytes
    scratch = exfat_volume.root / "slack"
    scratch.mkdir(exist_ok=True)

    before = shutil.disk_usage(exfat_volume.root).free
    count = 16
    for index in range(count):
        (scratch / f"tiny-{index:03d}.bin").write_bytes(b"x")
    after = shutil.disk_usage(exfat_volume.root).free

    consumed = before - after
    logical = count  # 16 bytes of actual content
    assert consumed >= count * cluster * 0.5, (
        f"expected roughly {count * cluster} bytes consumed for {logical} "
        f"logical bytes, saw {consumed}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows")
def test_volume_handle_path_is_well_formed(ntfs_volume) -> None:
    """The path the M2 enumeration backends will open read-only."""
    assert ntfs_volume.volume_handle_path == f"\\\\.\\{ntfs_volume.letter}:"
