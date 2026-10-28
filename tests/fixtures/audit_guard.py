"""A runtime guard for the no-touch test, built on ``sys.addaudithook``.

The no-touch snapshot proves the tree came out unchanged. This guard catches
the *attempt*, at the moment it is made, whichever API made it — including the
ones ``tools/ro_lint.py`` cannot see statically, such as a write reached
through name indirection (REVIEW F-08). The two are complementary: the
snapshot catches what the hook can't observe (a Win32 call through ``ctypes``
raises no audit event), and the hook catches what the snapshot can't
attribute (a file that was opened for writing and closed unchanged).

CPython never removes an audit hook, so one hook is installed for the whole
session and does nothing until a test enters :func:`guard`. While a guard is
active the hook watches every thread, which matters because the scanner and
the hasher do their reads on worker threads.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

#: Flags that let a descriptor create, change or truncate a file. O_TMPFILE is
#: tested on its own: on Linux it is ``__O_TMPFILE | O_DIRECTORY``, so masking
#: with it would mistake every read-only directory listing for a write.
_WRITE_FLAGS = 0
for _name in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL"):
    _WRITE_FLAGS |= getattr(os, _name, 0)
_O_TMPFILE = getattr(os, "O_TMPFILE", 0)


def _flags_write(flags: int) -> bool:
    return bool(flags & _WRITE_FLAGS) or bool(_O_TMPFILE and flags & _O_TMPFILE == _O_TMPFILE)

#: Events whose path arguments name something that would be changed.
_PATH_MUTATIONS = {
    "os.remove": 1, "os.rmdir": 1, "os.mkdir": 1, "os.truncate": 1,
    "os.chmod": 1, "os.chown": 1, "os.chflags": 1, "os.utime": 1,
    "os.setxattr": 1, "os.removexattr": 1, "os.rename": 2, "os.link": 2,
    "os.symlink": 2, "shutil.rmtree": 1, "shutil.copyfile": 2,
    "shutil.copymode": 2, "shutil.copystat": 2, "shutil.copytree": 2,
    "shutil.move": 2, "shutil.chown": 1, "shutil.make_archive": 1,
    "shutil.unpack_archive": 2, "sqlite3.connect": 1,
}

#: Events that start another process. Refused wherever they point: a process
#: can do anything, and nothing in a scan, hash or report cycle needs one.
_PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen", "os.system", "os.startfile", "os.posix_spawn",
        "os.exec", "os.spawn", "os.fork", "os.forkpty", "pty.spawn",
    }
)


class AuditViolation(AssertionError):
    """Raised inside the offending call, so the operation never happens."""


class _State:
    roots: tuple[str, ...] = ()
    violations: list[str] = []
    local = threading.local()


def _within(path: object) -> bool:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return False        # a descriptor or a database URI flag; not a path
    real = os.path.realpath(os.fspath(path))
    return any(real == root or real.startswith(root + os.sep) for root in _State.roots)


def _describe(event: str, args: tuple) -> str | None:
    if event in _PROCESS_EVENTS:
        return f"{event} {args[:2]!r}"
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, 0))[:3]
        writes = (
            any(c in mode for c in "wax+") if isinstance(mode, str)
            else _flags_write(flags or 0)
        )
        if writes and _within(path):
            return f"open {path!r} mode={mode!r} flags={flags!r}"
        return None
    count = _PATH_MUTATIONS.get(event)
    if count and any(_within(a) for a in args[:count]):
        return f"{event} {args[:count]!r}"
    return None


def _hook(event: str, args: tuple) -> None:
    if not _State.roots or getattr(_State.local, "busy", False):
        return
    _State.local.busy = True        # realpath must not re-enter the hook
    try:
        problem = _describe(event, args)
    finally:
        _State.local.busy = False
    if problem:
        _State.violations.append(problem)
        raise AuditViolation(f"attempted change to guarded media: {problem}")


_installed = False


def install() -> None:
    global _installed
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True


@contextmanager
def guard(*roots: os.PathLike | str) -> Iterator[list[str]]:
    """Refuse, and record, any attempt to change ``roots`` or start a process.

    Yields the list of violations, which the caller asserts is empty: an
    exception raised in the hook can be swallowed by the code under test, the
    record cannot.
    """
    install()
    if _State.roots:
        raise RuntimeError("audit guards do not nest")
    _State.roots = tuple(os.path.realpath(os.fspath(r)) for r in roots)
    _State.violations = []
    try:
        yield _State.violations
    finally:
        _State.roots = ()
