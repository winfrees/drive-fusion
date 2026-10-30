"""What a simulated drive's smartctl would print (ROADMAP M5.7).

The JSON has the shape of smartctl 7.x ``--json`` output, so Drive Fusion's
own parser reads it. No smartctl runs: the adapter hands this to the tool in
place of the process's output and records the command the tool would have run.
"""

from __future__ import annotations

import json

#: The sector counts a failing simulated drive reports.
FAILING = {"reallocated": 48, "pending": 8, "uncorrectable": 2, "crc_errors": 0}


def _attr(ident: int, name: str, raw: int, value: int = 100) -> dict:
    return {"id": ident, "name": name, "value": value, "raw": {"value": raw}}


def output(kind: str, *, model: str, serial: str, power_on_hours: int) -> str:
    meta = {"version": [7, 5], "exit_status": 0}
    if kind == "hidden":
        meta.update(exit_status=1, messages=[
            {"string": "/dev/sdb: Unknown USB bridge [0x152d:0x0578 (0x214)]",
             "severity": "error"}])
        return json.dumps({"smartctl": meta})
    counts = FAILING if kind == "failing" else dict.fromkeys(FAILING, 0)
    return json.dumps({
        "smartctl": meta, "model_name": model, "serial_number": serial,
        "smart_status": {"passed": kind != "failing"},
        "power_on_time": {"hours": power_on_hours},
        "temperature": {"current": 35},
        "ata_smart_attributes": {"table": [
            _attr(5, "Reallocated_Sector_Ct", counts["reallocated"]),
            _attr(9, "Power_On_Hours", power_on_hours),
            _attr(197, "Current_Pending_Sector", counts["pending"]),
            _attr(198, "Offline_Uncorrectable", counts["uncorrectable"]),
            _attr(199, "UDMA_CRC_Error_Count", counts["crc_errors"]),
        ]},
    })
