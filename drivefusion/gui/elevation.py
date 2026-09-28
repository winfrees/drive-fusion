"""Asking Windows for administrator rights, and when not to.

Deferred here from M2 (``core/enum/helper/client.py``) because prompting needs a
window to host the prompt. The helper needs elevation for one thing only: opening
a raw volume handle to read the NTFS change journal. Everything else — directory
enumeration, hashing, the whole catalog — runs unprivileged.

Three rules, because this is the one place the tool asks for more power than most
of its work needs:

* **Elevation is optional, and the tool states what is lost without it.**
  Declining costs a faster rescan on NTFS, nothing else. A dialog implying the
  program is broken without administrator rights would be false.
* **Only the helper is ever launched, by absolute path, resolved from our own
  executable's directory.** Never a shell, never a name looked up on PATH, never
  a path from the catalog or a config file. A ``runas`` launch of an
  attacker-supplied path would hand them Administrator, so the path is derived
  and then verified rather than accepted.
* **Arguments are passed as arguments.** ``ShellExecuteW`` takes the file and its
  parameters as separate strings, so there is no command line for a quoting
  mistake to escape from.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from drivefusion.core.enum.helper.client import HELPER_EXECUTABLE, is_elevated

WINDOWS = sys.platform == "win32"

#: Shown wherever elevation is offered. States the benefit and the cost.
RATIONALE = (
    "Reading the NTFS change journal needs administrator rights. With them, "
    "rescanning a drive that has not changed much takes seconds instead of a "
    "full re-walk.\n\n"
    "Without them Drive Fusion still catalogs everything — it just re-walks each "
    "drive every time. Nothing else is affected, and nothing on your drives is "
    "written to either way."
)


class Decision(Enum):
    """What can usefully be done about elevation right now."""

    NOT_WINDOWS = "not-windows"
    ALREADY_ELEVATED = "already-elevated"
    CAN_PROMPT = "can-prompt"
    HELPER_MISSING = "helper-missing"


@dataclass(frozen=True)
class ElevationState:
    decision: Decision
    helper_path: Path | None
    detail: str

    @property
    def can_prompt(self) -> bool:
        return self.decision is Decision.CAN_PROMPT

    @property
    def journal_available(self) -> bool:
        """True when a delta rescan can actually be attempted."""
        return self.decision is Decision.ALREADY_ELEVATED


def helper_path() -> Path:
    """Where the helper must live: beside this executable, nowhere else.

    Frozen builds put ``dfscan-helper.exe`` next to ``drivefusion.exe``. From a
    source checkout there is no frozen helper, and the returned path simply will
    not exist — which is reported rather than worked around by searching.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    elif sys.argv and sys.argv[0]:
        base = Path(sys.argv[0]).resolve().parent
    else:
        base = Path.cwd()
    return base / HELPER_EXECUTABLE


def assess(
    *,
    windows: bool | None = None,
    elevated: bool | None = None,
    helper: Path | None = None,
) -> ElevationState:
    """Decide what to offer. Pure, so every branch is testable off Windows."""
    windows = WINDOWS if windows is None else windows
    if not windows:
        return ElevationState(
            Decision.NOT_WINDOWS,
            None,
            "Change-journal reads are a Windows feature; scans here always walk "
            "directories.",
        )

    elevated = is_elevated() if elevated is None else elevated
    if elevated:
        return ElevationState(
            Decision.ALREADY_ELEVATED,
            helper,
            "Running with administrator rights; change-journal rescans are "
            "available.",
        )

    path = helper if helper is not None else helper_path()
    if not path.exists():
        return ElevationState(
            Decision.HELPER_MISSING,
            path,
            f"The helper ({HELPER_EXECUTABLE}) is not installed beside this "
            "program, so elevation would have nothing to run. Scans will walk "
            "directories instead.",
        )

    return ElevationState(Decision.CAN_PROMPT, path, RATIONALE)


class ElevationError(RuntimeError):
    """Raised when a prompt could not be shown, or was declined."""


def request(state: ElevationState, arguments: list[str] | None = None) -> int:
    """Launch the helper elevated, showing Windows' own consent prompt.

    The user declining is an ordinary outcome, reported as
    :class:`ElevationError` rather than as a crash: the tool carries on
    unprivileged.
    """
    if not state.can_prompt or state.helper_path is None:
        raise ElevationError(state.detail)

    path = state.helper_path.resolve()
    if path.name != HELPER_EXECUTABLE:
        # Defence in depth: nothing but the helper is ever run with elevation.
        raise ElevationError(f"refusing to elevate {path.name}")
    if not path.is_file():
        raise ElevationError(f"helper not found at {path}")

    import ctypes

    parameters = " ".join(arguments or [])
    result = int(
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",            # the verb that raises the consent prompt
            str(path),
            parameters or None,
            str(path.parent),
            0,                  # SW_HIDE: the helper has no window of its own
        )
    )
    # ShellExecuteW returns <= 32 for failure; 5 is ERROR_ACCESS_DENIED, which is
    # what a declined prompt looks like.
    if result == 5:
        raise ElevationError(
            "Elevation was declined. Drive Fusion will keep cataloging without "
            "it; rescans will re-walk each drive."
        )
    if result <= 32:
        raise ElevationError(f"Windows refused to start the helper (code {result}).")
    return result


def status_line(state: ElevationState) -> str:
    """One line for the status bar."""
    if state.decision is Decision.ALREADY_ELEVATED:
        return "Administrator — change-journal rescans available"
    if state.decision is Decision.CAN_PROMPT:
        return "Standard user — full re-walk on every rescan"
    if state.decision is Decision.HELPER_MISSING:
        return "Standard user — helper not installed"
    return f"{os.name} — directory walk"
