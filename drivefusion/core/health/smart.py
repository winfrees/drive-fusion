"""Reading SMART health through smartctl, and only reading it (ROADMAP M5.3).

smartctl can do more than read: it can start self-tests, switch SMART on and
off, and change a drive's offline and autosave settings. Drive Fusion's
promise is that it changes nothing, and that includes a drive's firmware
state, so it runs smartctl with one fixed set of read-only arguments
(:data:`READ_ONLY_ARGS`) and nothing else:

* :func:`assert_read_only` refuses any argv that names a state-changing
  option, and every launch goes through it;
* ``tools/ro_lint.py`` (DF014) fails the build if a module that may start a
  process names one of those options outside the list that forbids them.

The smartctl that runs is the one the build bundled, checked against its
pinned SHA-256 through a handle that keeps it from being swapped
(``core/pinned.py``). SMART needs administrator rights on Windows; the CLI
and GUI ask before reading, and say why when they can't.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from drivefusion.core import pinned

#: The only arguments smartctl is ever given, besides an optional ``-d TYPE``
#: and the device: identity, overall health, attributes, self-test log.
READ_ONLY_ARGS = ("--json=c", "-i", "-H", "-A", "-l", "selftest")

#: Device types tried after an automatic attempt fails behind a USB bridge.
#: ``sat`` (SCSI-to-ATA translation) is what most bridges speak.
BRIDGE_TYPES = ("sat",)

#: Options that change a drive's state. Exempt from DF014 by its name only.
SMARTCTL_FORBIDDEN = (
    "-t", "--test", "-s", "--smart", "-o", "--offlineauto", "-S", "--saveauto",
    "--set", "-X", "--abort", "-C", "--captive",
)

#: Exit-status bits (smartctl(8)): the command line or the device could not be
#: used, so no health was read at all.
_NO_DATA_BITS = 0b0000_0111


class SmartError(RuntimeError):
    """smartctl could not be run, or was asked to do more than read."""


def assert_read_only(argv: list[str]) -> None:
    """Refuse an argv naming any state-changing smartctl option."""
    for arg in argv[1:]:
        option = arg.split("=", 1)[0]
        if option in SMARTCTL_FORBIDDEN:
            raise SmartError(f"refusing to run smartctl with {arg!r}: it would change the drive")


def query_argv(smartctl: Path, device: str, dev_type: str | None = None) -> list[str]:
    argv = [str(smartctl), *READ_ONLY_ARGS]
    if dev_type:
        argv += ["-d", dev_type]
    argv.append(device)
    assert_read_only(argv)
    return argv


@dataclass
class HealthReading:
    """One reading of one drive, in the units ``drive_health`` stores."""

    available: bool
    reason: str = ""                      # why nothing was read, when it wasn't
    source: str | None = None             # smartctl version
    device: str | None = None
    model: str | None = None
    serial: str | None = None
    passed: bool | None = None            # the drive's own overall verdict
    power_on_hours: int | None = None
    temperature_c: int | None = None
    reallocated: int | None = None
    pending: int | None = None
    uncorrectable: int | None = None
    crc_errors: int | None = None
    wear_leveling: int | None = None      # percent of rated life remaining
    tbw_written: int | None = None        # bytes written over the drive's life
    self_test_status: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def confidence(self) -> str:
        return "measured" if self.available else "unknown"


def _attrs(data: dict) -> dict[int, dict]:
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    return {int(a["id"]): a for a in table if "id" in a}


def _raw(attrs: dict[int, dict], *ids: int) -> int | None:
    for i in ids:
        if i in attrs:
            raw = attrs[i].get("raw") or {}
            if isinstance(raw.get("value"), int):
                return raw["value"]
    return None


def _first_error(data: dict) -> str:
    for message in (data.get("smartctl") or {}).get("messages") or []:
        if message.get("severity") == "error" and message.get("string"):
            return message["string"]
    return ""


def parse(text: str) -> HealthReading:
    """smartctl ``--json`` output to a :class:`HealthReading`.

    A drive that answered nothing (no device type, a bridge that hides SMART,
    access denied) is ``available=False`` with smartctl's own reason, never a
    reading of zeros: "no data" must not look like "no problems".
    """
    try:
        data = json.loads(text)
    except ValueError:
        return HealthReading(False, "smartctl did not return JSON")
    meta = data.get("smartctl") or {}
    version = ".".join(str(v) for v in meta.get("version") or []) or None
    source = f"smartctl {version}" if version else "smartctl"
    nvme = data.get("nvme_smart_health_information_log")
    attrs = _attrs(data)
    has_data = bool(attrs or nvme or "smart_status" in data)
    if not has_data or (int(meta.get("exit_status") or 0) & _NO_DATA_BITS and not attrs and not nvme):
        reason = _first_error(data) or "the drive returned no SMART data"
        return HealthReading(False, reason, source=source, raw=data)

    reading = HealthReading(
        True, source=source, raw=data,
        device=(data.get("device") or {}).get("name"),
        model=data.get("model_name"), serial=data.get("serial_number"),
        passed=(data.get("smart_status") or {}).get("passed"),
        power_on_hours=(data.get("power_on_time") or {}).get("hours"),
        temperature_c=(data.get("temperature") or {}).get("current"),
    )
    if nvme:
        reading.uncorrectable = nvme.get("media_errors")
        used = nvme.get("percentage_used")
        reading.wear_leveling = None if used is None else max(0, 100 - int(used))
        units = nvme.get("data_units_written")
        reading.tbw_written = None if units is None else int(units) * 512_000
        reading.power_on_hours = reading.power_on_hours or nvme.get("power_on_hours")
        reading.temperature_c = reading.temperature_c or nvme.get("temperature")
        return reading

    reading.reallocated = _raw(attrs, 5)
    reading.pending = _raw(attrs, 197)
    reading.uncorrectable = _raw(attrs, 198, 187)
    reading.crc_errors = _raw(attrs, 199)
    for wear_id in (177, 231, 233):           # vendors differ; all are % remaining
        if wear_id in attrs and isinstance(attrs[wear_id].get("value"), int):
            reading.wear_leveling = attrs[wear_id]["value"]
            break
    lbas = _raw(attrs, 241)
    reading.tbw_written = None if lbas is None else lbas * 512
    tests = (((data.get("ata_smart_self_test_log") or {}).get("standard") or {})
             .get("table") or [])
    if tests:
        reading.self_test_status = (tests[0].get("status") or {}).get("string")
    return reading


# -- running it -----------------------------------------------------------------

def bundled_smartctl() -> Path | None:
    """The smartctl this build shipped, or None (a source checkout, or a build
    made without ``tools/fetch_smartctl.py``)."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    path = Path(base) / "smartctl.exe"
    return path if path.is_file() else None


def run(smartctl: Path, device: str, dev_type: str | None = None, *, timeout: int = 60) -> str:
    """Run the bundled smartctl read-only on ``device``; return its JSON text.

    smartctl is hashed through a locked handle and checked against its pin,
    and the handle is held until smartctl has finished.
    """
    argv = query_argv(smartctl, device, dev_type)
    try:
        with pinned.locked(smartctl) as handle:
            pinned.check(smartctl, pinned.digest(handle),
                         pinned=pinned.pinned_sha256("SMARTCTL_SHA256"),
                         writable=pinned.user_can_write(smartctl.parent), what="smartctl")
            done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except pinned.PinError as exc:
        raise SmartError(str(exc)) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmartError(f"smartctl could not be run: {exc}") from None
    return done.stdout.decode("utf-8", "replace")


def device_for_drive(catalog, drive_id: int) -> tuple[str | None, str]:
    """smartctl's name for a drive (``/dev/pdN``) through one of its connected
    volumes, or None and the reason."""
    if sys.platform != "win32":
        return None, "SMART reads are supported on Windows only"
    from drivefusion.core.discovery import windows

    volumes = catalog.conn.execute(
        "SELECT volume_guid FROM volume WHERE drive_id = ? ORDER BY id", (drive_id,)
    ).fetchall()
    for volume in volumes:
        number = windows.disk_number_for_volume(volume["volume_guid"])
        if number is not None:
            return f"/dev/pd{number}", ""
    return None, "the drive is not connected"


def read_drive(catalog, drive_id: int, *, smartctl: Path, runner=None,
               locate=None) -> HealthReading:
    """Read one drive's health and record it (``drive_health``, audited).

    ``runner`` and ``locate`` default to :func:`run` and
    :func:`device_for_drive`, looked up when called.
    """
    runner = runner or run
    locate = locate or device_for_drive
    device, why = locate(catalog, drive_id)
    if device is None:
        reading = HealthReading(False, why)
    else:
        try:
            reading = parse(runner(smartctl, device))
            for dev_type in BRIDGE_TYPES:
                if reading.available:
                    break
                bridged = parse(runner(smartctl, device, dev_type))
                if bridged.available:
                    reading = bridged
        except SmartError as exc:
            reading = HealthReading(False, str(exc))
        reading.device = reading.device or device
    record(catalog, drive_id, reading)
    return reading


def record(catalog, drive_id: int, reading: HealthReading) -> int:
    from drivefusion.core.store.schema import utcnow

    smart_json = json.dumps(reading.raw or {"reason": reading.reason}, separators=(",", ":"))
    cursor = catalog.conn.execute(
        "INSERT INTO drive_health (drive_id, observed_at, source, power_on_hours, "
        "temperature_c, reallocated, pending, uncorrectable, crc_errors, wear_leveling, "
        "tbw_written, self_test_status, smart_json, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (drive_id, utcnow(), reading.source, reading.power_on_hours, reading.temperature_c,
         reading.reallocated, reading.pending, reading.uncorrectable, reading.crc_errors,
         reading.wear_leveling, reading.tbw_written, reading.self_test_status, smart_json,
         reading.confidence),
    )
    summary = {k: v for k, v in asdict(reading).items() if k != "raw" and v is not None}
    catalog.record("health.read", {"drive_id": drive_id, **summary})
    return int(cursor.lastrowid)


def latest(catalog, drive_id: int):
    return catalog.conn.execute(
        "SELECT * FROM drive_health WHERE drive_id = ? ORDER BY id DESC LIMIT 1", (drive_id,)
    ).fetchone()


def verdict(row) -> str:
    """A one-word summary of a ``drive_health`` row for lists and columns."""
    if row is None:
        return "not read"
    if row["confidence"] != "measured":
        return "unknown"
    try:
        passed = (json.loads(row["smart_json"] or "{}").get("smart_status") or {}).get("passed")
    except ValueError:
        passed = None
    if passed is False or (row["pending"] or 0) > 0 or (row["uncorrectable"] or 0) > 0:
        return "FAILING" if passed is False else "warning"
    if (row["reallocated"] or 0) > 0:
        return "warning"
    return "OK"
