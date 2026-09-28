"""Formatting shared by the CLI and the GUI.

One implementation so the two surfaces cannot disagree about what 1.5 TB is.
"""

from __future__ import annotations

from datetime import datetime, timezone

UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(value: int | None) -> str:
    """Byte counts at a glance. ``None`` is unknown, and says so."""
    if value is None:
        return "—"
    size = float(value)
    for unit in UNITS:
        if abs(size) < 1024 or unit == "PB":
            return f"{int(size):,} B" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} PB"


def human_count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def describe_age(timestamp: str | None) -> str:
    """How long ago an ISO timestamp was, in words.

    A drive last seen months ago is the most useful thing on the drives screen,
    so "never scanned" is never rendered as a blank.
    """
    if not timestamp:
        return "never"
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    seconds = (datetime.now(timezone.utc) - when).total_seconds()
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


def percent(part: int | None, whole: int | None) -> str:
    if not whole or part is None:
        return "—"
    return f"{100.0 * part / whole:.0f}%"
