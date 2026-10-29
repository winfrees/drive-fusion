"""Drive metadata (ROADMAP M5.2): validated, audited, and grouped into batches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.health import metadata
from drivefusion.core.health.metadata import MetadataError, derived_batch_key, parse_changes
from drivefusion.core.store import audit, schema
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "catalog.db") as cat:
        yield cat


# -- parsing ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "raw", "column", "stored"),
    [
        ("purchase-date", "2021-03-14", "purchase_date", "2021-03-14"),
        ("purchase-date", "2021-03", "purchase_date", "2021-03"),     # month known only
        ("price", "129.99", "purchase_price_cents", 12999),
        ("price", "$1,299.00", "purchase_price_cents", 129900),
        ("role", "Archive", "role", "archive"),
        ("zone", "  home office ", "disaster_zone", "home office"),
        ("warranty-end", "2024-03-14", "warranty_end", "2024-03-14"),
    ],
)
def test_values_are_parsed_to_what_is_stored(name, raw, column, stored) -> None:
    assert parse_changes({name: raw}) == {column: stored}


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("purchase-date", "14/03/2021", "not a date"),
        ("purchase-date", "2021-02-30", "not a date"),
        ("purchase-date", "2199-01-01", "too far in the future"),
        ("price", "cheap", "not a price"),
        ("price", "-5", "zero or more"),
        ("price", "1.999", "at most two decimals"),
        ("role", "backup", "not a role"),
        ("nickname", "   ", "empty"),
        ("serial", "X", "not something that can be edited"),
    ],
)
def test_bad_values_are_refused_with_a_reason(name, raw, message) -> None:
    with pytest.raises(MetadataError, match=message):
        parse_changes({name: raw})


def test_none_clears_a_field() -> None:
    assert parse_changes({"location": None}) == {"location": None}


# -- the batch key ---------------------------------------------------------------

def test_the_batch_key_is_maker_model_and_purchase_month() -> None:
    a = derived_batch_key("Seagate", "ST4000DM004", "2021-03-14")
    b = derived_batch_key("  seagate ", "st4000dm004", "2021-03")
    assert a == b == "seagate|st4000dm004|2021-03"
    assert derived_batch_key("Seagate", "ST4000DM004", "2021-04-01") != a
    assert derived_batch_key("Seagate", None, "2021-03") is None
    assert derived_batch_key("Seagate", "ST4000DM004", None) is None


def test_an_override_wins_and_says_so(catalog: Catalog) -> None:
    drive = catalog.upsert_drive(serial="S-1", manufacturer="JMicron", model="USB to SATA")
    catalog.update_drive(drive, {"purchase-date": "2020-06-01"})
    assert metadata.batch_key(catalog.drive(drive)) == ("jmicron|usb to sata|2020-06", "derived")
    catalog.update_drive(drive, {"batch-key": "wd-red-2020-06-order-1183"})
    assert metadata.batch_key(catalog.drive(drive)) == ("wd-red-2020-06-order-1183", "override")
    other = catalog.upsert_drive(serial="S-2")
    assert metadata.batch_key(catalog.drive(other)) == (None, "unknown")


# -- the catalog -----------------------------------------------------------------

def test_an_edit_is_validated_whole_then_applied_and_audited(catalog: Catalog) -> None:
    drive = catalog.upsert_drive(serial="S-1")
    with pytest.raises(MetadataError):
        catalog.update_drive(drive, {"location": "Shelf B", "price": "cheap"})
    assert catalog.drive(drive)["location"] is None, "nothing is written if anything is bad"

    applied = catalog.update_drive(drive, {"location": "Shelf B", "role": "archive"})
    assert applied == {"location": "Shelf B", "role": "archive"}
    entry = audit.entries(catalog.conn, limit=1)[0]
    assert entry["action"] == "drive.edit"
    assert json.loads(entry["detail_json"])["changes"] == {
        "location": [None, "Shelf B"], "role": [None, "archive"]}

    assert catalog.update_drive(drive, {"location": "Shelf B"}) == {}, "no change, no entry"
    assert audit.entries(catalog.conn, limit=1)[0]["id"] == entry["id"]

    catalog.update_drive(drive, {"location": None})
    assert catalog.drive(drive)["location"] is None


def test_an_unknown_drive_is_refused(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="no drive 99"):
        catalog.update_drive(99, {"location": "x"})


def test_schema_v7_has_drive_health(catalog: Catalog) -> None:
    assert schema.current_version(catalog.conn) >= 7
    columns = {r["name"] for r in catalog.conn.execute("PRAGMA table_info(drive_health)")}
    assert {"drive_id", "observed_at", "reallocated", "pending", "confidence",
            "annual_failure_prob", "smart_json"} <= columns
