"""Tiered hashing: what gets opened, what does not, and what identity means."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drivefusion.core import fsio
from drivefusion.core.identity import hasher
from drivefusion.core.identity.pipeline import hash_pass, verify_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def scan(catalog: Catalog, root: Path, guid: str = "id:1") -> int:
    volume_id = catalog.upsert_volume(volume_guid=guid, fs_type="ntfs")
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)
    return volume_id


def content_of(catalog: Catalog, name: str):
    return catalog.conn.execute(
        "SELECT content_id FROM file WHERE name = ?", (name,)
    ).fetchone()["content_id"]


# -- the hashes themselves ----------------------------------------------------

def test_quick_hash_distinguishes_same_size_different_bytes(tmp_path: Path) -> None:
    big = hasher.SMALL_FILE_BYTES * 4
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"A" * big)
    b.write_bytes(b"B" * big)

    assert hasher.quick_hash(str(a), big) != hasher.quick_hash(str(b), big)


def test_quick_hash_matches_for_identical_content(tmp_path: Path) -> None:
    big = hasher.SMALL_FILE_BYTES * 4
    payload = bytes(range(256)) * (big // 256)
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(payload)
    b.write_bytes(payload)

    assert hasher.quick_hash(str(a), len(payload)) == hasher.quick_hash(
        str(b), len(payload)
    )


def test_quick_hash_folds_in_size(tmp_path: Path) -> None:
    """Same sampled blocks, different length, must not collide."""
    short = tmp_path / "short.bin"
    long = tmp_path / "long.bin"
    short.write_bytes(b"x" * 100)
    long.write_bytes(b"x" * 200)

    assert hasher.quick_hash(str(short), 100) != hasher.quick_hash(str(long), 200)


def test_quick_hash_is_exact_for_small_files(tmp_path: Path) -> None:
    """Below the threshold it reads everything, so no full pass is needed."""
    assert hasher.is_quick_hash_exact(hasher.SMALL_FILE_BYTES)
    assert not hasher.is_quick_hash_exact(hasher.SMALL_FILE_BYTES + 1)

    small = tmp_path / "s.bin"
    small.write_bytes(b"tiny")
    # Differing only in the middle, which sampling could miss on a big file.
    other = tmp_path / "o.bin"
    other.write_bytes(b"tyni")
    assert hasher.quick_hash(str(small), 4) != hasher.quick_hash(str(other), 4)


def test_quick_hash_catches_a_middle_only_difference(tmp_path: Path) -> None:
    """The midpoint sample exists precisely for this case."""
    size = hasher.SMALL_FILE_BYTES * 4
    payload = bytearray(b"z" * size)
    a = tmp_path / "a.bin"
    a.write_bytes(bytes(payload))

    payload[size // 2 : size // 2 + 16] = b"different bytes!"
    b = tmp_path / "b.bin"
    b.write_bytes(bytes(payload))

    assert hasher.quick_hash(str(a), size) != hasher.quick_hash(str(b), size)


def test_full_hash_reports_bytes_read(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"q" * 5000)
    digest, read = hasher.full_hash(str(target))
    assert read == 5000
    assert len(digest) == 32


def test_hashing_refuses_cloud_placeholders(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "p.bin"
    target.write_bytes(b"x" * 100)
    real_stat = fsio.stat

    def dehydrated(path: str, **kwargs):
        info = real_stat(path, **kwargs)
        return type(info)(
            size=info.size, mtime_ns=info.mtime_ns, ctime_ns=info.ctime_ns,
            atime_ns=info.atime_ns, is_dir=info.is_dir, nlink=info.nlink,
            attributes=fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            file_id=info.file_id,
        )

    monkeypatch.setattr(fsio, "stat", dehydrated)
    from drivefusion.core.errors import DehydratedFileError

    with pytest.raises(DehydratedFileError):
        hasher.quick_hash(str(target), 100)


# -- the pipeline -------------------------------------------------------------

def test_unique_sizes_are_never_opened(catalog: Catalog, tmp_path: Path) -> None:
    """The property the whole design rests on: a unique size implies unique
    content, so those files cost nothing to rule out."""
    root = tmp_path / "vol"
    root.mkdir()
    (root / "one.bin").write_bytes(b"a" * 111)
    (root / "two.bin").write_bytes(b"b" * 222)
    (root / "three.bin").write_bytes(b"c" * 333)
    scan(catalog, root)

    opened: list[str] = []
    original = fsio.open_read

    def tracking(path: str, **kwargs):
        opened.append(path)
        return original(path, **kwargs)

    import drivefusion.core.identity.hasher as hasher_module

    hasher_module.fsio.open_read = tracking
    try:
        counters = hash_pass(catalog)
    finally:
        hasher_module.fsio.open_read = original

    assert counters.candidates == 0
    assert opened == []


def test_identical_files_share_one_content_row(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "vol"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    payload = b"identical bytes" * 40
    (root / "a" / "x.bin").write_bytes(payload)
    (root / "b" / "y.bin").write_bytes(payload)
    scan(catalog, root)

    counters = hash_pass(catalog)

    assert counters.candidates == 2
    assert content_of(catalog, "x.bin") == content_of(catalog, "y.bin")


def test_same_size_different_bytes_stay_distinct(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "vol"
    root.mkdir()
    (root / "a.bin").write_bytes(b"A" * 900)
    (root / "b.bin").write_bytes(b"B" * 900)
    scan(catalog, root)

    hash_pass(catalog)

    assert content_of(catalog, "a.bin") != content_of(catalog, "b.bin")


def test_large_same_size_files_are_separated_by_the_full_pass(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Files whose sampled blocks match must not be merged on the quick hash."""
    root = tmp_path / "vol"
    root.mkdir()
    size = hasher.SMALL_FILE_BYTES * 4
    shared = bytearray(b"s" * size)

    a = bytearray(shared)
    b = bytearray(shared)
    # Differ only in a region no sample touches: a quarter of the way in.
    a[size // 4 : size // 4 + 8] = b"AAAAAAAA"
    b[size // 4 : size // 4 + 8] = b"BBBBBBBB"
    (root / "a.bin").write_bytes(bytes(a))
    (root / "b.bin").write_bytes(bytes(b))
    scan(catalog, root)

    counters = hash_pass(catalog)

    assert counters.full_hashed == 2, "the collision should have forced a full read"
    assert content_of(catalog, "a.bin") != content_of(catalog, "b.bin")


def test_full_pass_is_skipped_when_the_quick_hash_was_exact(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Small files are already fully read; re-reading them is pure cost."""
    root = tmp_path / "vol"
    root.mkdir()
    payload = b"small identical" * 10
    (root / "a.bin").write_bytes(payload)
    (root / "b.bin").write_bytes(payload)
    scan(catalog, root)

    counters = hash_pass(catalog)

    assert counters.quick_hashed == 2
    assert counters.full_hashed == 0
    assert content_of(catalog, "a.bin") == content_of(catalog, "b.bin")


def test_unreadable_files_keep_no_identity(catalog: Catalog, tmp_path: Path) -> None:
    """A file that could not be hashed must not be folded in with anything."""
    root = tmp_path / "vol"
    root.mkdir()
    (root / "a.bin").write_bytes(b"x" * 500)
    (root / "b.bin").write_bytes(b"y" * 500)
    scan(catalog, root)

    real = hasher.quick_hash

    def failing(path: str, size: int):
        if path.endswith("b.bin"):
            raise OSError("access denied (simulated)")
        return real(path, size)

    import drivefusion.core.identity.pipeline as pipeline_module

    pipeline_module.hasher.quick_hash = failing
    try:
        counters = hash_pass(catalog)
    finally:
        pipeline_module.hasher.quick_hash = real

    assert counters.unreadable == 1
    assert content_of(catalog, "a.bin") is not None
    assert content_of(catalog, "b.bin") is None


def test_hashing_is_idempotent(catalog: Catalog, tmp_path: Path) -> None:
    root = tmp_path / "vol"
    root.mkdir()
    payload = b"repeat" * 100
    (root / "a.bin").write_bytes(payload)
    (root / "b.bin").write_bytes(payload)
    scan(catalog, root)

    first = hash_pass(catalog)
    second = hash_pass(catalog)

    assert first.candidates == 2
    assert second.candidates == 0, "already-identified files are not re-read"

    rows = catalog.conn.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"]
    assert rows == 1


def test_candidates_are_ordered_for_sequential_reads(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Reads follow file reference number, which approximates disk layout."""
    root = tmp_path / "vol"
    root.mkdir()
    for index in range(6):
        (root / f"f{index}.bin").write_bytes(b"z" * 700)
    volume_id = scan(catalog, root)

    rows = catalog.hash_candidates(volume_id=volume_id)
    frns = [r["frn"] for r in rows if r["frn"] is not None]
    assert frns == sorted(frns)


# -- fixity -------------------------------------------------------------------

def test_verify_detects_silent_corruption(catalog: Catalog, tmp_path: Path) -> None:
    """Bytes changed, timestamp untouched — nobody notices this by looking."""
    root = tmp_path / "vol"
    root.mkdir()
    size = hasher.SMALL_FILE_BYTES * 4
    (root / "a.bin").write_bytes(b"a" * size)
    (root / "b.bin").write_bytes(b"a" * size)
    scan(catalog, root)
    hash_pass(catalog)

    target = root / "a.bin"
    before = os.stat(target)
    corrupted = bytearray(b"a" * size)
    corrupted[size // 3] = ord("X")
    target.write_bytes(bytes(corrupted))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    result = verify_pass(catalog)

    assert result["mismatched"] == 1
    assert any(i["result"] == "mismatch" for i in result["incidents"])


def test_verify_covers_small_files_too(catalog: Catalog, tmp_path: Path) -> None:
    """Small files never get a full hash, but their quick hash read every byte.

    Skipping them would exclude most of a real catalog by count while still
    reporting a clean result — a fixity check that quietly covers a minority
    is worse than none.
    """
    root = tmp_path / "vol"
    root.mkdir()
    payload = b"small but catalogued" * 10
    (root / "a.bin").write_bytes(payload)
    (root / "b.bin").write_bytes(payload)
    scan(catalog, root)
    hash_pass(catalog)

    assert catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM content WHERE full_hash IS NOT NULL"
    ).fetchone()["n"] == 0, "small files should not have needed a full hash"

    assert verify_pass(catalog)["checked"] == 2

    target = root / "a.bin"
    before = os.stat(target)
    damaged = bytearray(payload)
    damaged[len(payload) // 2] = ord("!")
    target.write_bytes(bytes(damaged))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    result = verify_pass(catalog)
    assert result["mismatched"] == 1
    assert result["checked"] == 2


def test_verify_flags_a_small_file_that_changed_length(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Truncation with the timestamp preserved is still a change to the bytes."""
    root = tmp_path / "vol"
    root.mkdir()
    payload = b"length matters" * 8
    (root / "a.bin").write_bytes(payload)
    (root / "b.bin").write_bytes(payload)
    scan(catalog, root)
    hash_pass(catalog)

    target = root / "a.bin"
    before = os.stat(target)
    target.write_bytes(payload[:-4])
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert verify_pass(catalog)["mismatched"] == 1


def test_verify_passes_on_intact_content(catalog: Catalog, tmp_path: Path) -> None:
    root = tmp_path / "vol"
    root.mkdir()
    size = hasher.SMALL_FILE_BYTES * 4
    payload = bytearray(b"q" * size)
    (root / "a.bin").write_bytes(bytes(payload))
    payload[size // 4] = ord("Z")
    (root / "b.bin").write_bytes(bytes(payload))
    scan(catalog, root)
    hash_pass(catalog)

    result = verify_pass(catalog)

    assert result["checked"] == 2
    assert result["mismatched"] == 0
