"""Parsers for the Win32 enumeration structures.

These run everywhere, which is the point: the binary layouts are the most
error-prone part of the NTFS work and a wrong offset produces plausible
garbage rather than an obvious failure. Buffers are built independently of the
parsers, from the documented offsets.
"""

from __future__ import annotations

import pytest

from drivefusion.core.enum import records
from drivefusion.core.enum.records import RecordParseError
from tests.fixtures import winbuf

DIRECTORY = winbuf.FILE_ATTRIBUTE_DIRECTORY


# -- USN records --------------------------------------------------------------

@pytest.mark.parametrize("builder", [winbuf.usn_record_v2, winbuf.usn_record_v3])
def test_parses_a_single_record(builder) -> None:
    buffer = winbuf.usn_buffer(
        [builder(frn=42, parent_frn=7, name="report.txt", attributes=0x20, usn=999)]
    )
    parsed = list(records.parse_usn_records(buffer, offset=8))

    assert len(parsed) == 1
    record = parsed[0]
    assert record.frn == 42
    assert record.parent_frn == 7
    assert record.name == "report.txt"
    assert record.attributes == 0x20
    assert record.usn == 999
    assert not record.is_dir


@pytest.mark.parametrize("builder", [winbuf.usn_record_v2, winbuf.usn_record_v3])
def test_parses_a_mixed_stream(builder) -> None:
    buffer = winbuf.usn_buffer(
        [
            builder(frn=1, parent_frn=5, name="Projects", attributes=DIRECTORY),
            builder(frn=2, parent_frn=1, name="a.txt"),
            builder(frn=3, parent_frn=1, name="ünïcode — файл.psd"),
        ]
    )
    parsed = list(records.parse_usn_records(buffer, offset=8))

    assert [r.name for r in parsed] == ["Projects", "a.txt", "ünïcode — файл.psd"]
    assert [r.is_dir for r in parsed] == [True, False, False]
    assert [r.frn for r in parsed] == [1, 2, 3]


def test_v2_and_v3_records_agree():
    """The two versions differ only in id width; parsed output must match."""
    kwargs = dict(frn=99, parent_frn=5, name="same.bin", attributes=0x80, usn=1234)
    v2 = list(records.parse_usn_records(winbuf.usn_buffer([winbuf.usn_record_v2(**kwargs)]), offset=8))
    v3 = list(records.parse_usn_records(winbuf.usn_buffer([winbuf.usn_record_v3(**kwargs)]), offset=8))
    assert v2 == v3


def test_v3_preserves_a_128_bit_id_low_word() -> None:
    """FILE_ID_128 is wider than the FRN the parent links use."""
    wide = (1 << 100) | 0xDEADBEEF
    buffer = winbuf.usn_buffer(
        [winbuf.usn_record_v3(frn=wide, parent_frn=wide, name="x")]
    )
    record = next(records.parse_usn_records(buffer, offset=8))
    assert record.frn == wide


def test_timestamps_convert_from_filetime() -> None:
    when = 1_700_000_000_000_000_000
    buffer = winbuf.usn_buffer(
        [winbuf.usn_record_v2(frn=1, parent_frn=0, name="t", timestamp_ns=when)]
    )
    record = next(records.parse_usn_records(buffer, offset=8))
    # FILETIME has 100 ns resolution, so equality is to that precision.
    assert abs(record.timestamp_ns - when) < 100


def test_unset_timestamps_are_zero_not_1601() -> None:
    """A zero FILETIME must not surface as a date in 1601."""
    assert records.filetime_to_ns(0) == 0
    assert records.filetime_to_ns(-1) == 0


def test_empty_buffer_yields_nothing() -> None:
    assert list(records.parse_usn_records(winbuf.usn_buffer([]), offset=8)) == []


def test_zero_record_length_terminates() -> None:
    buffer = winbuf.usn_buffer(
        [winbuf.usn_record_v2(frn=1, parent_frn=0, name="a")]
    ) + b"\x00" * 16
    assert len(list(records.parse_usn_records(buffer, offset=8))) == 1


def test_truncated_record_is_an_error_not_a_guess() -> None:
    """Fabricating entries from a bad buffer is worse than failing loudly."""
    good = winbuf.usn_buffer([winbuf.usn_record_v2(frn=1, parent_frn=0, name="a.txt")])
    with pytest.raises(RecordParseError, match="claims"):
        list(records.parse_usn_records(good[:-8], offset=8))


def test_unknown_version_is_rejected() -> None:
    buffer = bytearray(winbuf.usn_buffer([winbuf.usn_record_v2(frn=1, parent_frn=0, name="a")]))
    buffer[8 + 4] = 9  # MajorVersion
    with pytest.raises(RecordParseError, match="unsupported USN record version"):
        list(records.parse_usn_records(bytes(buffer), offset=8))


def test_name_overlapping_the_header_is_rejected() -> None:
    buffer = bytearray(winbuf.usn_buffer([winbuf.usn_record_v2(frn=1, parent_frn=0, name="a")]))
    buffer[8 + 58] = 8  # NameOffset inside the header
    with pytest.raises(RecordParseError, match="overlaps"):
        list(records.parse_usn_records(bytes(buffer), offset=8))


# -- directory info -----------------------------------------------------------

def test_parses_full_dir_info() -> None:
    buffer = winbuf.full_dir_info(
        [
            {"name": "photos", "attributes": DIRECTORY},
            {"name": "a.raw", "size": 1234, "alloc_size": 131072,
             "mtime_ns": 1_700_000_000_000_000_000},
        ]
    )
    parsed = list(records.parse_full_dir_info(buffer))

    assert [e.name for e in parsed] == ["photos", "a.raw"]
    assert parsed[0].is_dir and not parsed[1].is_dir
    assert parsed[1].size == 1234
    # Cluster slack: allocation far exceeds logical size on a 128 KiB volume.
    assert parsed[1].alloc_size == 131072
    assert parsed[0].file_id is None


def test_parses_id_both_dir_info_with_file_ids() -> None:
    buffer = winbuf.id_both_dir_info(
        [
            {"name": "docs", "attributes": DIRECTORY, "file_id": 1122},
            {"name": "b.txt", "size": 10, "file_id": 3344},
        ]
    )
    parsed = list(records.parse_id_both_dir_info(buffer))

    assert [e.file_id for e in parsed] == [1122, 3344]
    assert [e.name for e in parsed] == ["docs", "b.txt"]


def test_absent_file_id_is_none_not_zero() -> None:
    """exFAT reports no usable id; zero must not be mistaken for one."""
    buffer = winbuf.id_both_dir_info([{"name": "x", "file_id": 0}])
    assert next(records.parse_id_both_dir_info(buffer)).file_id is None


def test_long_and_unicode_names_survive() -> None:
    long_name = "深" * 120 + ".txt"
    buffer = winbuf.full_dir_info([{"name": long_name}])
    assert next(records.parse_full_dir_info(buffer)).name == long_name


def test_non_advancing_offset_is_rejected() -> None:
    """A zero-advance chain would otherwise loop forever."""
    import struct

    buffer = bytearray(
        winbuf.full_dir_info([{"name": "a"}, {"name": "b"}])
    )
    struct.pack_into("<I", buffer, 0, 0xFFFFFFF0)  # huge, wraps past the buffer
    with pytest.raises(RecordParseError):
        list(records.parse_full_dir_info(bytes(buffer)))


def test_single_entry_terminates() -> None:
    buffer = winbuf.full_dir_info([{"name": "only"}])
    assert [e.name for e in records.parse_full_dir_info(buffer)] == ["only"]
