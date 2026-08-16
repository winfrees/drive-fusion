"""Backend selection and how enumeration cost is described to the user."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drivefusion.core.enum.backend import (
    describe_age,
    method_summary,
    plan_enumeration,
    rescan_cost,
)
from drivefusion.core.enum.journal import JournalState, Method

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def test_exfat_always_walks() -> None:
    decision = plan_enumeration(supports_usn=False, elevated=True)
    assert decision.method is Method.WALK


def test_ntfs_with_a_valid_cursor_reads_a_delta() -> None:
    state = JournalState(
        journal_id=7, first_usn=0, next_usn=900, lowest_valid_usn=0, max_usn=1 << 40
    )
    decision = plan_enumeration(
        supports_usn=True, elevated=True, journal_id=7, next_usn=500,
        journal_state=state,
    )
    assert decision.is_delta


def test_ntfs_without_elevation_walks() -> None:
    decision = plan_enumeration(supports_usn=True, elevated=False)
    assert decision.method is Method.WALK
    assert "administrator" in decision.reason


@pytest.mark.parametrize(
    ("supports_usn", "expected"),
    [(True, "delta"), (False, "full-walk"), (None, "full-walk")],
)
def test_rescan_cost(supports_usn, expected: str) -> None:
    assert rescan_cost(supports_usn) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=30), "30 minutes ago"),
        (timedelta(hours=5), "5 hours ago"),
        (timedelta(days=34), "34 days ago"),
        (timedelta(days=400), "13 months ago"),
    ],
)
def test_describe_age(delta: timedelta, expected: str) -> None:
    when = (NOW - delta).isoformat(timespec="seconds")
    assert describe_age(when, now=NOW) == expected


def test_never_scanned_is_stated_plainly() -> None:
    """Silence would read as "current"; a volume never scanned must say so."""
    assert describe_age(None) == "never scanned"
    assert describe_age("") == "never scanned"


def test_unparseable_timestamp_does_not_crash() -> None:
    assert describe_age("not a date", now=NOW) == "unknown"


def test_naive_timestamps_are_treated_as_utc() -> None:
    naive = (NOW - timedelta(hours=5)).replace(tzinfo=None).isoformat()
    assert describe_age(naive, now=NOW) == "5 hours ago"


def test_method_summary_explains_itself() -> None:
    delta = plan_enumeration(
        supports_usn=True, elevated=True, journal_id=1, next_usn=5,
        journal_state=JournalState(1, 0, 10, 0, 1 << 40),
    )
    assert "only what changed" in method_summary(delta, True)

    walk = plan_enumeration(supports_usn=False, elevated=True)
    assert "full directory walk" in method_summary(walk, False)
