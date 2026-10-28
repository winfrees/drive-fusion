"""A disk's own identity, read from its partition table (docs/REVIEW.md F-05).

Pure parsing, kept apart from the Win32 calls in ``windows.py`` so it is
tested on every platform. ``windows.py`` fetches the buffer with
``IOCTL_DISK_GET_DRIVE_LAYOUT_EX``.
"""

from __future__ import annotations

import uuid

PARTITION_STYLE_MBR = 0
PARTITION_STYLE_GPT = 1


def parse_drive_layout(raw: bytes) -> str | None:
    """The disk's own identity from a ``DRIVE_LAYOUT_INFORMATION_EX`` buffer.

    ``mbr:<signature>`` or ``gpt:<disk GUID>``; ``None`` for a RAW disk, an
    unsigned MBR disk, or a buffer too short to hold the header. The union
    after the two DWORD header fields starts at offset 8 for both styles.
    """
    if len(raw) < 8:
        return None
    style = int.from_bytes(raw[0:4], "little")
    if style == PARTITION_STYLE_MBR and len(raw) >= 12:
        signature = int.from_bytes(raw[8:12], "little")
        return f"mbr:{signature:08X}" if signature else None
    if style == PARTITION_STYLE_GPT and len(raw) >= 24:
        disk_id = raw[8:24]
        if not any(disk_id):
            return None
        return f"gpt:{{{str(uuid.UUID(bytes_le=disk_id)).upper()}}}"
    return None
