"""Collecting health for the drives in the catalog.

Reading SMART is the one place Drive Fusion runs an external program against a
user's hardware, so the rules are narrow: read-only arguments only (enforced in
``smart.py``), never spin up a sleeping drive to interrogate it, and a drive
that reports nothing is recorded as *having reported nothing* rather than as
healthy.

Every observation is appended. A reallocated count that went from 0 to 3 last
month is a different fact from one that has read 3 for two years, and only a
series can tell those apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import drivefusion.core.health.risk as risk_module
import drivefusion.core.health.smart as smart_module
from drivefusion.core.health.afr import AfrTable
from drivefusion.core.health.smart import SmartError, SmartReading
from drivefusion.core.store.catalog import Catalog


@dataclass
class CollectionResult:
    measured: int = 0
    estimated: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.measured} measured", f"{self.estimated} estimated"]
        if self.failed:
            parts.append(f"{self.failed} unreadable")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return "; ".join(parts)


def device_for(drive: dict, volumes: list[dict] | None = None) -> str | None:
    """The path smartctl should be pointed at for a drive.

    Windows uses ``/dev/sdN``-style aliases through smartmontools' own mapping,
    or a physical drive number. Where the catalog does not know, this returns
    ``None`` and the caller records an estimate rather than guessing a device
    node — pointing smartctl at the wrong disk would attribute one drive's
    health to another, which is worse than having no reading.
    """
    device = drive.get("smart_device")
    if device:
        return str(device)
    for volume in volumes or []:
        if volume.get("smart_device"):
            return str(volume["smart_device"])
    return None


def collect_health(
    catalog: Catalog,
    *,
    table: AfrTable | None = None,
    runner=None,
    progress=None,
) -> CollectionResult:
    """Read health for every known drive and record what was found.

    Drives with no reachable device get an age-and-media estimate, labelled as
    such. The pass never fails as a whole because one drive is unreadable: an
    archive with a dead enclosure still needs a report about the other nineteen.
    """
    result = CollectionResult()
    drives = [dict(row) for row in catalog.drives()]

    for drive in drives:
        if progress:
            progress(drive)

        reading: SmartReading | None = None
        document: dict | None = None
        device = device_for(drive)

        if device and smart_module.available():
            try:
                document = smart_module.run(device, runner=runner)
                reading = smart_module.parse(document)
            except SmartError as exc:
                result.failed += 1
                result.notes.append(f"{_name(drive)}: {exc}")

        assessment = risk_module.drive_risk(drive, reading, table=table)
        counters: dict = {}
        if reading is not None:
            counters = {
                "power_on_hours": reading.power_on_hours,
                "temperature_c": reading.temperature_c,
                "reallocated": reading.reallocated,
                "pending": reading.pending,
                "uncorrectable": reading.uncorrectable,
                "crc_errors": reading.crc_errors,
                "wear_leveling": reading.wear_leveling,
                "tbw_written": reading.tbw_written,
                "self_test_status": reading.self_test_status,
                "smart_passed": None if reading.passed is None else int(reading.passed),
            }

        catalog.record_health(
            int(drive["id"]),
            confidence=assessment.confidence,
            annual_failure_prob=assessment.annual_probability,
            smart_json=json.dumps(document) if document else None,
            **counters,
        )

        if assessment.confidence == risk_module.MEASURED:
            result.measured += 1
        elif assessment.confidence == risk_module.ESTIMATED:
            result.estimated += 1
        else:
            result.skipped += 1

    if not smart_module.available():
        result.notes.append(
            "smartctl is not installed, so every figure is an age-and-media "
            "estimate. Install smartmontools for measured rates."
        )
    return result


def _name(drive: dict) -> str:
    return (
        drive.get("nickname")
        or drive.get("model")
        or drive.get("serial")
        or f"drive {drive.get('id')}"
    )


def reading_from_row(row) -> SmartReading | None:
    """Rebuild a reading from a stored ``drive_health`` row.

    Returns ``None`` where nothing was reported, so the risk model sees the same
    absence of evidence it would have seen live rather than a row of NULLs that
    might be mistaken for zeroes.
    """
    if row is None:
        return None
    reading = SmartReading(
        power_on_hours=row["power_on_hours"],
        temperature_c=row["temperature_c"],
        reallocated=row["reallocated"],
        pending=row["pending"],
        uncorrectable=row["uncorrectable"],
        crc_errors=row["crc_errors"],
        wear_leveling=row["wear_leveling"],
        tbw_written=row["tbw_written"],
        self_test_status=row["self_test_status"],
        passed=None if row["smart_passed"] is None else bool(row["smart_passed"]),
    )
    return reading if reading.has_counters or reading.power_on_hours is not None else None
