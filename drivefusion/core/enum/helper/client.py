"""Parent side of the enumeration helper.

Spawns the helper, reads its frames, and parses them with the same functions a
same-process read would use. Errors arrive as structured status lines on the
helper's stderr rather than as a bare exit code, so a failure can say *why*.

Elevation, honestly: this spawns the helper directly, which inherits the
parent's token. It therefore gets a volume handle when Drive Fusion is already
running elevated, and reports a clear reason when it is not. Prompting for
elevation from a non-elevated process needs ShellExecuteEx with the ``runas``
verb plus a named pipe to carry the output back, which belongs with the GUI
that can host the prompt (M4) rather than being written blind here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterator

from drivefusion.core.enum.helper.protocol import (
    ProtocolError,
    decode_status,
    read_frames,
)
from drivefusion.core.enum.records import UsnRecord, parse_usn_records

WINDOWS = sys.platform == "win32"

#: Name of the frozen helper, sitting beside the main executable.
HELPER_EXECUTABLE = "dfscan-helper.exe"


class HelperError(RuntimeError):
    """Raised when the helper could not run or failed."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HelperResult:
    records: list[UsnRecord] = field(default_factory=list)
    frames: int = 0
    next_cursor: int | None = None
    status: dict = field(default_factory=dict)


def is_elevated() -> bool:
    """True when this process can open a volume handle."""
    if WINDOWS:
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def helper_command() -> list[str]:
    """How to invoke the helper, frozen or from source."""
    if getattr(sys, "frozen", False):
        beside = os.path.join(os.path.dirname(sys.executable), HELPER_EXECUTABLE)
        return [beside]
    return [sys.executable, "-m", "drivefusion.core.enum.helper"]


def run_helper(
    volume_device_path: str,
    *,
    mode: str = "query",
    journal_id: int | None = None,
    next_usn: int | None = None,
    command: list[str] | None = None,
    timeout: float | None = None,
) -> HelperResult:
    """Run the helper and collect its records.

    ``command`` is injectable so the protocol can be exercised end to end
    against a stand-in child on any platform.
    """
    argv = list(command or helper_command())
    argv += ["--volume", volume_device_path, "--mode", mode]
    if journal_id is not None:
        argv += ["--journal-id", str(journal_id)]
    if next_usn is not None:
        argv += ["--next-usn", str(next_usn)]

    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        raise HelperError(f"cannot start the enumeration helper: {exc}", "spawn")

    result = HelperResult()
    protocol_error: ProtocolError | None = None
    try:
        for payload in read_frames(process.stdout):
            result.frames += 1
            result.records.extend(parse_usn_records(payload, offset=8))
            result.next_cursor = int.from_bytes(payload[:8], "little", signed=True)
    except ProtocolError as exc:
        protocol_error = exc

    returncode = process.wait(timeout=timeout)
    # Drain before closing: the status lines are the only place the helper can
    # say *why* it stopped, and an exit code cannot carry "you need
    # administrator rights".
    result.status = _drain_status(process)
    _close(process)

    reported = result.status.get("message")
    if returncode != 0 and reported:
        # The helper's own reason beats any symptom observed downstream: a
        # missing terminator is what an access failure *looks* like from here.
        raise HelperError(reported, result.status.get("code", "failed"))
    if protocol_error is not None:
        suffix = f" (helper exited {returncode})" if returncode else ""
        raise HelperError(f"{protocol_error}{suffix}", "protocol")
    if returncode != 0:
        raise HelperError(f"helper exited {returncode}", "failed")
    return result


def _close(process) -> None:
    for stream in (process.stdout, process.stderr):
        if stream and not stream.closed:
            try:
                stream.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _drain_status(process) -> dict:
    try:
        raw = process.stderr.read() if process.stderr else b""
    except (ValueError, OSError):
        raw = b""
    status: dict = {}
    for line in raw.splitlines():
        parsed = decode_status(line)
        if parsed:
            status.update(parsed)
    return status


def stream_records(
    volume_device_path: str, **kwargs
) -> Iterator[UsnRecord]:
    """Convenience wrapper for callers that only want the records."""
    yield from run_helper(volume_device_path, **kwargs).records
