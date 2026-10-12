"""smartctl: reading drive health without touching the drive.

**The read-only problem this module introduces.** Everything before M5 read files
through a gateway that has no writer and opens handles without write access.
``smartctl`` is different: it is an external program that *can* change a drive's
state. ``smartctl -t long /dev/sda`` starts a self-test that runs for hours;
``-s off`` disables SMART; ``--set`` alters drive configuration. None of that is
a filesystem write, so the gateway, the lint's mutating-call rules and the
no-touch test would all miss it. A tool whose central promise is "never modifies
your drives" must not be able to start a self-test on one.

So the invocation is constrained the way the elevation path is (``gui/
elevation.py``): an **allowlist of arguments**, checked at the point of use.
Anything not on the list is refused before a process is spawned, including by a
future edit that means well. The read-only flags are the whole of what this tool
needs — SMART data is a read.

Parsing is a pure function over already-decoded JSON, so every vendor quirk below
is testable without a drive attached, on any platform.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

#: Arguments this tool is permitted to pass to smartctl. Read-only, all of them.
#:
#: ``--json`` machine-readable output, ``-a``/``-x`` read all attributes,
#: ``-i`` identity, ``-H`` overall health, ``-n standby`` declines to spin up a
#: sleeping drive, and ``-d <type>`` selects how to talk through a bridge.
ALLOWED_FLAGS = frozenset(
    {"--json", "-j", "-a", "-x", "-i", "-H", "-c", "-A", "-n", "-d", "--scan", "--scan-open"}
)

#: Arguments that change the drive, and the reason each is refused. Checked
#: explicitly as well as by the allowlist so the refusal can say *why*, and so
#: this list reads as documentation of what the tool will not do.
FORBIDDEN_FLAGS = {
    "-t": "would start a self-test on the drive",
    "--test": "would start a self-test on the drive",
    "-X": "would abort a running self-test",
    "--abort": "would abort a running self-test",
    "-s": "would enable or disable SMART on the drive",
    "--smart": "would enable or disable SMART on the drive",
    "-o": "would change automatic offline testing",
    "--offlineauto": "would change automatic offline testing",
    "-S": "would change attribute autosave",
    "--saveauto": "would change attribute autosave",
    "--set": "would change drive configuration",
}


class SmartError(RuntimeError):
    """smartctl could not be run, or was asked to do something it must not."""


@dataclass(frozen=True)
class SmartReading:
    """What one smartctl run established about a drive.

    Every count is ``None`` when the drive did not report it. ``None`` is not
    zero: a USB bridge that passes no SMART data through is not a drive with no
    reallocated sectors, and the risk model must be able to tell the difference.
    """

    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    media: str | None = None            # hdd | ssd | nvme
    capacity_bytes: int | None = None
    rotation_rpm: int | None = None

    power_on_hours: int | None = None
    temperature_c: int | None = None
    reallocated: int | None = None
    pending: int | None = None
    uncorrectable: int | None = None
    crc_errors: int | None = None
    wear_leveling: int | None = None    # percent of SSD endurance consumed
    tbw_written: int | None = None      # bytes written over the drive's life

    passed: bool | None = None          # the drive's own overall-health verdict
    self_test_status: str | None = None
    #: Attributes the drive reported that we did not recognise, kept so a vendor
    #: quirk is visible rather than silently dropped.
    unparsed: dict = field(default_factory=dict)

    @property
    def has_counters(self) -> bool:
        """True when at least one error counter came back.

        Drives the whole ``measured`` vs ``estimated`` distinction: without this,
        an empty reading would be scored as a perfectly healthy drive.
        """
        return any(
            value is not None
            for value in (
                self.reallocated,
                self.pending,
                self.uncorrectable,
                self.crc_errors,
                self.wear_leveling,
            )
        )


def check_arguments(arguments: list[str]) -> None:
    """Refuse anything that could change the drive. Raises, never warns."""
    for argument in arguments:
        flag = argument.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise SmartError(
                f"refusing to run smartctl {flag}: it {FORBIDDEN_FLAGS[flag]}. "
                "Drive Fusion reads drive health and never alters a drive."
            )
        if flag.startswith("-") and flag not in ALLOWED_FLAGS:
            raise SmartError(
                f"refusing to run smartctl {flag}: not on the read-only "
                "allowlist in core/health/smart.py."
            )


def smartctl_path() -> str | None:
    """Where smartctl is, or ``None``. Bundled on Windows (§12), else on PATH."""
    return shutil.which("smartctl")


def available() -> bool:
    return smartctl_path() is not None


def run(
    device: str,
    *,
    arguments: list[str] | None = None,
    timeout: float = 30.0,
    runner=None,
) -> dict:
    """Read one device and return smartctl's decoded JSON.

    ``runner`` is injectable so the tests exercise this function rather than a
    reimplementation of it.
    """
    arguments = arguments if arguments is not None else ["--json", "-a", "-n", "standby"]
    check_arguments(arguments)

    executable = smartctl_path()
    if executable is None:
        raise SmartError(
            "smartctl was not found. Install smartmontools, or accept "
            "age-based estimates labelled 'estimated'."
        )

    argv = [executable, *arguments, device]
    invoke = runner or _default_runner
    completed = invoke(argv, timeout)

    # smartctl encodes findings in its exit status bitmask; low bits mean the
    # device is failing, which is a successful *read* of bad news, not an error.
    # Only a failure to communicate is an error here.
    if completed.stdout:
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SmartError(f"smartctl returned output that is not JSON: {exc}") from exc

    raise SmartError(
        completed.stderr.strip() or f"smartctl exited {completed.returncode} with no output"
    )


def _default_runner(argv: list[str], timeout: float):
    return subprocess.run(  # noqa: S603 - argv is built here; see check_arguments
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# -- parsing ------------------------------------------------------------------

def _int(value) -> int | None:
    """A count, or None. Never a silent zero."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


#: SMART attribute ids, which are stable across vendors far more reliably than
#: attribute names are.
ATTRIBUTE_IDS = {
    5: "reallocated",
    9: "power_on_hours",
    177: "wear_leveling",
    187: "uncorrectable",       # reported_uncorrect
    196: "reallocated_events",
    197: "pending",             # current_pending_sector
    198: "uncorrectable_offline",
    199: "crc_errors",
    231: "ssd_life_left",
    233: "nand_writes",
    241: "lbas_written",
}


def parse(document: dict) -> SmartReading:
    """Turn smartctl's JSON into a reading.

    Written against the shape smartctl actually emits for three device classes —
    SATA (``ata_smart_attributes``), NVMe (``nvme_smart_health_information_log``)
    and drives behind a bridge that pass nothing through. The last case is the
    common one for external USB drives and must produce an empty reading rather
    than an exception or a row of zeroes.
    """
    if not isinstance(document, dict):
        raise SmartError("smartctl JSON was not an object")

    device = document.get("device") or {}
    values: dict = {
        "model": document.get("model_name") or document.get("model_family"),
        "serial": document.get("serial_number"),
        "firmware": document.get("firmware_version"),
        "capacity_bytes": _int((document.get("user_capacity") or {}).get("bytes")),
        "rotation_rpm": _int(document.get("rotation_rate")),
    }

    status = document.get("smart_status") or {}
    if "passed" in status:
        values["passed"] = bool(status["passed"])

    # Media class. rotation_rate of 0 means solid state; smartctl says so
    # explicitly for NVMe via the device type.
    protocol = (device.get("type") or device.get("protocol") or "").lower()
    if "nvme" in protocol:
        values["media"] = "nvme"
    elif values["rotation_rpm"]:
        values["media"] = "hdd"
    elif values["rotation_rpm"] == 0:
        values["media"] = "ssd"

    power_on = document.get("power_on_time") or {}
    if "hours" in power_on:
        values["power_on_hours"] = _int(power_on["hours"])

    temperature = document.get("temperature") or {}
    if "current" in temperature:
        values["temperature_c"] = _int(temperature["current"])

    unparsed: dict = {}
    _parse_ata_attributes(document, values, unparsed)
    _parse_nvme_log(document, values)

    selftest = (document.get("ata_smart_data") or {}).get("self_test") or {}
    status_text = (selftest.get("status") or {}).get("string")
    if status_text:
        values["self_test_status"] = status_text

    values["unparsed"] = unparsed
    return SmartReading(**values)


def _parse_ata_attributes(document: dict, values: dict, unparsed: dict) -> None:
    table = (document.get("ata_smart_attributes") or {}).get("table") or []
    for entry in table:
        if not isinstance(entry, dict):
            continue
        attribute_id = _int(entry.get("id"))
        raw = entry.get("raw") or {}
        # Prefer the parsed raw value; fall back to the string smartctl prints,
        # whose first token is the count for every attribute we read.
        value = _int(raw.get("value"))
        if value is None:
            text = str(raw.get("string", "")).split()
            value = _int(text[0]) if text else None
        if value is None or attribute_id is None:
            continue

        name = ATTRIBUTE_IDS.get(attribute_id)
        if name == "reallocated":
            values["reallocated"] = value
        elif name == "pending":
            values["pending"] = value
        elif name == "uncorrectable":
            values["uncorrectable"] = value
        elif name == "crc_errors":
            values["crc_errors"] = value
        elif name == "wear_leveling":
            # Reported as percentage of life *remaining*; stored as consumed so
            # bigger always means worse, like every other counter here.
            values["wear_leveling"] = max(0, 100 - value) if value <= 100 else None
        elif name == "power_on_hours" and values.get("power_on_hours") is None:
            values["power_on_hours"] = value
        elif name == "lbas_written":
            # 512-byte logical blocks in the overwhelmingly common case.
            values["tbw_written"] = value * 512
        elif name is None:
            unparsed[str(attribute_id)] = entry.get("name") or value


def _parse_nvme_log(document: dict, values: dict) -> None:
    log = document.get("nvme_smart_health_information_log") or {}
    if not log:
        return
    if values.get("power_on_hours") is None:
        values["power_on_hours"] = _int(log.get("power_on_hours"))
    if values.get("temperature_c") is None:
        values["temperature_c"] = _int(log.get("temperature"))

    used = _int(log.get("percentage_used"))
    if used is not None:
        values["wear_leveling"] = used
    media_errors = _int(log.get("media_errors"))
    if media_errors is not None:
        values["uncorrectable"] = media_errors

    written = _int(log.get("data_units_written"))
    if written is not None:
        # NVMe counts 1000 512-byte units per data unit.
        values["tbw_written"] = written * 1000 * 512
