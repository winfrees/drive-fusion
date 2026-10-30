"""Running a program only if it is the one the build shipped (REVIEW F-07).

Two bundled executables run with administrator rights or near them: the
enumeration helper, and smartctl (ROADMAP M5.3). The build records each one's
SHA-256 in a module packed inside the main executables' archive
(``drivefusion_helper_pin``, written by ``packaging/drivefusion.spec``), so
replacing a bundled file can't replace its pin with it.

Before running one, its caller opens it with :func:`locked`, hashes it
through that handle, checks the hash with :func:`check`, and runs it while
still holding the handle. Windows refuses any other open for writing or
deleting while the handle is held, so the bytes checked are the bytes run.
"""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path

WINDOWS = sys.platform == "win32"


class PinError(RuntimeError):
    """A bundled program can't be shown to be the one the build shipped."""


def pinned_sha256(name: str) -> str | None:
    """The SHA-256 the build recorded under ``name`` (e.g. ``HELPER_SHA256``),
    or None for a build without one (a source checkout)."""
    try:
        import drivefusion_helper_pin as pins  # type: ignore[import-not-found]
    except ImportError:
        return None
    value = getattr(pins, name, None)
    return value.lower() if value else None


def user_can_write(directory: Path) -> bool:
    """Whether this process can write to ``directory``: if it can, so can
    anything else running as this user."""
    return os.access(directory, os.W_OK)


@contextmanager
def locked(path: Path):
    """Open ``path`` for reading, denying everyone else write and delete
    access for as long as the handle is held.

    Elsewhere than Windows it is a plain read: the bundled programs are
    Windows-only, and the checks around this run in tests on any platform.
    """
    if not WINDOWS:
        with open(path, "rb") as handle:
            yield handle
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001        # read only: no FILE_SHARE_WRITE or _DELETE
    OPEN_EXISTING = 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ, None,
                                  OPEN_EXISTING, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise PinError(
            f"cannot open {path} exclusively (WinError {ctypes.get_last_error()}); "
            "something else has it open for writing"
        )
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with open(fd, "rb", closefd=True) as reader:
        yield reader


def digest(handle) -> str:
    sha = hashlib.sha256()
    while chunk := handle.read(1 << 20):
        sha.update(chunk)
    return sha.hexdigest()


def check(path: Path, actual: str, *, pinned: str | None, writable: bool, what: str) -> None:
    """Raise :class:`PinError` unless ``path`` may be run.

    A pinned build runs only the exact file it shipped. A build with no pin
    runs nothing from a folder the user can write to, since anything running
    as the user could have put it there.
    """
    if pinned is not None:
        if actual.lower() != pinned:
            raise PinError(
                f"refusing to run {path}: it is not the {what} this build shipped "
                "(its SHA-256 differs). Reinstall Drive Fusion."
            )
        return
    if writable:
        raise PinError(
            f"refusing to run {path}: this build records no fingerprint of its "
            f"{what}, and the folder it is in can be written to without "
            "administrator rights, so the file can't be trusted. Install Drive "
            "Fusion in a protected folder such as Program Files, or use a release "
            "build."
        )
