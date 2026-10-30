"""What the user knows about a drive that the hardware doesn't say (PLAN §7.3, §8.2).

Purchase date, price, warranty, role, location and disaster zone are the
``p`` and ``l`` of PLAN §2: age drives the failure rate, and copies bought
together or kept together fail together. Everything here is validated before
it reaches the catalog, because a mistyped date would quietly become a wrong
failure probability in M5.4.

The **batch key** groups drives likely to share a manufacturing batch:
manufacturer + model + purchase month. It is derived, not stored, unless the
user overrides it, which matters for disks behind USB bridges that report the
bridge's model rather than the disk's.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

ROLES = ("working", "replica", "archive", "offsite")


class MetadataError(ValueError):
    """A value that can't be stored as given; the message says what would be."""


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise MetadataError("empty; use --clear to remove a value")
    if len(value) > 500:
        raise MetadataError("longer than 500 characters")
    return value


def _date(value: str) -> str:
    """An ISO date; YYYY-MM is accepted where only the month is known."""
    value = value.strip()
    for fmt, length in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        if len(value) == length:
            try:
                parsed = dt.datetime.strptime(value, fmt).date()
            except ValueError:
                break
            if parsed > dt.date.today() + dt.timedelta(days=366 * 10):
                raise MetadataError(f"{value} is too far in the future")
            return value
    raise MetadataError(f"{value!r} is not a date; use YYYY-MM-DD (or YYYY-MM)")


def _price_cents(value: str) -> int:
    cleaned = re.sub(r"[\s,$€£]", "", value)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise MetadataError(f"{value!r} is not a price; use e.g. 129.99") from None
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        raise MetadataError(f"{value!r}: a price is zero or more, with at most two decimals")
    return int(amount * 100)


def _role(value: str) -> str:
    value = value.strip().lower()
    if value not in ROLES:
        raise MetadataError(f"{value!r} is not a role; choose one of {', '.join(ROLES)}")
    return value


@dataclass(frozen=True)
class Field:
    name: str            # the user-facing name (CLI flag without dashes)
    column: str          # the drive table column
    label: str
    parse: Callable[[str], object]


FIELDS: tuple[Field, ...] = (
    Field("nickname", "nickname", "Nickname", _text),
    Field("purchase-date", "purchase_date", "Purchased", _date),
    Field("price", "purchase_price_cents", "Price", _price_cents),
    Field("vendor", "vendor", "Vendor", _text),
    Field("warranty-end", "warranty_end", "Warranty ends", _date),
    Field("role", "role", "Role", _role),
    Field("location", "location", "Location", _text),
    Field("zone", "disaster_zone", "Disaster zone", _text),
    Field("retired", "retired_at", "Retired", _date),
    Field("batch-key", "batch_key", "Batch (override)", _text),
    Field("notes", "notes", "Notes", _text),
)
BY_NAME = {f.name: f for f in FIELDS}


def parse_changes(values: dict[str, str | None]) -> dict[str, object]:
    """User-facing names and raw strings to columns and stored values.

    ``None`` clears a field. Raises :class:`MetadataError` naming the field.
    """
    out: dict[str, object] = {}
    for name, raw in values.items():
        field = BY_NAME.get(name)
        if field is None:
            raise MetadataError(f"{name!r} is not something that can be edited")
        if raw is None:
            out[field.column] = None
            continue
        try:
            out[field.column] = field.parse(raw)
        except MetadataError as exc:
            raise MetadataError(f"{field.label}: {exc}") from None
    return out


def _normalise(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(text.split()).casefold() or None


def derived_batch_key(manufacturer: str | None, model: str | None,
                      purchase_date: str | None) -> str | None:
    """Manufacturer + model + purchase month, or None until all three are known."""
    maker, kind = _normalise(manufacturer), _normalise(model)
    if not (maker and kind and purchase_date and len(purchase_date) >= 7):
        return None
    return f"{maker}|{kind}|{purchase_date[:7]}"


def batch_key(row) -> tuple[str | None, str]:
    """The batch key in force for a drive row, and where it came from:
    ``override``, ``derived``, or ``unknown``."""
    if row["batch_key"]:
        return row["batch_key"], "override"
    key = derived_batch_key(row["manufacturer"], row["model"], row["purchase_date"])
    return key, ("derived" if key else "unknown")


def format_value(column: str, value) -> str:
    if value is None:
        return "—"
    if column == "purchase_price_cents":
        return f"{Decimal(int(value)) / 100:.2f}"
    return str(value)
