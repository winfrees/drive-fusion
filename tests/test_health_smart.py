"""SMART through smartctl: read-only, pinned, and honest about "unknown" (ROADMAP M5.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core.health import smart
from drivefusion.core.health.smart import SmartError, assert_read_only, parse, query_argv
from drivefusion.core.store import audit
from drivefusion.core.store.catalog import Catalog

FIXTURES = Path(__file__).parent / "fixtures" / "smart"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "catalog.db") as cat:
        yield cat


# -- read-only, enforced at run time --------------------------------------------

def test_the_query_is_exactly_the_read_only_arguments() -> None:
    argv = query_argv(Path("smartctl.exe"), "/dev/pd1")
    assert argv == ["smartctl.exe", "--json=c", "-i", "-H", "-A", "-l", "selftest", "/dev/pd1"]
    assert query_argv(Path("smartctl.exe"), "/dev/pd1", "sat")[-3:] == ["-d", "sat", "/dev/pd1"]


@pytest.mark.parametrize("bad", ["-t", "--test=long", "-s", "--smart=off", "-o", "-S",
                                 "--saveauto=on", "--set=standby,now", "-X", "-C"])
def test_any_state_changing_option_is_refused(bad: str) -> None:
    with pytest.raises(SmartError, match="would change the drive"):
        assert_read_only(["smartctl", "-i", bad, "/dev/pd0"])


# -- parsing ---------------------------------------------------------------------

def test_a_healthy_hdd() -> None:
    r = parse(fixture("ata_hdd_healthy.json"))
    assert r.available and r.confidence == "measured" and r.passed is True
    assert (r.reallocated, r.pending, r.uncorrectable, r.crc_errors) == (0, 0, 0, 0)
    assert (r.power_on_hours, r.temperature_c) == (12000, 34)
    assert r.tbw_written == 39062500000 * 512
    assert r.source == "smartctl 7.5" and r.model == "ST4000DM004-2CV104"
    assert r.self_test_status == "Completed without error"


def test_a_failing_hdd_keeps_every_bad_number() -> None:
    r = parse(fixture("ata_hdd_failing.json"))
    assert r.available and r.passed is False
    assert (r.reallocated, r.pending, r.uncorrectable, r.crc_errors) == (2048, 16, 8, 3)
    assert r.self_test_status == "Completed: read failure"


def test_ssd_wear_is_life_remaining() -> None:
    r = parse(fixture("sata_ssd.json"))
    assert r.wear_leveling == 97 and r.uncorrectable == 0     # 187 when 198 is absent


def test_nvme() -> None:
    r = parse(fixture("nvme.json"))
    assert r.available and r.passed is True
    assert (r.wear_leveling, r.uncorrectable) == (96, 0)        # 100 - percentage_used
    assert r.tbw_written == 19531250 * 512_000
    assert (r.power_on_hours, r.temperature_c) == (3100, 38)


@pytest.mark.parametrize(("name", "reason"), [
    ("no_device_type.json", "Unable to detect device type"),      # real smartctl output
    ("usb_bridge_unknown.json", "Unknown USB bridge"),
])
def test_no_data_is_unknown_with_smartctls_reason_never_zeros(name: str, reason: str) -> None:
    r = parse(fixture(name))
    assert not r.available and r.confidence == "unknown"
    assert reason in r.reason
    assert r.reallocated is None and r.pending is None, "no data must not read as no problems"


def test_garbage_is_unknown() -> None:
    assert parse("not json").reason == "smartctl did not return JSON"


# -- reading and recording -----------------------------------------------------

def _drive(catalog: Catalog) -> int:
    return catalog.upsert_drive(serial="SMART-1", model="ST4000DM004")


def test_a_reading_is_recorded_and_audited(catalog: Catalog) -> None:
    drive = _drive(catalog)
    calls = []

    def runner(exe, device, dev_type=None):
        calls.append((device, dev_type))
        return fixture("ata_hdd_healthy.json")

    reading = smart.read_drive(catalog, drive, smartctl=Path("smartctl.exe"), runner=runner,
                               locate=lambda c, d: ("/dev/pd1", ""))
    assert reading.available and calls == [("/dev/pd1", None)]
    row = smart.latest(catalog, drive)
    assert (row["confidence"], row["reallocated"], row["source"]) == ("measured", 0, "smartctl 7.5")
    assert smart.verdict(row) == "OK"
    entry = audit.entries(catalog.conn, limit=1)[0]
    assert entry["action"] == "health.read"
    assert json.loads(entry["detail_json"])["drive_id"] == drive


def test_a_bridge_gets_a_second_try_as_sat(catalog: Catalog) -> None:
    drive = _drive(catalog)
    answers = {None: fixture("usb_bridge_unknown.json"), "sat": fixture("ata_hdd_healthy.json")}
    calls = []

    def runner(exe, device, dev_type=None):
        calls.append(dev_type)
        return answers[dev_type]

    reading = smart.read_drive(catalog, drive, smartctl=Path("smartctl.exe"), runner=runner,
                               locate=lambda c, d: ("/dev/pd4", ""))
    assert calls == [None, "sat"] and reading.available


def test_a_disconnected_drive_is_recorded_as_unknown_with_the_reason(catalog: Catalog) -> None:
    drive = _drive(catalog)
    reading = smart.read_drive(catalog, drive, smartctl=Path("smartctl.exe"),
                               runner=lambda *a, **k: pytest.fail("ran smartctl"),
                               locate=lambda c, d: (None, "the drive is not connected"))
    assert not reading.available
    row = smart.latest(catalog, drive)
    assert row["confidence"] == "unknown" and smart.verdict(row) == "unknown"


def test_a_failing_reading_reads_as_failing(catalog: Catalog) -> None:
    drive = _drive(catalog)
    smart.read_drive(catalog, drive, smartctl=Path("smartctl.exe"),
                     runner=lambda *a, **k: fixture("ata_hdd_failing.json"),
                     locate=lambda c, d: ("/dev/pd2", ""))
    assert smart.verdict(smart.latest(catalog, drive)) == "FAILING"
    assert smart.verdict(None) == "not read"


def test_readings_accumulate_rather_than_overwrite(catalog: Catalog) -> None:
    drive = _drive(catalog)
    for name in ("ata_hdd_healthy.json", "ata_hdd_failing.json"):
        smart.read_drive(catalog, drive, smartctl=Path("smartctl.exe"),
                         runner=lambda *a, n=name, **k: fixture(n),
                         locate=lambda c, d: ("/dev/pd1", ""))
    count = catalog.conn.execute(
        "SELECT COUNT(*) FROM drive_health WHERE drive_id = ?", (drive,)).fetchone()[0]
    assert count == 2


# -- the pinned smartctl ---------------------------------------------------------

def test_an_unpinned_or_tampered_smartctl_is_not_run(tmp_path: Path, monkeypatch) -> None:
    exe = tmp_path / "smartctl.exe"
    exe.write_bytes(b"MZ not smartctl")
    monkeypatch.setattr(smart.subprocess, "run", lambda *a, **k: pytest.fail("ran it"))

    monkeypatch.setattr(smart.pinned, "pinned_sha256", lambda name: "0" * 64)
    with pytest.raises(SmartError, match="not the smartctl this build shipped"):
        smart.run(exe, "/dev/pd0")

    monkeypatch.setattr(smart.pinned, "pinned_sha256", lambda name: None)
    with pytest.raises(SmartError, match="can be written to"):
        smart.run(exe, "/dev/pd0")


def test_a_source_checkout_has_no_bundled_smartctl() -> None:
    assert smart.bundled_smartctl() is None


def test_the_lock_file_pins_both_hashes() -> None:
    lock = json.loads((Path(__file__).parent.parent / "packaging" / "smartmontools.json").read_text())
    for key in ("installer_sha256", "smartctl_sha256"):
        assert len(lock[key]) == 64 and int(lock[key], 16) >= 0
    assert lock["license"].startswith("GPL") and lock["signing_key_fingerprint"]
