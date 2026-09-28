"""Content hashing: the two tiers that read bytes.

Hashing every byte of tens of terabytes is the failure mode that kills tools
like this, so the pipeline reads as little as it can get away with
(docs/PLAN.md §6.4):

* **Tier 0** compares metadata and reads nothing. Handled by the scanner.
* **Tier 1 — quick hash.** Size plus a few sampled blocks. Only files whose
  *size* occurs more than once anywhere in the catalog are candidates: a
  unique size implies unique content, so most files are never opened at all.
* **Tier 2 — full hash.** Only where quick hashes collide. This is the only
  tier that reads whole files, over a small fraction of total bytes.

Everything here reads through the read-only gateway, and nothing here decides
*which* files to read — that is the pipeline's job.
"""

from __future__ import annotations

import hashlib
from typing import Callable

from drivefusion.core import fsio

#: Bytes sampled at each of head, midpoint, and tail.
SAMPLE_BYTES = 64 * 1024

#: Below this, sampling three blocks would read most of the file anyway, so
#: the quick hash just reads all of it and is exact.
SMALL_FILE_BYTES = 3 * SAMPLE_BYTES

#: Streaming chunk for full hashes. Large enough to keep syscalls rare.
STREAM_BYTES = 1024 * 1024


def _select_algorithm() -> tuple[str, Callable]:
    """BLAKE3 when available, BLAKE2b otherwise.

    BLAKE3 is several times faster and multithreads; BLAKE2b keeps the tool
    dependency-free. The name is stored per content row so a catalog written
    by one build stays readable by the other.
    """
    try:
        import blake3  # type: ignore

        return "blake3", lambda: blake3.blake3()
    except ImportError:
        return "blake2b", lambda: hashlib.blake2b(digest_size=32)


HASH_ALGO, _new_digest = _select_algorithm()


def new_digest():
    return _new_digest()


def quick_hash(path: str, size: int) -> bytes:
    """Sample a file cheaply enough to rule out most non-duplicates.

    The size is folded into the digest as well as stored alongside it, so two
    files that happen to share sampled blocks but differ in length can never
    collide. For files at or below the small-file threshold this reads the
    whole file, which makes the quick hash exact for them — a useful property,
    since small files dominate most archives by count.
    """
    digest = new_digest()
    digest.update(size.to_bytes(8, "little"))

    with fsio.open_read(path) as handle:
        if size <= SMALL_FILE_BYTES:
            while chunk := handle.read(STREAM_BYTES):
                digest.update(chunk)
            return digest.digest()

        digest.update(handle.read(SAMPLE_BYTES))

        handle.seek(size // 2)
        digest.update(handle.read(SAMPLE_BYTES))

        handle.seek(max(0, size - SAMPLE_BYTES))
        digest.update(handle.read(SAMPLE_BYTES))

    return digest.digest()


def full_hash(path: str) -> tuple[bytes, int]:
    """Hash an entire file. Returns the digest and the bytes actually read.

    The byte count is returned rather than assumed from the catalog: a file
    that changed size between the scan and the hash would otherwise be
    recorded with a length it no longer has.
    """
    digest = new_digest()
    read = 0
    with fsio.open_read(path) as handle:
        while chunk := handle.read(STREAM_BYTES):
            digest.update(chunk)
            read += len(chunk)
    return digest.digest(), read


def is_quick_hash_exact(size: int) -> bool:
    """True when the quick hash covered every byte, so no full hash is needed."""
    return size <= SMALL_FILE_BYTES
