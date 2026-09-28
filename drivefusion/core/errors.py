"""Exceptions raised by the read-only gateway."""

from __future__ import annotations


class DriveFusionError(Exception):
    """Base class for all Drive Fusion errors."""


class ReadOnlyViolation(DriveFusionError):
    """Raised when caller code attempts to write through a read-only handle.

    Reaching this exception means a caller tried to do something the tool is
    designed to be incapable of. It is never caught internally: it should crash
    the operation loudly rather than degrade quietly.
    """


class DehydratedFileError(DriveFusionError):
    """Raised when opening a file would trigger a cloud download.

    Cloud-backed placeholder files (OneDrive and similar) report their content
    as present, but reading one causes the provider to hydrate it — a state
    change on the user's storage and potentially a very large transfer. The
    gateway refuses to open them; callers catalog them from metadata instead.
    """


class UnreadableError(DriveFusionError):
    """Raised when a path cannot be read (permissions, I/O error, disconnection).

    Expected during scans and recorded per file rather than aborting a volume.
    """
