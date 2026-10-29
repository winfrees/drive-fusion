"""A disk's identity from its partition table, for drives with no serial (F-05)."""

from __future__ import annotations

import struct
import uuid

from drivefusion.core.discovery.layout import parse_drive_layout


def _layout(style: int, union: bytes, partitions: int = 1) -> bytes:
    # DRIVE_LAYOUT_INFORMATION_EX: PartitionStyle, PartitionCount, union, entries.
    return struct.pack("<II", style, partitions) + union + b"\0" * (144 * partitions)


def test_an_mbr_disk_is_named_by_its_signature() -> None:
    raw = _layout(0, struct.pack("<II", 0x1A2B3C4D, 0) + b"\0" * 32)
    assert parse_drive_layout(raw) == "mbr:1A2B3C4D"


def test_a_gpt_disk_is_named_by_its_disk_guid() -> None:
    disk_id = uuid.UUID("6f1c2a3b-4d5e-4f60-8a1b-2c3d4e5f6071")
    union = disk_id.bytes_le + struct.pack("<qqI", 34 * 512, 1 << 30, 128) + b"\0" * 4
    assert parse_drive_layout(_layout(1, union)) == "gpt:{6F1C2A3B-4D5E-4F60-8A1B-2C3D4E5F6071}"


def test_a_disk_without_an_identity_gets_none() -> None:
    """None means "not identified"; a made-up id would merge unrelated disks."""
    assert parse_drive_layout(_layout(0, b"\0" * 40)) is None          # unsigned MBR
    assert parse_drive_layout(_layout(1, b"\0" * 40)) is None          # zero GPT id
    assert parse_drive_layout(_layout(2, b"\x01" * 40)) is None        # RAW
    assert parse_drive_layout(b"\0\0\0") is None                       # truncated


def test_two_disks_get_two_identities() -> None:
    a = _layout(0, struct.pack("<II", 1, 0) + b"\0" * 32)
    b = _layout(0, struct.pack("<II", 2, 0) + b"\0" * 32)
    assert parse_drive_layout(a) != parse_drive_layout(b)
