"""Choosing an enumeration strategy per volume, and describing the cost.

With a fleet split evenly between NTFS and exFAT (docs/PLAN.md §1), the choice
is not "fast path or fallback" — it is two genuinely different strategies, and
which one applies is a per-volume fact the user should be able to see.
"""

from __future__ import annotations

from datetime import datetime, timezone

from drivefusion.core.enum.journal import Decision, JournalCursor, Method, decide


def plan_enumeration(
    *,
    supports_usn: bool | None,
    elevated: bool,
    journal_id: int | None = None,
    next_usn: int | None = None,
    journal_state=None,
) -> Decision:
    """Decide how to enumerate a volume, with a reason the user can read."""
    return decide(
        JournalCursor(journal_id, next_usn),
        journal_state,
        supports_usn=bool(supports_usn),
        elevated=elevated,
    )


def rescan_cost(supports_usn: bool | None) -> str:
    """``delta`` when a rescan can be cheap, ``full-walk`` when it cannot."""
    return "delta" if supports_usn else "full-walk"


def describe_age(last_seen: str | None, *, now: datetime | None = None) -> str:
    """Human staleness for a volume's last scan.

    exFAT volumes cannot be cheaply confirmed current, so the interface shows
    how old the information is rather than implying freshness (§6.4).
    """
    if not last_seen:
        return "never scanned"

    now = now or datetime.now(timezone.utc)
    try:
        when = datetime.fromisoformat(last_seen)
    except ValueError:
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    seconds = (now - when).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)} hours ago"
    days = hours / 24
    if days < 60:
        return f"{int(days)} days ago"
    return f"{int(days / 30)} months ago"


def method_summary(decision: Decision, supports_usn: bool | None) -> str:
    """One line for the scan output explaining what is about to happen."""
    if decision.method is Method.USN_DELTA:
        return f"reading only what changed ({decision.reason})"
    if decision.method is Method.USN_FULL:
        return f"full journal enumeration ({decision.reason})"
    if supports_usn:
        return f"full directory walk ({decision.reason})"
    return f"full directory walk ({decision.reason})"
