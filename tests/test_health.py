"""Health, risk, and the numbers a user would act on.

This module produces probabilities, and a probability is the easiest kind of
output to get wrong invisibly: nothing raises, the report just reads plausibly
and points at the wrong drive. So the assertions here are mostly about *ordering
and evidence* rather than exact values — that a pending sector outranks an old
drive, that missing data never reads as good news, that two copies in one
cupboard are worth less than two in two buildings.

The read-only assertions matter just as much. ``smartctl -t long`` starts a
multi-hour self-test on a user's drive, and nothing else in this codebase would
stop it: it is not a filesystem write, so the gateway, the lint and the no-touch
test all miss it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drivefusion.core.health import afr as afr_module
from drivefusion.core.health import risk as risk_module
from drivefusion.core.health import smart as smart_module
from drivefusion.core.health.risk import ESTIMATED, MEASURED, UNKNOWN, drive_risk
from drivefusion.core.health.smart import SmartError, SmartReading
from drivefusion.core.store.catalog import Catalog

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


@pytest.fixture
def table():
    return afr_module.load()


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def years_ago(years: float) -> str:
    return (NOW - timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")


# -- smartctl must never be able to change a drive ----------------------------

@pytest.mark.parametrize(
    "argument",
    ["-t", "--test", "-X", "-s", "--smart", "-o", "-S", "--set", "--set=foo"],
)
def test_smartctl_refuses_arguments_that_change_the_drive(argument: str) -> None:
    """The one command in this tool that could alter hardware, fenced off.

    smartctl -t starts a self-test that runs for hours. That is not a filesystem
    write, so none of the other three read-only mechanisms would catch it.
    """
    with pytest.raises(SmartError, match="refusing to run"):
        smart_module.check_arguments(["--json", argument])


def test_smartctl_refuses_anything_not_on_the_allowlist() -> None:
    with pytest.raises(SmartError, match="allowlist"):
        smart_module.check_arguments(["--json", "--some-new-flag"])


def test_the_default_arguments_are_permitted() -> None:
    """A guard that blocked normal use would be removed within a week."""
    smart_module.check_arguments(["--json", "-a", "-n", "standby"])


def test_run_checks_arguments_before_spawning_anything(monkeypatch) -> None:
    """The refusal must come before the process, not after it."""
    spawned = []

    def runner(argv, timeout):
        spawned.append(argv)
        raise AssertionError("should never be reached")

    monkeypatch.setattr(smart_module, "smartctl_path", lambda: "/usr/sbin/smartctl")
    with pytest.raises(SmartError):
        smart_module.run("/dev/sda", arguments=["--json", "-t", "long"], runner=runner)
    assert spawned == []


# -- parsing real-shaped smartctl output --------------------------------------

SATA_DOCUMENT = {
    "model_name": "ST8000DM004-2CX188",
    "serial_number": "ZCT0ABCD",
    "firmware_version": "0001",
    "rotation_rate": 5425,
    "user_capacity": {"bytes": 8001563222016},
    "smart_status": {"passed": True},
    "power_on_time": {"hours": 31000},
    "temperature": {"current": 38},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 8}},
            {"id": 9, "name": "Power_On_Hours", "raw": {"value": 31000}},
            {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 2}},
            {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 0}},
            {"id": 187, "name": "Reported_Uncorrect", "raw": {"value": 1}},
        ]
    },
    "ata_smart_data": {"self_test": {"status": {"string": "completed without error"}}},
}

NVME_DOCUMENT = {
    "model_name": "Samsung SSD 990 PRO 2TB",
    "serial_number": "S6Z1NJ0T",
    "device": {"type": "nvme", "protocol": "NVMe"},
    "user_capacity": {"bytes": 2000398934016},
    "smart_status": {"passed": True},
    "nvme_smart_health_information_log": {
        "power_on_hours": 4200,
        "temperature": 41,
        "percentage_used": 7,
        "media_errors": 0,
        "data_units_written": 41_000_000,
    },
}

#: What a cheap USB bridge actually returns: identity and nothing else.
BRIDGE_DOCUMENT = {
    "model_name": "USB 3.0 Device",
    "device": {"type": "scsi", "protocol": "SCSI"},
}


def test_parses_a_sata_drive() -> None:
    reading = smart_module.parse(SATA_DOCUMENT)
    assert reading.media == "hdd"
    assert reading.power_on_hours == 31000
    assert reading.reallocated == 8
    assert reading.pending == 2
    assert reading.uncorrectable == 1
    assert reading.crc_errors == 0
    assert reading.passed is True
    assert reading.has_counters


def test_parses_an_nvme_drive() -> None:
    reading = smart_module.parse(NVME_DOCUMENT)
    assert reading.media == "nvme"
    assert reading.power_on_hours == 4200
    assert reading.wear_leveling == 7          # percent of endurance consumed
    assert reading.uncorrectable == 0
    assert reading.tbw_written == 41_000_000 * 1000 * 512


def test_a_bridge_that_reports_nothing_yields_no_counters() -> None:
    """The common external-drive case, and the one most likely to mislead.

    Every counter must be None, not zero: zero would be a claim that the drive
    has no reallocated sectors, which nobody has established.
    """
    reading = smart_module.parse(BRIDGE_DOCUMENT)
    assert not reading.has_counters
    assert reading.reallocated is None
    assert reading.pending is None
    assert reading.power_on_hours is None


def test_unrecognised_attributes_are_kept_not_dropped() -> None:
    document = dict(SATA_DOCUMENT)
    document["ata_smart_attributes"] = {
        "table": [{"id": 251, "name": "Vendor_Specific", "raw": {"value": 99}}]
    }
    reading = smart_module.parse(document)
    assert "251" in reading.unparsed


def test_a_zero_counter_is_distinguished_from_a_missing_one() -> None:
    """`0 pending sectors` and `pending sectors unknown` are different facts."""
    document = dict(SATA_DOCUMENT)
    document["ata_smart_attributes"] = {
        "table": [{"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}}]
    }
    reported_zero = smart_module.parse(document)
    assert reported_zero.pending == 0
    assert reported_zero.has_counters

    silent = smart_module.parse(BRIDGE_DOCUMENT)
    assert silent.pending is None
    assert not silent.has_counters


# -- the risk model -----------------------------------------------------------

def test_a_pending_sector_dominates_the_estimate(table) -> None:
    """It means unreadable data the drive could not relocate. Not a nudge."""
    drive = {"id": 1, "media": "hdd", "bus": "sata", "purchase_date": years_ago(2)}

    healthy = drive_risk(drive, SmartReading(reallocated=0, pending=0), table=table, now=NOW)
    pending = drive_risk(drive, SmartReading(reallocated=0, pending=1), table=table, now=NOW)

    assert pending.annual_probability > healthy.annual_probability * 5
    assert any("pending sector" in reason for reason in pending.reasons)


def test_missing_smart_is_never_treated_as_good_news(table) -> None:
    """The central honesty rule, restated for drives.

    A drive verified clean and a drive that reported nothing are not in the same
    state — the second could have fifty pending sectors with no way to tell — so
    silence must score *strictly worse*, not merely no better. An earlier version
    of this test passed by equality, which would have let a future change price
    uncertainty at zero without anything noticing.
    """
    old = {"id": 1, "media": "hdd", "bus": "usb", "purchase_date": years_ago(6)}

    silent = drive_risk(old, SmartReading(), table=table, now=NOW)
    no_reading_at_all = drive_risk(old, None, table=table, now=NOW)
    reporting_clean = drive_risk(
        old, SmartReading(reallocated=0, pending=0, power_on_hours=100), table=table, now=NOW
    )

    assert silent.confidence == ESTIMATED
    assert reporting_clean.confidence == MEASURED
    assert silent.annual_probability > reporting_clean.annual_probability
    assert no_reading_at_all.annual_probability > reporting_clean.annual_probability


def test_a_drive_with_nothing_known_is_labelled_unknown(table) -> None:
    bare = drive_risk({"id": None}, None, table=table, now=NOW)
    assert bare.confidence == UNKNOWN
    assert any("placeholder" in reason for reason in bare.reasons)


def test_age_follows_the_bathtub_curve(table) -> None:
    """Year 0 is elevated, years 1-3 are the floor, and it climbs after 4."""
    def probability(age: float) -> float:
        return drive_risk(
            {"id": 1, "media": "hdd", "bus": "sata", "purchase_date": years_ago(age)},
            SmartReading(reallocated=0, pending=0),
            table=table,
            now=NOW,
        ).annual_probability

    infant, settled, old = probability(0.5), probability(2), probability(7)
    assert infant > settled, "infant mortality is real and the curve must show it"
    assert old > infant, "a seven-year-old drive is the riskiest of the three"


def test_a_usb_enclosure_counts_against_the_drive(table) -> None:
    """The bridge is part of the failure unit; losing it loses access."""
    base = {"id": 1, "media": "hdd", "purchase_date": years_ago(2)}
    internal = drive_risk({**base, "bus": "sata"}, SmartReading(pending=0), table=table, now=NOW)
    external = drive_risk({**base, "bus": "usb"}, SmartReading(pending=0), table=table, now=NOW)
    assert external.annual_probability > internal.annual_probability


def test_probability_never_reaches_certainty(table) -> None:
    """Even a drive failing every check might survive the year."""
    doomed = drive_risk(
        {"id": 1, "media": "hdd", "bus": "usb", "purchase_date": years_ago(10)},
        SmartReading(
            pending=90, uncorrectable=90, reallocated=900, crc_errors=9000, passed=False
        ),
        table=table,
        now=NOW,
    )
    assert doomed.annual_probability < 1.0
    assert doomed.annual_probability == pytest.approx(risk_module.MAX_ANNUAL_PROBABILITY)


# -- correlation: the flattering error ----------------------------------------

def test_two_copies_from_one_purchase_order_are_worth_less(table) -> None:
    """Multiplying as if independent is the most flattering error available."""
    same_batch = [
        {"id": 1, "batch_key": "WD|RED|2021-03"},
        {"id": 2, "batch_key": "WD|RED|2021-03"},
    ]
    different = [
        {"id": 1, "batch_key": "WD|RED|2021-03"},
        {"id": 2, "batch_key": "SEAGATE|IRONWOLF|2023-08"},
    ]
    assert risk_module.correlation(same_batch, table=table) > 1.0
    assert risk_module.correlation(different, table=table) == 1.0


def test_two_copies_in_one_room_are_worth_less(table) -> None:
    same_room = [
        {"id": 1, "disaster_zone": "home-office"},
        {"id": 2, "disaster_zone": "home-office"},
    ]
    apart = [
        {"id": 1, "disaster_zone": "home-office"},
        {"id": 2, "disaster_zone": "parents-house"},
    ]
    assert risk_module.correlation(same_room, table=table) > 1.0
    assert risk_module.correlation(apart, table=table) == 1.0


def test_correlation_raises_the_joint_probability(table) -> None:
    risks = [
        risk_module.DriveRisk(1, 0.02, MEASURED),
        risk_module.DriveRisk(2, 0.02, MEASURED),
    ]
    independent = risk_module.loss_probability(risks, None, table=table)
    correlated = risk_module.loss_probability(
        risks,
        [{"id": 1, "batch_key": "B"}, {"id": 2, "batch_key": "B"}],
        table=table,
    )
    assert correlated > independent


def test_content_on_no_drive_is_already_lost(table) -> None:
    """Returning 0.0 would rank the most endangered content as the safest."""
    assert risk_module.loss_probability([], None, table=table) == 1.0


def test_confidence_is_that_of_the_weakest_link() -> None:
    risks = [
        risk_module.DriveRisk(1, 0.01, MEASURED),
        risk_module.DriveRisk(2, 0.01, ESTIMATED),
    ]
    assert risk_module.worst_confidence(risks) == ESTIMATED


# -- the AFR table is data, not code ------------------------------------------

def test_the_shipped_table_declares_itself_generic(table) -> None:
    """A default must not be mistaken for a published fleet statistic."""
    assert table.is_generic
    assert "generic-defaults" in table.citation
    assert table.version


def test_per_model_data_overrides_the_generic_baseline(tmp_path: Path) -> None:
    source = json.loads(
        (afr_module.STANDARDS_DIR / afr_module.DEFAULT_TABLE).read_text()
    )
    source["per_model"] = {
        "ST8000DM004": {
            "afr": 0.0095,
            "source": "Backblaze Drive Stats",
            "url": "https://example.invalid/stats",
            "retrieved_at": "2026-01-15",
        }
    }
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(source))

    custom = afr_module.load(str(path))
    published, from_published = custom.base_for("hdd", "ST8000DM004")
    assert from_published
    assert published == 0.0095

    generic, from_generic = custom.base_for("hdd", "Some Other Drive")
    assert not from_generic
    assert generic == custom.base_afr["hdd"]


def test_a_malformed_table_is_an_error_not_a_default(tmp_path: Path) -> None:
    """Silently substituting defaults would make a broken rubric invisible."""
    path = tmp_path / "broken.json"
    path.write_text('{"id": "x", "version": "1"}')
    with pytest.raises(afr_module.AfrError):
        afr_module.load(str(path))


def test_the_standards_table_is_declared_as_packaged_data() -> None:
    """A frozen build with no AFR table raises on the first risk report.

    That failure would not appear in CI — the build succeeds and the smoke test
    does not ask for a report — so the spec entry is asserted here instead.
    """
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "drivefusion.spec").read_text()
    assert '"standards"' in spec, "standards/ is not collected into the build"

    shipped = afr_module.STANDARDS_DIR / afr_module.DEFAULT_TABLE
    assert shipped.exists(), f"the default table is missing from {shipped}"


def test_the_table_path_is_resolved_for_a_frozen_build(monkeypatch, tmp_path: Path) -> None:
    """PyInstaller unpacks data under sys._MEIPASS, not beside the modules."""
    from drivefusion.core import standards as core_standards

    monkeypatch.setattr(core_standards.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert core_standards.standards_dir() == tmp_path / "standards"
    assert afr_module._standards_dir() == tmp_path / "standards" / "afr"

    monkeypatch.delattr(core_standards.sys, "_MEIPASS", raising=False)
    assert afr_module._standards_dir().is_dir(), "checkout path broke"
    assert core_standards.standards_dir().is_dir()


def test_a_missing_table_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(afr_module.AfrError):
        afr_module.load(str(tmp_path / "absent.json"))


# -- storage ------------------------------------------------------------------

def test_health_is_history_not_a_snapshot(catalog: Catalog) -> None:
    """A count that just changed and one steady for years are different facts."""
    drive_id = catalog.upsert_drive(serial="S-HIST")
    catalog.record_health(drive_id, confidence=MEASURED, reallocated=0, observed_at="2026-01-01T00:00:00+00:00")
    catalog.record_health(drive_id, confidence=MEASURED, reallocated=0, observed_at="2026-05-01T00:00:00+00:00")
    catalog.record_health(drive_id, confidence=MEASURED, reallocated=6, observed_at="2026-09-01T00:00:00+00:00")

    history = catalog.health_history(drive_id)
    assert [row["reallocated"] for row in history] == [0, 0, 6]
    assert catalog.latest_health(drive_id)["reallocated"] == 6


def test_an_unreported_counter_is_stored_as_null(catalog: Catalog) -> None:
    """Storing 0 would turn "said nothing" into "said it is fine"."""
    drive_id = catalog.upsert_drive(serial="S-NULL")
    catalog.record_health(drive_id, confidence=ESTIMATED)

    row = catalog.latest_health(drive_id)
    assert row["reallocated"] is None
    assert row["pending"] is None
    assert row["confidence"] == ESTIMATED


def test_reading_from_row_reports_absence_as_absence(catalog: Catalog) -> None:
    from drivefusion.core.health.collect import reading_from_row

    drive_id = catalog.upsert_drive(serial="S-EMPTY")
    catalog.record_health(drive_id, confidence=ESTIMATED)
    assert reading_from_row(catalog.latest_health(drive_id)) is None

    catalog.record_health(drive_id, confidence=MEASURED, pending=0, reallocated=1)
    recovered = reading_from_row(catalog.latest_health(drive_id))
    assert recovered is not None
    assert recovered.reallocated == 1


def test_drive_metadata_is_editable_but_identity_is_not(catalog: Catalog) -> None:
    """Serial identifies the drive; letting it be edited would re-point history."""
    drive_id = catalog.upsert_drive(serial="S-EDIT", model="M")
    catalog.update_drive(
        drive_id,
        nickname="ARCHIVE-2",
        purchase_date="2024-02-01",
        vendor="Example Ltd",
        disaster_zone="parents-house",
        batch_key="WD|RED|2024-02",
    )
    row = catalog.drive(drive_id)
    assert row["nickname"] == "ARCHIVE-2"
    assert row["disaster_zone"] == "parents-house"

    with pytest.raises(ValueError, match="serial"):
        catalog.update_drive(drive_id, serial="SOMETHING-ELSE")
    with pytest.raises(ValueError):
        catalog.update_drive(drive_id, id=99)
