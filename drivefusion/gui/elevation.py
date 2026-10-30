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
* **Only the genuine helper is ever launched, by absolute path, resolved from
  our own executable's directory.** Never a shell, never a name looked up on
  PATH, never a path from the catalog or a config file. A ``runas`` launch of
  an attacker-supplied file would hand them Administrator, so the file is
  *proven* before the prompt (docs/REVIEW.md F-07): the build pins the
  helper's SHA-256 inside the main executable, and the helper is hashed through
  a handle that denies writing and deleting to everyone else, kept open until
  Windows has started it, so it can't be swapped between check and launch. A
  build without a pin refuses to elevate a helper in a folder the user can
  write to.
* **Arguments are quoted for the command line.** ``ShellExecuteW`` takes the
  parameters as one string, which the helper splits again, so they are built
  with ``subprocess.list2cmdline``: an argument with a space or a quote stays
  one argument.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from drivefusion.core import pinned
from drivefusion.core.enum.helper.client import HELPER_EXECUTABLE, is_elevated
from drivefusion.core.pinned import PinError
from drivefusion.core.pinned import check as _pinned_check

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


def pinned_helper_sha256() -> str | None:
    """The helper's SHA-256 as the build recorded it, or None for an unpinned
    build (a source checkout). See ``core/pinned.py``."""
    return pinned.pinned_sha256("HELPER_SHA256")


#: Opens the helper denying others write and delete access (``core/pinned.py``).
_locked = pinned.locked


def check_helper(path: Path, digest: str, *, pinned: str | None, writable: bool) -> None:
    """Raise :class:`ElevationError` unless ``path`` may be run as Administrator.

    ``digest`` is the SHA-256 of the file as read through the locked handle.
    """
    try:
        _pinned_check(path, digest, pinned=pinned, writable=writable, what="helper")
    except PinError as exc:
        raise ElevationError(str(exc)) from None


def helper_parameters(arguments: list[str] | None) -> str | None:
    """One command-line string the helper will split back into ``arguments``."""
    return subprocess.list2cmdline(arguments) if arguments else None


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

    parameters = helper_parameters(arguments)
    with _locked(path) as handle:
        # Hashed through the locked handle, and launched while it is still
        # held: the bytes checked are the bytes Windows starts.
        check_helper(path, pinned.digest(handle), pinned=pinned_helper_sha256(),
                     writable=pinned.user_can_write(path.parent))

        import ctypes

        result = int(
            ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",            # the verb that raises the consent prompt
                str(path),
                parameters,
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


def restart_elevated(arguments: list[str] | None = None) -> None:
    """Start this Drive Fusion again with administrator rights, after the user
    asked to (ROADMAP M5.3: reading SMART needs them). Windows shows its own
    consent prompt; the main application never elevates without one.

    Only an installed (frozen) build can do this: from a source checkout there
    is no executable of ours to restart, so :class:`ElevationError` says how
    to start it as administrator instead.
    """
    if not WINDOWS or not getattr(sys, "frozen", False):
        raise ElevationError(
            "Start Drive Fusion as administrator yourself: right-click it and choose "
            "\"Run as administrator\" (from a source checkout, run it from an "
            "Administrator terminal)."
        )
    import ctypes

    result = int(ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, helper_parameters(arguments), None, 1))
    if result == 5:
        raise ElevationError("Administrator rights were declined; nothing changed.")
    if result <= 32:
        raise ElevationError(f"Windows could not restart Drive Fusion (code {result}).")


def status_line(state: ElevationState) -> str:
    """One line for the status bar."""
    if state.decision is Decision.ALREADY_ELEVATED:
        return "Administrator — change-journal rescans available"
    if state.decision is Decision.CAN_PROMPT:
        return "Standard user — full re-walk on every rescan"
    if state.decision is Decision.HELPER_MISSING:
        return "Standard user — helper not installed"
    return f"{os.name} — directory walk"
