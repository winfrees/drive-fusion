"""The AFR model: base rates as data, factors as pure functions (ROADMAP M5.4)."""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from drivefusion.core.health import afr

ROOT = Path(__file__).parent.parent
TODAY = dt.date(2026, 10, 30)
PARAMS = afr.model()


def table(**overrides) -> afr.RateTable:
    data = {"source": "test", "version": "1", "basis": "fleet",
            "classes": {"hdd": 0.02, "ssd": 0.01, "unknown": 0.03},
            "models": [{"model": "ST4000DM000", "afr": 0.025},
                       {"model": "HGST HMS5C4040BLE640", "afr": 0.004}]}
    data.update(overrides)
    return afr.rate_table(data)


def drive(**columns):
    row = {"model": None, "media": None, "purchase_date": None, "bus": None}
    row.update(columns)
    return row


# -- the shipped data files ---------------------------------------------------------

def test_every_shipped_table_names_its_source_and_version() -> None:
    for path in (ROOT / "drivefusion" / "standards" / "afr").glob("*.json"):
        data = json.loads(path.read_text())
        assert data["kind"] in ("afr-rates", "afr-model"), path.name
        assert path.name == f"{data['kind'][4:]}-{data['source']}-{data['version']}.json"
        if data["kind"] == "afr-rates":
            assert data["basis"] in afr.BASIS_ORDER
            assert all(0 < r < 1 for r in data["classes"].values())


def test_a_rate_exists_for_any_drive_from_the_shipped_tables() -> None:
    base, matched, _ = afr.base_afr("NO SUCH MODEL", None, afr.tables())
    assert matched == "fallback" and 0 < base < 1


def test_fleet_statistics_outrank_placeholders(monkeypatch) -> None:
    files = [
        {"kind": "afr-rates", "source": "provisional", "version": "1", "basis": "provisional",
         "classes": {"hdd": 0.015, "unknown": 0.015}},
        {"kind": "afr-rates", "source": "backblaze", "version": "2025Q2", "basis": "fleet",
         "classes": {"hdd": 0.012}},
    ]
    monkeypatch.setattr(afr, "_files", lambda: files)
    afr.tables.cache_clear()
    try:
        ordered = afr.tables()
        assert [t.name for t in ordered] == ["backblaze-2025Q2", "provisional-1"]
        assert afr.base_afr(None, "hdd", ordered)[0] == 0.012
        assert afr.base_afr(None, None, ordered)[0] == 0.015, "fallback from the next table"
    finally:
        afr.tables.cache_clear()


# -- base rate ------------------------------------------------------------------------

def test_a_model_matches_its_family_whatever_the_suffix_or_vendor_prefix() -> None:
    t = (table(),)
    assert afr.base_afr("ST4000DM000-1F2168", "hdd", t)[:2] == (0.025, "model")
    assert afr.base_afr("HMS5C4040BLE640", "hdd", t)[:2] == (0.004, "model")
    assert afr.base_afr("hgst hms5c4040ble640", "hdd", t)[:2] == (0.004, "model")


def test_no_model_falls_back_to_the_media_class_then_to_unknown() -> None:
    t = (table(),)
    assert afr.base_afr("WD40EFRX", "ssd", t)[:2] == (0.01, "class")
    assert afr.base_afr(None, "hdd", t)[:2] == (0.02, "class")
    assert afr.base_afr(None, None, t)[:2] == (0.03, "fallback")


def test_a_short_model_string_never_matches_a_family() -> None:
    t = (table(models=[{"model": "ST4", "afr": 0.5}]),)
    assert afr.base_afr("ST4000DM000", "hdd", t)[1] == "class"


# -- the factors ------------------------------------------------------------------------

@pytest.mark.parametrize(("years", "factor"), [
    (None, 1.0), (0.2, 1.3), (2.0, 0.9), (3.5, 1.0), (4.5, 1.3), (5.5, 1.7), (9.0, 2.2),
])
def test_hdd_age_is_a_bathtub(years, factor) -> None:
    assert afr.age_factor(years, "hdd", PARAMS) == factor


def test_age_from_a_purchase_month_or_day() -> None:
    assert afr.age_years(None, TODAY) is None
    assert afr.age_years("2026-10-15", TODAY) == pytest.approx(15 / 365.25)
    assert afr.age_years("2021-10", TODAY) == pytest.approx(5.04, abs=0.01)   # mid-month
    assert afr.age_years("2030-01", TODAY) == 0.0


def test_duty_counts_only_what_was_measured() -> None:
    assert afr.duty_factor("hdd", None, None, PARAMS) == 1.0
    assert afr.duty_factor("hdd", 10_000, None, PARAMS) == 1.0
    assert afr.duty_factor("hdd", 40_000, None, PARAMS) == 1.5
    assert afr.duty_factor("ssd", 40_000, None, PARAMS) == 1.0, "hours don't wear an SSD"
    assert afr.duty_factor("ssd", None, 5, PARAMS) == 3.0
    assert afr.duty_factor("ssd", None, 97, PARAMS) == 1.0


def test_a_pending_sector_dominates() -> None:
    assert afr.smart_factor(None, None, None, None, PARAMS) == 1.0
    assert afr.smart_factor(0, 0, 0, 0, PARAMS) == 1.0
    assert afr.smart_factor(8, 0, 0, 0, PARAMS) == 2.0
    assert afr.smart_factor(500, 0, 0, 0, PARAMS) == 4.0
    assert afr.smart_factor(8, 0, 3, 0, PARAMS) == 5.0
    assert afr.smart_factor(8, 1, 3, 0, PARAMS) == 10.0
    assert afr.smart_factor(0, 0, 0, 7, PARAMS) == pytest.approx(1.2)


def test_a_usb_bridge_adds_risk() -> None:
    assert afr.bus_factor("usb", PARAMS) == 1.2
    assert afr.bus_factor("sata", PARAMS) == afr.bus_factor(None, PARAMS) == 1.0


# -- one drive ----------------------------------------------------------------------

def test_without_smart_a_drive_is_estimated_and_says_what_was_assumed() -> None:
    risk = afr.drive_risk(drive(model="ST4000DM000-1F2168", media="hdd",
                                purchase_date="2019-03", bus="usb"),
                          today=TODAY, rate_tables=(table(),))
    assert risk.confidence == "estimated"
    assert risk.afr == pytest.approx(0.025 * 2.2 * 1.2)
    assert risk.factors["smart"] == risk.factors["duty"] == 1.0
    assert any("no SMART reading" in a for a in risk.assumptions)


def test_nothing_known_is_unknown_but_still_a_rate() -> None:
    risk = afr.drive_risk(drive(), today=TODAY, rate_tables=(table(),))
    assert risk.confidence == "unknown" and risk.afr == 0.03
    assert any("purchase date unknown" in a for a in risk.assumptions)


def test_an_unknown_input_never_lowers_the_rate() -> None:
    known = afr.drive_risk(drive(media="hdd", purchase_date="2024-01"), today=TODAY,
                           rate_tables=(table(),))
    assert known.factors["age"] < 1.0       # the flat middle of the bathtub
    unknown = afr.drive_risk(drive(media="hdd"), today=TODAY, rate_tables=(table(),))
    assert unknown.factors["age"] == 1.0    # unknown counts as average, not as best


def test_a_measured_smart_reading_is_used_and_capped() -> None:
    health = {"confidence": "measured", "power_on_hours": 40_000, "wear_leveling": None,
              "reallocated": 2048, "pending": 16, "uncorrectable": 8, "crc_errors": 3}
    risk = afr.drive_risk(drive(model="ST4000DM000", media="hdd", purchase_date="2017-01"),
                          today=TODAY, health=health, rate_tables=(table(),))
    assert risk.confidence == "measured"
    assert risk.afr == pytest.approx(min(0.025 * 2.2 * 1.5 * 10 * 1.2, PARAMS.max_afr))


def test_an_unknown_smart_reading_is_ignored() -> None:
    health = {"confidence": "unknown", "pending": 99}
    risk = afr.drive_risk(drive(media="hdd"), today=TODAY, health=health,
                          rate_tables=(table(),))
    assert risk.factors["smart"] == 1.0 and risk.confidence == "estimated"


# -- importing a Backblaze snapshot ------------------------------------------------------

def _write_stats(path: Path, rows: list[tuple[str, str, int]]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "serial_number", "model", "capacity_bytes", "failure"])
        for i, (model, serial, failure) in enumerate(rows):
            writer.writerow([f"2025-04-{i % 28 + 1:02d}", serial, model, 4000787030016, failure])


def test_the_importer_computes_afr_per_model(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import import_backblaze
    finally:
        sys.path.pop(0)
    stats = tmp_path / "2025-04.csv"
    rows = [("ST4000DM000", f"S{i}", 0) for i in range(730)]
    rows += [("ST4000DM000", "S0", 1), ("ST4000DM000", "S1", 1)]
    rows += [("TINY MODEL", "T1", 1)]
    _write_stats(stats, rows)
    out = tmp_path / "afr"
    out.mkdir()
    assert import_backblaze.main([str(stats), "--version", "2025Q2", "--min-drive-days", "100",
                                  "--retrieved", "2026-10-30", "--out", str(out)]) == 0
    data = json.loads((out / "rates-backblaze-2025Q2.json").read_text())
    [entry] = data["models"]                         # TINY MODEL had too little exposure
    assert entry["model"] == "ST4000DM000" and entry["drive_days"] == 732
    assert entry["afr"] == pytest.approx(2 / (732 / 365), abs=1e-6)
    assert data["classes"]["hdd"] == pytest.approx(3 / (733 / 365), abs=1e-6)
    assert data["basis"] == "fleet" and len(data["inputs"]["2025-04.csv"]) == 64
    assert afr.rate_table(data).models[0] == ("ST4000DM000", entry["afr"])
