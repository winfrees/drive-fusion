"""The NTFS change journal: state, validity, and record streaming.

The journal is what turns a rescan of an unchanged 10M-file volume from a
half-hour re-walk into a few seconds (docs/PLAN.md §6.3). It is also the part
where being wrong is dangerous rather than slow: if a cursor is stale and the
tool trusts it anyway, the scan reports "nothing changed" for files that did
change, and every copy count downstream is quietly wrong.

So validity is decided by a pure function with an explicit reason, and the
default on any doubt is a full enumeration. Correctness never depends on the
journal being intact.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

#: USN_JOURNAL_DATA_V0 (WinIoCtl.h).
_JOURNAL_DATA = struct.Struct("<Qqqqqqq")
_JOURNAL_DATA_SIZE = 56

#: READ_USN_JOURNAL_DATA_V0.
_READ_JOURNAL_INPUT = struct.Struct("<qIIQQQ")

#: MFT_ENUM_DATA_V0.
_MFT_ENUM_INPUT = struct.Struct("<Qqq")

# Win32 error codes that mean "this journal cannot be used as you asked".
ERROR_INVALID_FUNCTION = 1
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181

JOURNAL_UNUSABLE_ERRORS = frozenset(
    {
        ERROR_INVALID_FUNCTION,
        ERROR_JOURNAL_DELETE_IN_PROGRESS,
        ERROR_JOURNAL_NOT_ACTIVE,
        ERROR_JOURNAL_ENTRY_DELETED,
    }
)

#: Every reason bit; a rescan wants all change kinds, not a subset.
USN_REASON_MASK = 0xFFFFFFFF


class Method(str, Enum):
    """How a volume will be enumerated."""

    USN_DELTA = "usn-delta"
    USN_FULL = "usn-full"
    WALK = "walk"


@dataclass(frozen=True, slots=True)
class JournalState:
    """The volume's current journal, as reported by FSCTL_QUERY_USN_JOURNAL."""

    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int
    maximum_size: int = 0
    allocation_delta: int = 0


@dataclass(frozen=True, slots=True)
class JournalCursor:
    """What the catalog remembers about a volume's journal from last time."""

    journal_id: int | None
    next_usn: int | None

    @property
    def is_set(self) -> bool:
        return self.journal_id is not None and self.next_usn is not None


@dataclass(frozen=True, slots=True)
class Decision:
    """How to enumerate, and why — the reason is shown to the user."""

    method: Method
    reason: str

    @property
    def is_delta(self) -> bool:
        return self.method is Method.USN_DELTA


def parse_journal_data(data: bytes) -> JournalState:
    """Parse a USN_JOURNAL_DATA_V0 buffer."""
    if len(data) < _JOURNAL_DATA_SIZE:
        raise ValueError(
            f"journal data is {len(data)} bytes, expected at least "
            f"{_JOURNAL_DATA_SIZE}"
        )
    (
        journal_id, first_usn, next_usn, lowest_valid_usn, max_usn,
        maximum_size, allocation_delta,
    ) = _JOURNAL_DATA.unpack_from(data, 0)
    return JournalState(
        journal_id=journal_id,
        first_usn=first_usn,
        next_usn=next_usn,
        lowest_valid_usn=lowest_valid_usn,
        max_usn=max_usn,
        maximum_size=maximum_size,
        allocation_delta=allocation_delta,
    )


def build_read_journal_input(
    cursor: JournalCursor, *, reason_mask: int = USN_REASON_MASK
) -> bytes:
    """READ_USN_JOURNAL_DATA_V0 for a delta read."""
    if not cursor.is_set:
        raise ValueError("cannot read a delta without a stored cursor")
    return _READ_JOURNAL_INPUT.pack(
        cursor.next_usn,   # StartUsn
        reason_mask,       # ReasonMask
        0,                 # ReturnOnlyOnClose
        0,                 # Timeout: do not block waiting for new records
        0,                 # BytesToWaitFor: return immediately
        cursor.journal_id,
    )


def build_enum_input(start_frn: int, high_usn: int, *, low_usn: int = 0) -> bytes:
    """MFT_ENUM_DATA_V0 for a bulk enumeration pass."""
    return _MFT_ENUM_INPUT.pack(start_frn, low_usn, high_usn)


def decide(
    cursor: JournalCursor,
    state: JournalState | None,
    *,
    supports_usn: bool,
    elevated: bool,
) -> Decision:
    """Choose an enumeration method, erring toward a full pass.

    Every path that cannot *prove* the stored cursor still describes this
    journal falls back to a full enumeration. Under-reporting changes is the
    one failure this module must not produce: it would leave the catalog
    confidently stale rather than obviously incomplete.
    """
    if not supports_usn:
        return Decision(
            Method.WALK,
            "filesystem has no change journal, so every rescan is a full re-walk",
        )
    if not elevated:
        return Decision(
            Method.WALK,
            "reading the change journal needs administrator rights; "
            "run a fast scan to use it",
        )
    if state is None:
        return Decision(
            Method.USN_FULL, "the volume's journal could not be queried"
        )
    if not cursor.is_set:
        return Decision(
            Method.USN_FULL, "first scan of this volume, so there is no cursor"
        )
    if cursor.journal_id != state.journal_id:
        return Decision(
            Method.USN_FULL,
            "the journal was deleted and recreated since the last scan, so the "
            "stored cursor refers to a journal that no longer exists",
        )
    if cursor.next_usn < state.lowest_valid_usn:
        return Decision(
            Method.USN_FULL,
            "records since the last scan have been purged from the journal, so "
            "the changes they described cannot be recovered",
        )
    if cursor.next_usn > state.next_usn:
        return Decision(
            Method.USN_FULL,
            "the journal is older than the stored cursor, which happens when a "
            "volume is restored from an image",
        )
    if cursor.next_usn == state.next_usn:
        return Decision(
            Method.USN_DELTA, "no journal activity since the last scan"
        )
    return Decision(Method.USN_DELTA, "reading changes since the last scan")


# -- Windows shims -----------------------------------------------------------
# Deliberately thin. Everything above is pure and tested on any platform; what
# follows is syscall plumbing that only Windows can exercise.

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_ENUM_USN_DATA = 0x000900B3

#: One IOCTL round trip per buffer, so a bigger buffer is fewer transitions.
ENUM_BUFFER_BYTES = 1 << 20


def query_journal(volume_device_path: str) -> JournalState | None:
    """Query a volume's journal. Returns None when it has none to offer."""
    from drivefusion.core.enum import winio

    data = winio.device_ioctl(
        volume_device_path, FSCTL_QUERY_USN_JOURNAL, b"", _JOURNAL_DATA_SIZE
    )
    return None if data is None else parse_journal_data(data)
