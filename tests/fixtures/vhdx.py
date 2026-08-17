"""Real NTFS and exFAT volumes for tests, built from VHDX images.

Two things in the plan cannot be tested against a temp directory:

* the NTFS fast path (MFT and USN journal access, M2) needs a real NTFS volume
  opened by volume handle; and
* the exFAT path needs a volume with no journal, no file IDs, no hardlinks, and
  a large cluster size — cluster-slack arithmetic is invisible on a typical
  NTFS test directory.

So CI attaches genuine volumes. This requires Windows and administrator rights;
everywhere else the fixtures skip, which is why the pure-logic tests never
depend on them.
"""

from __future__ import annotations

import ctypes
import os
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"

#: 128 KiB, a common exFAT default on large external drives and the reason
#: destination footprints must be computed in clusters (docs/PLAN.md §9).
EXFAT_CLUSTER_BYTES = 131072


def is_admin() -> bool:
    if WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def skip_reason() -> str | None:
    """Why the VHDX fixtures cannot run here, or None if they can."""
    if not WINDOWS:
        return "VHDX volume fixtures require Windows"
    if not is_admin():
        return "VHDX volume fixtures require administrator rights (diskpart)"
    return None


def free_drive_letter() -> str:
    used = set()
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for index, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << index):
            used.add(letter)
    for letter in reversed(string.ascii_uppercase[7:]):  # H: through Z:
        if letter not in used:
            return letter
    raise RuntimeError("no free drive letter available for a test volume")


def _diskpart(script: str) -> None:
    """Run a diskpart script, raising with its output if it fails."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="ascii"
    ) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["diskpart", "/s", script_path],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"diskpart failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
            )
    finally:
        os.unlink(script_path)


@dataclass
class TestVolume:
    """An attached test volume. ``root`` is where fixture content goes."""

    image_path: Path
    letter: str
    filesystem: str
    cluster_bytes: int

    @property
    def root(self) -> Path:
        return Path(f"{self.letter}:\\")

    @property
    def volume_handle_path(self) -> str:
        """The raw device path enumeration backends open (read-only)."""
        return f"\\\\.\\{self.letter}:"


def create_volume(
    image_path: Path,
    filesystem: str = "ntfs",
    size_mb: int = 512,
    cluster_bytes: int | None = None,
) -> TestVolume:
    """Create, attach, partition, and format a VHDX. Windows + admin only."""
    reason = skip_reason()
    if reason:
        raise RuntimeError(reason)

    filesystem = filesystem.lower()
    if filesystem not in {"ntfs", "exfat"}:
        raise ValueError(f"unsupported filesystem for fixtures: {filesystem}")
    if cluster_bytes is None:
        cluster_bytes = EXFAT_CLUSTER_BYTES if filesystem == "exfat" else 4096

    letter = free_drive_letter()
    image_path.parent.mkdir(parents=True, exist_ok=True)

    _diskpart(
        f'create vdisk file="{image_path}" maximum={size_mb} type=expandable\n'
        f'select vdisk file="{image_path}"\n'
        "attach vdisk\n"
        "create partition primary\n"
        f"format fs={filesystem} unit={cluster_bytes} label=DFTEST quick\n"
        f"assign letter={letter}\n"
    )
    return TestVolume(
        image_path=image_path,
        letter=letter,
        filesystem=filesystem,
        cluster_bytes=cluster_bytes,
    )


def destroy_volume(volume: TestVolume) -> None:
    """Detach and remove a test volume. Only ever touches our own image."""
    try:
        _diskpart(
            f'select vdisk file="{volume.image_path}"\n'
            "detach vdisk\n"
        )
    finally:
        if volume.image_path.exists():
            os.unlink(volume.image_path)
