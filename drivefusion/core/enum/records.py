"""Binary layouts returned by the NTFS and directory-enumeration IOCTLs.

Everything here is a pure function over a buffer. That is deliberate: these
structures are the most error-prone part of M2 — a wrong offset silently yields
plausible-looking garbage — and pure parsers can be tested exhaustively against
synthetic buffers on any platform, without Windows and without a real volume.
The ctypes call sites that produce these buffers stay as thin as possible
(``journal.py``, ``dirinfo.py``).

Layouts are from the Win32 headers:

* ``USN_RECORD_V2`` / ``USN_RECORD_V3`` (WinIoCtl.h)
* ``FILE_FULL_DIR_INFO`` / ``FILE_ID_BOTH_DIR_INFO`` (WinBase.h)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

#: 100-nanosecond intervals between 1601-01-01 (FILETIME) and the Unix epoch.
FILETIME_EPOCH_DELTA = 116_444_736_000_000_000

FILE_ATTRIBUTE_DIRECTORY = 0x00000010

#: USN_RECORD_V2: fixed header before the variable-length name.
_V2_HEADER = struct.Struct("<IHHQQqqIIIIHH")
_V2_HEADER_SIZE = 60

#: USN_RECORD_V3 uses 128-bit file ids, so the name starts further in.
_V3_PREFIX = struct.Struct("<IHH")
_V3_TAIL = struct.Struct("<qqIIIIHH")
_V3_HEADER_SIZE = 76

#: FILE_FULL_DIR_INFO up to (not including) the name.
_FULL_DIR_INFO = struct.Struct("<IIqqqqqqIII")
_FULL_DIR_INFO_SIZE = 68

#: FILE_ID_BOTH_DIR_INFO: ShortNameLength is a byte at 68, ShortName runs
#: 70..93, then the 8-byte FileId is aligned to 96 and the name starts at 104.
_ID_BOTH_NAME_OFFSET = 104
_ID_BOTH_FILE_ID_OFFSET = 96


class RecordParseError(ValueError):
    """Raised when a buffer cannot be a valid record stream.

    Raised rather than skipped: a malformed buffer means the offsets are wrong,
    and continuing would fabricate entries. A scan that invents files is worse
    than a scan that stops.
    """


def filetime_to_ns(filetime: int) -> int:
    """Convert a Win32 FILETIME to nanoseconds since the Unix epoch.

    Zero and the sentinel -1 mean "not set"; both map to 0 rather than to a
    date in 1601, which would otherwise show up in reports as a real timestamp.
    """
    if filetime <= 0:
        return 0
    return (filetime - FILETIME_EPOCH_DELTA) * 100


@dataclass(frozen=True, slots=True)
class UsnRecord:
    """One entry from the change journal or a bulk MFT enumeration.

    Note what is *absent*: a USN record carries no size and no modification
    time. It identifies a file, its parent, and its name. Sizes come from a
    directory-info pass (``dirinfo.py``) — see docs/PLAN.md §6.3.
    """

    frn: int
    parent_frn: int
    name: str
    attributes: int
    usn: int
    timestamp_ns: int = 0
    reason: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)


@dataclass(frozen=True, slots=True)
class DirEntry:
    """One entry from a batch directory read, with the metadata a scan needs."""

    name: str
    attributes: int
    size: int
    alloc_size: int
    mtime_ns: int
    ctime_ns: int
    file_id: int | None = None
    #: Set only by the portable lister, where a per-entry stat can fail. A
    #: Win32 batch read delivers metadata with the name, so it cannot.
    stat_failed: bool = False

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)


def _decode_name(buffer: memoryview, start: int, length: int) -> str:
    if length < 0 or start + length > len(buffer):
        raise RecordParseError(
            f"name at {start} length {length} runs past the {len(buffer)}-byte buffer"
        )
    return bytes(buffer[start : start + length]).decode("utf-16-le", "surrogatepass")


def parse_usn_records(data: bytes | memoryview, *, offset: int = 0) -> Iterator[UsnRecord]:
    """Yield USN records from an IOCTL output buffer.

    ``offset`` skips the leading cursor that both producing IOCTLs prepend:
    ``FSCTL_ENUM_USN_DATA`` writes the next start FRN (8 bytes) and
    ``FSCTL_READ_USN_JOURNAL`` writes the next USN (8 bytes).

    Both v2 and v3 records are handled, since which one a volume produces
    depends on the journal's configured range and cannot be assumed.
    """
    view = memoryview(data)
    position = offset

    while position < len(view):
        remaining = len(view) - position
        if remaining < 8:
            break  # trailing slack, not a record

        record_length, major, minor = _V3_PREFIX.unpack_from(view, position)
        if record_length == 0:
            break
        if record_length < 8 or position + record_length > len(view):
            raise RecordParseError(
                f"record at {position} claims {record_length} bytes, "
                f"{remaining} remain"
            )

        if major == 2:
            fields = _V2_HEADER.unpack_from(view, position)
            (
                _length, _major, _minor, frn, parent_frn, usn, timestamp,
                reason, _source, _security, attributes, name_length,
                name_offset,
            ) = fields
            header_size = _V2_HEADER_SIZE
        elif major == 3:
            # 128-bit file ids; the low 64 bits are the FRN that
            # OpenFileById and the parent links use.
            frn = int.from_bytes(bytes(view[position + 8 : position + 24]), "little")
            parent_frn = int.from_bytes(
                bytes(view[position + 24 : position + 40]), "little"
            )
            (
                usn, timestamp, reason, _source, _security, attributes,
                name_length, name_offset,
            ) = _V3_TAIL.unpack_from(view, position + 40)
            header_size = _V3_HEADER_SIZE
        else:
            raise RecordParseError(
                f"unsupported USN record version {major}.{minor} at {position}"
            )

        if name_offset < header_size:
            raise RecordParseError(
                f"name offset {name_offset} overlaps the {header_size}-byte "
                f"header at {position}"
            )

        name = _decode_name(
            view[position : position + record_length], name_offset, name_length
        )
        yield UsnRecord(
            frn=frn,
            parent_frn=parent_frn,
            name=name,
            attributes=attributes,
            usn=usn,
            timestamp_ns=filetime_to_ns(timestamp),
            reason=reason,
        )
        position += record_length


def parse_full_dir_info(data: bytes | memoryview) -> Iterator[DirEntry]:
    """Yield entries from a ``FILE_FULL_DIR_INFO`` buffer.

    Used on exFAT, which has no dependable persistent file ids, so this variant
    omits them rather than reporting a value that does not survive a move.
    """
    yield from _parse_dir_info(data, name_offset=_FULL_DIR_INFO_SIZE, with_id=False)


def parse_id_both_dir_info(data: bytes | memoryview) -> Iterator[DirEntry]:
    """Yield entries from a ``FILE_ID_BOTH_DIR_INFO`` buffer (NTFS)."""
    yield from _parse_dir_info(data, name_offset=_ID_BOTH_NAME_OFFSET, with_id=True)


def _parse_dir_info(
    data: bytes | memoryview, *, name_offset: int, with_id: bool
) -> Iterator[DirEntry]:
    view = memoryview(data)
    position = 0

    while True:
        if position + _FULL_DIR_INFO_SIZE > len(view):
            raise RecordParseError(
                f"entry at {position} runs past the {len(view)}-byte buffer"
            )
        (
            next_offset, _index, creation, _access, write, _change,
            end_of_file, allocation, attributes, name_length, _ea,
        ) = _FULL_DIR_INFO.unpack_from(view, position)

        file_id = None
        if with_id:
            file_id = struct.unpack_from("<q", view, position + _ID_BOTH_FILE_ID_OFFSET)[0]
            if file_id <= 0:
                file_id = None

        name = _decode_name(view, position + name_offset, name_length)
        yield DirEntry(
            name=name,
            attributes=attributes,
            size=end_of_file,
            alloc_size=allocation,
            mtime_ns=filetime_to_ns(write),
            ctime_ns=filetime_to_ns(creation),
            file_id=file_id,
        )

        if next_offset == 0:
            return
        if next_offset < 0 or position + next_offset <= position:
            raise RecordParseError(
                f"non-advancing next-entry offset {next_offset} at {position}"
            )
        position += next_offset
