"""The no-touch test: the highest-priority test in the suite.

It asserts the central promise — that a full run leaves catalogued media
byte-for-byte and metadata-for-metadata identical. Every milestone extends
``run_full_cycle`` with the stages it adds, so the assertion always covers the
whole pipeline rather than the parts that happened to be wired up first.

If this test fails, the tool has modified a user's drive. There is no
acceptable failure of this test.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from drivefusion.core import fsio
from drivefusion.core.errors import ReadOnlyViolation
from tests.fixtures import tree as tree_fixture


def run_full_cycle(root: Path) -> dict[str, str]:
    """Everything the tool does to a tree, end to end.

    M0: enumerate, stat, and hash every readable file through the gateway.
    M1 adds catalog persistence, M3 hashing tiers and analysis, M7 planning,
    M8 export — each extending this function rather than testing separately,
    so the guarantee is asserted over the real pipeline.
    """
    digests: dict[str, str] = {}
    for entry in fsio.walk(str(root)):
        if entry.is_dir or entry.is_symlink or entry.is_reparse_point:
            continue
        info = fsio.stat(entry.path)
        if info.is_dehydrated:
            continue
        digest = hashlib.sha256()
        with fsio.open_read(entry.path) as handle:
            while chunk := handle.read(1 << 16):
                digest.update(chunk)
        rel = Path(entry.path).relative_to(root).as_posix()
        digests[rel] = digest.hexdigest()
    return digests


#: Landmarks that must appear in any honest snapshot of the fixture tree.
#: Named explicitly so a snapshot that silently observes less than the whole
#: tree cannot masquerade as a passing no-touch assertion.
LANDMARKS = {
    "docs/report.txt",
    "docs/empty.txt",
    "docs/nested/é中文-ünicode.txt",
    "media/large.bin",
    "empty-dir",
}


def test_snapshot_observes_the_whole_tree(sample_tree: Path) -> None:
    """The instrument must see everything, or every guarantee below is vacuous.

    This is the test that catches a snapshot which quietly observed nothing —
    two empty snapshots compare equal, so the no-touch assertion would pass
    while examining no files at all.
    """
    snap = tree_fixture.snapshot(sample_tree)

    expected = {
        path.relative_to(sample_tree).as_posix() for path in sample_tree.rglob("*")
    }
    assert set(snap.entries) == expected
    assert LANDMARKS <= set(snap.entries)
    assert len(snap.entries) > 20


def test_snapshot_works_without_noatime_flags(sample_tree: Path, monkeypatch) -> None:
    """Simulate Windows, which has neither O_NOATIME nor O_DIRECTORY.

    Regression test for the first Windows CI run: ``os.open`` on a directory
    fails on Windows, the traversal swallowed the error, and every snapshot
    came back empty — so the no-touch assertion passed while examining nothing.
    Running this on any platform keeps that path covered.
    """
    monkeypatch.delattr(os, "O_NOATIME", raising=False)
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)

    snap = tree_fixture.snapshot(sample_tree)
    assert LANDMARKS <= set(snap.entries)

    expected = {
        path.relative_to(sample_tree).as_posix() for path in sample_tree.rglob("*")
    }
    assert set(snap.entries) == expected


def test_snapshot_content_hashes_are_untranslated(sample_tree: Path) -> None:
    """Binary reads must not be line-ending translated (a Windows default).

    Without O_BINARY, ``os.read`` on Windows strips carriage returns, so the
    snapshot would fingerprint bytes that are not what is on disk.
    """
    target = sample_tree / "docs" / "crlf.bin"
    target.write_bytes(b"line one\r\nline two\r\n\x00\xff")

    snap = tree_fixture.snapshot(sample_tree)
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert snap.entries["docs/crlf.bin"][4] == expected


def test_snapshot_refuses_to_be_empty(tmp_path: Path) -> None:
    """An empty snapshot is a broken instrument, not a valid measurement."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(tree_fixture.EmptySnapshotError):
        tree_fixture.snapshot(empty)


def test_full_cycle_leaves_the_tree_identical(sample_tree: Path) -> None:
    before = tree_fixture.snapshot(sample_tree)

    assert LANDMARKS <= set(before.entries), "snapshot did not observe the tree"

    digests = run_full_cycle(sample_tree)
    assert digests, "the cycle must actually read something for this to mean anything"

    after = tree_fixture.snapshot(sample_tree)

    differences = before.diff(after)
    assert not differences, "Drive Fusion modified catalogued media:\n" + "\n".join(
        differences
    )
    assert before.digest == after.digest


def test_full_cycle_reads_every_file(sample_tree: Path) -> None:
    """A no-touch test over a cycle that quietly skipped files proves nothing."""
    digests = run_full_cycle(sample_tree)

    expected = {
        path.relative_to(sample_tree).as_posix()
        for path in sample_tree.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert set(digests) == expected


def test_hashes_match_independent_read(sample_tree: Path) -> None:
    """The gateway must return the same bytes an ordinary read would."""
    digests = run_full_cycle(sample_tree)
    for rel, digest in digests.items():
        expected = hashlib.sha256((sample_tree / rel).read_bytes()).hexdigest()
        assert digest == expected, f"gateway returned different bytes for {rel}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows disables access-time updates by default; nothing to assert",
)
def test_reading_does_not_update_access_times(sample_tree: Path) -> None:
    """Reading should not even disturb access times where the OS allows it.

    ``O_NOATIME`` is only granted to a file's owner, so this is skipped rather
    than failed when the platform will not honour it — asserting something the
    OS refuses to provide would make the suite lie about coverage.
    """
    if not getattr(os, "O_NOATIME", 0):
        pytest.skip("O_NOATIME unavailable on this platform")

    probe = next(p for p in sample_tree.rglob("*.txt") if p.is_file())
    try:
        os.close(os.open(probe, os.O_RDONLY | os.O_NOATIME))
    except PermissionError:
        pytest.skip("O_NOATIME not permitted for this user on this filesystem")

    before = tree_fixture.snapshot(sample_tree)
    run_full_cycle(sample_tree)
    after = tree_fixture.snapshot(sample_tree)

    changed = [
        rel
        for rel, atime in before.atimes.items()
        if rel in after.atimes and after.atimes[rel] != atime
    ]
    assert not changed, f"access times changed for: {changed[:5]}"


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (lambda root: (root / "docs" / "report.txt").write_bytes(b"tampered"), "modified"),
        (lambda root: (root / "docs" / "report.txt").unlink(), "deleted"),
        (lambda root: (root / "docs" / "new.txt").write_bytes(b"new"), "created"),
        (lambda root: os.utime(root / "docs" / "report.txt", (0, 0)), "modified"),
        (lambda root: (root / "docs" / "report.txt").chmod(0o600), "modified"),
    ],
)
def test_the_snapshot_detects_tampering(sample_tree: Path, damage, expected: str) -> None:
    """Proof that the no-touch assertion has teeth.

    A test that passes because it cannot fail is worse than no test. Each kind
    of damage the tool must never cause is inflicted deliberately here, and the
    snapshot has to notice.
    """
    before = tree_fixture.snapshot(sample_tree)
    damage(sample_tree)
    after = tree_fixture.snapshot(sample_tree)

    differences = before.diff(after)
    assert differences, f"snapshot failed to detect {expected} content"
    assert any(d.startswith(expected) for d in differences), differences
    assert before.digest != after.digest


def test_handles_refuse_to_write(sample_tree: Path) -> None:
    """The refusal is specific and loud, not an incidental AttributeError."""
    target = sample_tree / "docs" / "report.txt"
    original = target.read_bytes()

    with fsio.open_read(str(target)) as handle:
        assert handle.readable() and not handle.writable()
        for attempt in (
            lambda: handle.write(b"x"),
            lambda: handle.writelines([b"x"]),
            lambda: handle.truncate(0),
        ):
            with pytest.raises(ReadOnlyViolation):
                attempt()

    assert target.read_bytes() == original
