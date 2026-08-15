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


def _read_bytes_noatime(path: Path) -> bytes:
    fd = _noatime_open(path)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _iter_paths(root: Path) -> list[Path]:
    """List every path beneath root without disturbing access times.

    The snapshot must not perturb what it measures. An ordinary ``os.walk``
    updates each directory's access time as it lists it, which would show up as
    a difference between two snapshots and mask — or fake — a real one. This
    is deliberately a separate implementation from ``drivefusion.core.fsio`` so
    the no-touch test is not validating the gateway against itself.
    """
    found: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            fd = _noatime_open(directory, getattr(os, "O_DIRECTORY", 0))
        except OSError:
            continue
        try:
            with os.scandir(fd) as it:
                entries = sorted(it, key=lambda e: e.name)
                for entry in entries:
                    full = directory / entry.name
                    found.append(full)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(full)
        finally:
            os.close(fd)
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
