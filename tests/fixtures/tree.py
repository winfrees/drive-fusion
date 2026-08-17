"""Synthetic fixture trees and the Merkle snapshot used by the no-touch test.

Test code is exempt from the read-only lint — it has to create the fixtures it
then proves were left untouched.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

#: A file large enough to exercise buffered, multi-chunk reads.
LARGE_FILE_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True)
class Snapshot:
    """A content-and-metadata fingerprint of a whole tree."""

    digest: str
    entries: dict[str, tuple]
    atimes: dict[str, int]

    def diff(self, other: "Snapshot") -> list[str]:
        """Human-readable differences, ignoring access times."""
        problems: list[str] = []
        for path in sorted(set(self.entries) | set(other.entries)):
            before = self.entries.get(path)
            after = other.entries.get(path)
            if before is None:
                problems.append(f"created: {path}")
            elif after is None:
                problems.append(f"deleted: {path}")
            elif before != after:
                fields = ("kind", "size", "mtime_ns", "mode", "sha256")
                changed = [
                    f"{name} {b!r} -> {a!r}"
                    for name, b, a in zip(fields, before, after)
                    if b != a
                ]
                problems.append(f"modified: {path} ({', '.join(changed)})")
        return problems


def build_tree(root: Path) -> Path:
    """Create a deterministic tree with the shapes that matter for scanning."""
    root.mkdir(parents=True, exist_ok=True)

    (root / "docs").mkdir()
    (root / "docs" / "nested").mkdir()
    (root / "media").mkdir()
    (root / "empty-dir").mkdir()

    # Identical content in two places: the duplicate-detection case.
    duplicate = b"identical content across two paths\n" * 64
    (root / "docs" / "report.txt").write_bytes(duplicate)
    (root / "media" / "report-copy.txt").write_bytes(duplicate)

    # Same size, different content: the quick-hash collision case.
    (root / "docs" / "same-size-a.bin").write_bytes(b"A" * 4096)
    (root / "docs" / "same-size-b.bin").write_bytes(b"B" * 4096)

    (root / "docs" / "empty.txt").write_bytes(b"")
    (root / "docs" / "nested" / "é中文-ünicode.txt").write_bytes(
        "unicode names must survive the round trip\n".encode()
    )

    # Deterministic pseudo-random bytes, large enough to span read buffers.
    filler = bytearray()
    seed = 0x9E3779B9
    while len(filler) < LARGE_FILE_BYTES:
        seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
        filler.extend(seed.to_bytes(4, "little"))
    (root / "media" / "large.bin").write_bytes(bytes(filler[:LARGE_FILE_BYTES]))

    # A deep path, to catch anything that assumes short paths.
    deep = root / "deep"
    for level in range(12):
        deep = deep / f"level-{level:02d}-with-a-reasonably-long-name"
    deep.mkdir(parents=True)
    (deep / "bottom.txt").write_bytes(b"bottom\n")

    # Links are reported but never traversed; creating one may require
    # privilege on Windows, so its absence is not a test failure.
    try:
        os.symlink(root / "docs", root / "docs-link", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pass

    return root


def _noatime_open(path: Path, extra_flags: int = 0) -> int:
    """Open read-only without updating the access time, where permitted."""
    flags = os.O_RDONLY | extra_flags
    noatime = getattr(os, "O_NOATIME", 0)
    if noatime:
        try:
            return os.open(path, flags | noatime)
        except OSError:
            pass
    return os.open(path, flags)


class EmptySnapshotError(RuntimeError):
    """Raised when a snapshot finds nothing — the instrument is broken.

    An empty snapshot compares equal to another empty snapshot, which would
    make every no-touch assertion pass without examining anything. That is the
    single most dangerous way this test suite could fail, so it is an error
    rather than a quiet zero.
    """


def _read_bytes_noatime(path: Path) -> bytes:
    fd = _noatime_open(path, getattr(os, "O_BINARY", 0))
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _list_dir(directory: Path) -> list[tuple[str, bool]]:
    """List one directory as (name, is_dir), preferring not to touch its atime.

    Where the platform supports ``O_NOATIME`` and ``O_DIRECTORY`` (Linux), the
    listing goes through a descriptor so the directory's access time is left
    alone. Windows has neither flag — and ``os.open`` on a directory fails
    there outright — so it lists by path, which is fine because Windows has
    disabled last-access updates by default since Vista.

    ``is_dir`` is resolved inside the descriptor's lifetime: ``DirEntry`` from
    an fd-based scan resolves its stat relative to that fd, so it must not
    outlive it.
    """
    noatime = getattr(os, "O_NOATIME", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)

    if noatime and directory_flag:
        try:
            fd = os.open(directory, os.O_RDONLY | directory_flag | noatime)
        except PermissionError:
            fd = None  # not the owner; fall back to a by-path listing
        if fd is not None:
            try:
                with os.scandir(fd) as it:
                    return sorted(
                        (entry.name, entry.is_dir(follow_symlinks=False))
                        for entry in it
                    )
            finally:
                os.close(fd)

    with os.scandir(directory) as it:
        return sorted(
            (entry.name, entry.is_dir(follow_symlinks=False)) for entry in it
        )


def _iter_paths(root: Path) -> list[Path]:
    """Every path beneath root, without disturbing access times where possible.

    The snapshot must not perturb what it measures. An ordinary ``os.walk``
    updates each directory's access time as it lists it, which would show up as
    a difference between two snapshots and mask — or fake — a real one. This
    is deliberately a separate implementation from ``drivefusion.core.fsio`` so
    the no-touch test is not validating the gateway against itself.

    Failures are raised, never skipped. A snapshot that quietly omits part of
    the tree still compares equal to itself, so silence here would disarm the
    entire no-touch assertion.
    """
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for name, is_dir in _list_dir(directory):
            full = directory / name
            found.append(full)
            if is_dir:
                stack.append(full)
    return found


def snapshot(root: Path) -> Snapshot:
    """Fingerprint every path, its metadata, and its bytes.

    Runs in two passes: one that reads content without touching access times,
    then one that records access times via ``lstat``, which never updates them.
    Two consecutive snapshots of an untouched tree are therefore identical.
    """
    entries: dict[str, tuple] = {}
    atimes: dict[str, int] = {}

    paths = _iter_paths(root)
    if not paths:
        raise EmptySnapshotError(
            f"snapshot of {root} found no entries; an empty snapshot would "
            "make every no-touch assertion vacuous"
        )

    for full in paths:
        rel = full.relative_to(root).as_posix()
        st = os.lstat(full)
        if os.path.islink(full):
            kind = "symlink"
            content = hashlib.sha256(
                os.readlink(full).encode("utf-8", "surrogateescape")
            ).hexdigest()
            size = 0
        elif os.path.isdir(full):
            kind = "dir"
            content = ""
            size = 0
        else:
            kind = "file"
            content = hashlib.sha256(_read_bytes_noatime(full)).hexdigest()
            size = st.st_size
        entries[rel] = (kind, size, st.st_mtime_ns, st.st_mode, content)

    # Second pass: access times, after all reading is done.
    for full in paths:
        atimes[full.relative_to(root).as_posix()] = os.lstat(full).st_atime_ns

    accumulator = hashlib.sha256()
    for rel in sorted(entries):
        accumulator.update(rel.encode("utf-8", "surrogateescape"))
        accumulator.update(b"\0")
        accumulator.update(repr(entries[rel]).encode())
        accumulator.update(b"\0")

    return Snapshot(digest=accumulator.hexdigest(), entries=entries, atimes=atimes)
