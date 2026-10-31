"""Where the catalog lives (PLAN §12, ROADMAP M8.7, D20).

In order: ``--catalog`` (the caller's), ``DRIVEFUSION_CATALOG``, then
**portable mode** when a file named ``portable`` sits beside the executable
(the catalog goes in a ``DriveFusion data`` folder beside it, so the tool and
its catalog travel together on a thumb drive), then ``~/.drivefusion``.
Nothing is ever moved from one to another.

A portable catalog is still bound by REVIEW F-03: ``Catalog()`` refuses a
catalog on a drive it scans (unless that is the system drive), so a thumb
drive carrying the catalog can't also be in scope.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENVIRONMENT_VARIABLE = "DRIVEFUSION_CATALOG"
MARKER = "portable"
PORTABLE_FOLDER = "DriveFusion data"


def program_folder() -> Path:
    """The folder of the running executable (frozen builds) or of the package."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def portable(folder: Path | None = None) -> bool:
    return (folder or program_folder()).joinpath(MARKER).is_file()


def default_catalog(*, environ=None, folder: Path | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get(ENVIRONMENT_VARIABLE):
        return Path(environ[ENVIRONMENT_VARIABLE])
    folder = folder or program_folder()
    if portable(folder):
        return folder / PORTABLE_FOLDER / "catalog.db"
    return Path.home() / ".drivefusion" / "catalog.db"


def describe(path: Path, *, environ=None, folder: Path | None = None) -> str:
    """Why the catalog is where it is, for `status` and the About box."""
    environ = os.environ if environ is None else environ
    folder = folder or program_folder()
    if environ.get(ENVIRONMENT_VARIABLE) and Path(environ[ENVIRONMENT_VARIABLE]) == path:
        return f"from {ENVIRONMENT_VARIABLE}"
    if portable(folder) and path == folder / PORTABLE_FOLDER / "catalog.db":
        return f"portable mode ('{MARKER}' beside the program)"
    if path == Path.home() / ".drivefusion" / "catalog.db":
        return "the default"
    return "chosen with --catalog"
