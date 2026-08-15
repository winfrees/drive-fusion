"""The read-only gateway: the only module permitted to touch catalogued media.

Everything the tool learns about a user's drives comes through this file. Its
entire public surface is ``scandir``, ``stat``, and ``open_read``. There is no
``open_write``, no ``remove``, and no ``rename`` to call — a caller cannot
modify user data through this module because the capability is not exposed.

Three properties are load-bearing (docs/PLAN.md §3.2):

1. Files are opened with read access only. On Windows that means ``CreateFileW``
   with ``GENERIC_READ`` and nothing else, so the handle is incapable of writing
   at the OS level regardless of what code above it does.
2. Sharing is permissive (``FILE_SHARE_READ | WRITE | DELETE``) so scanning
   never blocks another process from using, changing, or deleting its own file.
3. Cloud placeholder files are refused rather than opened, because opening one
   triggers a download.
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass
from typing import Callable, Iterator

from drivefusion.core.errors import (
    DehydratedFileError,
    ReadOnlyViolation,
    UnreadableError,
)

WINDOWS = sys.platform == "win32"

# Windows file attribute bits we care about. Defined unconditionally so that
# the predicates below are testable on any platform.
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

#: Attribute bits meaning "the bytes are not really here" — opening hydrates.
DEHYDRATED_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


def is_dehydrated(attributes: int) -> bool:
    """True when a file's attributes say reading it would trigger a recall."""
    return bool(attributes & DEHYDRATED_MASK)


def is_reparse_point(attributes: int) -> bool:
    """True for junctions, symlinks, and volume mount points."""
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True, slots=True)
class Entry:
    """One directory entry, captured without following links."""

    name: str
    path: str
    is_dir: bool
    is_symlink: bool
    attributes: int

    @property
    def is_reparse_point(self) -> bool:
        return is_reparse_point(self.attributes)

    @property
    def is_dehydrated(self) -> bool:
        return is_dehydrated(self.attributes)


@dataclass(frozen=True, slots=True)
class StatResult:
    """Metadata for one path. Sizes are bytes; times are nanoseconds."""

    size: int
    mtime_ns: int
    ctime_ns: int
    atime_ns: int
    is_dir: bool
    nlink: int
    attributes: int
    file_id: int | None = None

    @property
    def is_dehydrated(self) -> bool:
        return is_dehydrated(self.attributes)

    @property
    def is_reparse_point(self) -> bool:
        return is_reparse_point(self.attributes)


class ReadOnlyBinaryFile:
    """A binary reader that cannot write, by construction and by assertion.

    The underlying handle is already read-only at the OS level; this wrapper
    exists so that a caller's mistake surfaces as a loud, specific error at the
    call site instead of an ``AttributeError`` several frames away.
    """

    __slots__ = ("_raw", "_path")

    def __init__(self, raw: io.BufferedReader, path: str) -> None:
        self._raw = raw
        self._path = path

    # -- reading -----------------------------------------------------------
    def read(self, size: int = -1) -> bytes:
        return self._raw.read(size)

    def readinto(self, buffer) -> int | None:  # noqa: ANN001 - buffer protocol
        return self._raw.readinto(buffer)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def fileno(self) -> int:
        return self._raw.fileno()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return self._raw.seekable()

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        self._raw.close()

    @property
    def closed(self) -> bool:
        return self._raw.closed

    @property
    def path(self) -> str:
        return self._path

    def __enter__(self) -> "ReadOnlyBinaryFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self):
        return iter(self._raw)

    # -- refusals ----------------------------------------------------------
    def _refuse(self, operation: str):
        raise ReadOnlyViolation(
            f"{operation} attempted on read-only handle for {self._path!r}; "
            "Drive Fusion never writes to catalogued media"
        )

    def write(self, *_args: object, **_kwargs: object):
        self._refuse("write")

    def writelines(self, *_args: object, **_kwargs: object):
        self._refuse("writelines")

    def truncate(self, *_args: object, **_kwargs: object):
        self._refuse("truncate")


def long_path(path: str) -> str:
    """Return a path safe to pass to the Win32 API, however long it is.

    Windows APIs cap at MAX_PATH unless a path is prefixed, and archive drives
    routinely exceed 260 characters. On other platforms this is the identity.
    """
    if not WINDOWS:
        return path
    if path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _attributes_from_stat(st: os.stat_result) -> int:
    return int(getattr(st, "st_file_attributes", 0))


def _open_dir_fd_noatime(path: str) -> int | None:
    """Open a directory for listing without updating its access time.

    Listing a directory is a read, and a read should leave no trace. On POSIX
    that means listing through an ``O_NOATIME`` descriptor rather than by path,
    since the by-path form updates the directory's access time. Returns None
    when the platform or the file's ownership will not allow it, in which case
    the caller falls back to an ordinary listing.
    """
    flags = getattr(os, "O_NOATIME", 0) | getattr(os, "O_DIRECTORY", 0)
    if not flags:
        return None
    try:
        return os.open(path, os.O_RDONLY | flags)
    except OSError:
        return None


def scandir(path: str) -> Iterator[Entry]:
    """Yield the entries of one directory without following links.

    Links are reported, never traversed; callers decide what to do with them,
    so a junction loop cannot turn a bounded volume into an unbounded walk.
    """
    dir_fd = None if WINDOWS else _open_dir_fd_noatime(path)
    source: int | str = dir_fd if dir_fd is not None else long_path(path)
    try:
        try:
            with os.scandir(source) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        attributes = _attributes_from_stat(st)
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        # A file that vanished or denied us mid-listing is
                        # data, not a failure: report what we know, move on.
                        attributes = 0
                        is_dir = False
                    yield Entry(
                        name=entry.name,
                        # Built from the caller's path so results never carry
                        # the \\?\ prefix or a descriptor-relative name.
                        path=os.path.join(path, entry.name),
                        is_dir=is_dir,
                        is_symlink=entry.is_symlink(),
                        attributes=attributes,
                    )
        except OSError as exc:
            raise UnreadableError(f"cannot list {path!r}: {exc}") from exc
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def stat(path: str, *, follow_symlinks: bool = False) -> StatResult:
    """Metadata for one path, without following links by default."""
    try:
        st = os.stat(long_path(path), follow_symlinks=follow_symlinks)
    except OSError as exc:
        raise UnreadableError(f"cannot stat {path!r}: {exc}") from exc
    import stat as stat_module

    return StatResult(
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        ctime_ns=st.st_ctime_ns,
        atime_ns=st.st_atime_ns,
        is_dir=stat_module.S_ISDIR(st.st_mode),
        nlink=st.st_nlink,
        attributes=_attributes_from_stat(st),
        file_id=getattr(st, "st_ino", None) or None,
    )


def _open_fd_windows(path: str) -> int:
    """Open a read-only, fully shared handle and return a C file descriptor.

    ``GENERIC_READ`` alone is the point: a handle opened without write access
    cannot write, so the guarantee holds below the Python layer. Full sharing
    means scanning never interferes with another process's use of its file.
    """
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    handle = CreateFileW(
        long_path(path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        raise UnreadableError(
            f"cannot open {path!r}: WinError {ctypes.get_last_error()}"
        )
    return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))


def _open_fd_posix(path: str) -> int:
    """Open read-only, avoiding an access-time update where the OS permits it.

    ``O_NOATIME`` is only granted to a file's owner, so the fallback is normal
    read-only access. Windows disables access-time updates by default and the
    tool never calls ``SetFileTime``, so no equivalent is needed there.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    if noatime:
        try:
            return os.open(path, flags | noatime)
        except PermissionError:
            pass
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise UnreadableError(f"cannot open {path!r}: {exc}") from exc


def open_read(path: str, *, buffer_size: int = 1 << 20) -> ReadOnlyBinaryFile:
    """Open a file for reading, or refuse.

    Raises :class:`DehydratedFileError` for cloud placeholders — the one case
    where merely reading would change the user's storage.
    """
    info = stat(path)
    if info.is_dehydrated:
        raise DehydratedFileError(
            f"refusing to open cloud placeholder {path!r}: reading it would "
            "trigger a download. Catalog it from metadata instead."
        )
    if info.is_dir:
        raise UnreadableError(f"{path!r} is a directory")

    fd = _open_fd_windows(path) if WINDOWS else _open_fd_posix(path)
    try:
        raw = io.FileIO(fd, mode="rb", closefd=True)
        buffered = io.BufferedReader(raw, buffer_size=buffer_size)
    except Exception:
        os.close(fd)
        raise
    return ReadOnlyBinaryFile(buffered, path)


def walk(
    root: str,
    *,
    follow_reparse_points: bool = False,
    on_error: "Callable[[str, UnreadableError], None] | None" = None,
) -> Iterator[Entry]:
    """Depth-first iteration over a subtree, yielding files and directories.

    A placeholder for the real enumeration backends (docs/PLAN.md §6), present
    at M0 so the no-touch test exercises a genuine end-to-end read of a tree.
    Reparse points are reported but not traversed unless explicitly requested.

    A directory that cannot be listed does not abort the walk — on a real
    archive volume some paths will always be denied — but it is reported to
    ``on_error`` rather than silently dropped. Silence would let a scan claim
    complete coverage of a tree it only partly read, which is how a redundancy
    count ends up confidently wrong.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(scandir(current))
        except UnreadableError as exc:
            if on_error is not None:
                on_error(current, exc)
            continue
        for entry in entries:
            yield entry
            if entry.is_dir and (
                follow_reparse_points
                or not (entry.is_symlink or entry.is_reparse_point)
            ):
                stack.append(entry.path)
