"""Journal state parsing and the enumeration-strategy decision.

The decision function is the safety-critical part of M2: trusting a stale
cursor makes the catalog confidently wrong rather than obviously incomplete.
Every branch is asserted here, on any platform.
"""

from __future__ import annotations

import struct

import pytest

from drivefusion.core.enum.journal import (
    Decision,
    JournalCursor,
    JournalState,
    Method,
    build_enum_input,
    build_read_journal_input,
    decide,
    parse_journal_data,
)


def journal_data(
    journal_id=0xABCD, first=100, next_usn=5000, lowest=100, max_usn=1 << 40
) -> bytes:
    return struct.pack(
        "<Qqqqqqq", journal_id, first, next_usn, lowest, max_usn, 1 << 25, 1 << 20
    )


def state(**overrides) -> JournalState:
    defaults = dict(
        journal_id=0xABCD, first_usn=100, next_usn=5000,
        lowest_valid_usn=100, max_usn=1 << 40,
    )
    defaults.update(overrides)
    return JournalState(**defaults)


# -- parsing ------------------------------------------------------------------

def test_parses_journal_data() -> None:
    parsed = parse_journal_data(journal_data())
    assert parsed.journal_id == 0xABCD
    assert parsed.first_usn == 100
    assert parsed.next_usn == 5000
    assert parsed.lowest_valid_usn == 100


def test_short_buffer_is_rejected() -> None:
    with pytest.raises(ValueError, match="expected at least"):
        parse_journal_data(journal_data()[:20])


def test_read_journal_input_layout() -> None:
    cursor = JournalCursor(journal_id=0xABCD, next_usn=4242)
    payload = build_read_journal_input(cursor)

    start_usn, reason, only_on_close, timeout, wait_for, journal_id = struct.unpack(
        "<qIIQQQ", payload
    )
    assert start_usn == 4242
    assert journal_id == 0xABCD
    assert reason == 0xFFFFFFFF          # every change kind
    assert timeout == 0 and wait_for == 0  # must not block waiting for changes
    assert only_on_close == 0


def test_read_journal_input_requires_a_cursor() -> None:
    with pytest.raises(ValueError, match="without a stored cursor"):
        build_read_journal_input(JournalCursor(None, None))


def test_enum_input_layout() -> None:
    start_frn, low, high = struct.unpack("<Qqq", build_enum_input(0, 5000))
    assert (start_frn, low, high) == (0, 0, 5000)


# -- the decision -------------------------------------------------------------

def test_delta_when_the_cursor_still_matches() -> None:
    result = decide(
        JournalCursor(0xABCD, 4000), state(), supports_usn=True, elevated=True
    )
    assert result.method is Method.USN_DELTA
    assert result.is_delta


def test_delta_when_nothing_has_happened() -> None:
    result = decide(
        JournalCursor(0xABCD, 5000), state(next_usn=5000),
        supports_usn=True, elevated=True,
    )
    assert result.method is Method.USN_DELTA
    assert "no journal activity" in result.reason


def test_full_on_first_scan() -> None:
    result = decide(
        JournalCursor(None, None), state(), supports_usn=True, elevated=True
    )
    assert result.method is Method.USN_FULL
    assert "first scan" in result.reason


def test_full_when_the_journal_was_recreated() -> None:
    """A new journal id means the stored cursor indexes a journal that is gone."""
    result = decide(
        JournalCursor(0x1111, 4000), state(journal_id=0x2222),
        supports_usn=True, elevated=True,
    )
    assert result.method is Method.USN_FULL
    assert "deleted and recreated" in result.reason


def test_full_when_records_were_purged() -> None:
    """The cursor fell off the end: those changes are unrecoverable."""
    result = decide(
        JournalCursor(0xABCD, 50), state(lowest_valid_usn=1000),
        supports_usn=True, elevated=True,
    )
    assert result.method is Method.USN_FULL
    assert "purged" in result.reason


def test_full_when_the_volume_went_backwards() -> None:
    """Restoring a volume from an image rewinds the journal behind the cursor."""
    result = decide(
        JournalCursor(0xABCD, 9000), state(next_usn=5000),
        supports_usn=True, elevated=True,
    )
    assert result.method is Method.USN_FULL
    assert "restored from an image" in result.reason


def test_full_when_the_journal_cannot_be_queried() -> None:
    result = decide(
        JournalCursor(0xABCD, 4000), None, supports_usn=True, elevated=True
    )
    assert result.method is Method.USN_FULL


def test_walk_without_journal_support() -> None:
    """exFAT: half the fleet, and no journal to read."""
    result = decide(
        JournalCursor(None, None), None, supports_usn=False, elevated=True
    )
    assert result.method is Method.WALK
    assert "full re-walk" in result.reason


def test_walk_without_elevation() -> None:
    result = decide(
        JournalCursor(0xABCD, 4000), state(), supports_usn=True, elevated=False
    )
    assert result.method is Method.WALK
    assert "administrator" in result.reason


@pytest.mark.parametrize(
    ("cursor", "journal"),
    [
        (JournalCursor(None, None), state()),
        (JournalCursor(0x1111, 4000), state(journal_id=0x2222)),
        (JournalCursor(0xABCD, 50), state(lowest_valid_usn=1000)),
        (JournalCursor(0xABCD, 9000), state(next_usn=5000)),
        (JournalCursor(0xABCD, 4000), None),
    ],
)
def test_every_doubtful_case_falls_back_to_a_full_pass(cursor, journal) -> None:
    """The invariant: never claim a delta unless the cursor is provably valid."""
    result = decide(cursor, journal, supports_usn=True, elevated=True)
    assert result.method is Method.USN_FULL, result.reason
    assert result.reason  # a user-facing explanation is always given


def test_every_decision_carries_a_reason() -> None:
    for cursor, journal, usn, elevated in (
        (JournalCursor(0xABCD, 4000), state(), True, True),
        (JournalCursor(None, None), None, False, True),
        (JournalCursor(0xABCD, 4000), state(), True, False),
    ):
        result = decide(cursor, journal, supports_usn=usn, elevated=elevated)
        assert isinstance(result, Decision)
        assert len(result.reason) > 10
