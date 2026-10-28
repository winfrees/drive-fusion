"""Behaviour of the read-only gateway."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from drivefusion.core import fsio
from drivefusion.core.errors import DehydratedFileError, UnreadableError


def test_scandir_reports_entries_without_following_links(sample_tree: Path) -> None:
    names = {entry.name for entry in fsio.scandir(str(sample_tree))}
    assert {"docs", "media", "empty-dir", "deep"} <= names


def test_walk_does_not_traverse_links(sample_tree: Path) -> None:
    """A junction loop must not be able to make a bounded volume unbounded."""
    link = sample_tree / "docs-link"
    if not link.is_symlink():
        pytest.skip("symlink creation not permitted here")

    paths = [entry.path for entry in fsio.walk(str(sample_tree))]
    assert str(link) in paths, "the link itself should still be reported"

    # os.sep, not "/": on Windows a hardcoded slash would match nothing and
    # the assertion would pass without testing anything.
    below_link = [p for p in paths if p.startswith(str(link) + os.sep)]
    assert not below_link, f"walk traversed a link: {below_link[:3]}"


def test_walk_reports_unreadable_directories(sample_tree: Path, monkeypatch) -> None:
    """A skipped directory must be visible, not silent.

    A scan that quietly omits what it could not read would report complete
    coverage of a tree it only partly saw — and a copy count derived from that
    is confidently wrong rather than obviously incomplete.
    """
    blocked = str(sample_tree / "docs")
    real_scandir = fsio.scandir

    def failing_scandir(path: str):
        if path == blocked:
            raise UnreadableError("access denied (simulated)")
        return real_scandir(path)

    monkeypatch.setattr(fsio, "scandir", failing_scandir)

    reported: list[tuple[str, str]] = []
    entries = list(
        fsio.walk(
            str(sample_tree),
            on_error=lambda path, exc: reported.append((path, str(exc))),
        )
    )

    assert [path for path, _ in reported] == [blocked]
    assert not any(e.path.startswith(blocked + os.sep) for e in entries)


def test_stat_reports_sizes_and_times(sample_tree: Path) -> None:
    target = sample_tree / "docs" / "report.txt"
    info = fsio.stat(str(target))
    assert info.size == target.stat().st_size
    assert info.mtime_ns == target.stat().st_mtime_ns
    assert not info.is_dir


def test_missing_paths_raise_unreadable(tmp_path: Path) -> None:
    with pytest.raises(UnreadableError):
        fsio.stat(str(tmp_path / "does-not-exist"))
    with pytest.raises(UnreadableError):
        fsio.open_read(str(tmp_path / "does-not-exist"))


def test_opening_a_directory_is_refused(sample_tree: Path) -> None:
    with pytest.raises(UnreadableError):
        fsio.open_read(str(sample_tree / "docs"))


def test_cloud_placeholders_are_never_opened(sample_tree: Path, monkeypatch) -> None:
    """The refusal must happen before any open, or it has already failed.

    Opening a dehydrated file triggers a provider download — a change to the
    user's storage. This asserts the gateway short-circuits rather than
    detecting the problem after the fact.
    """
    target = str(sample_tree / "docs" / "report.txt")
    real_stat = fsio.stat

    def dehydrated_stat(path: str, **kwargs):
        info = real_stat(path, **kwargs)
        return type(info)(
            size=info.size,
            mtime_ns=info.mtime_ns,
            ctime_ns=info.ctime_ns,
            atime_ns=info.atime_ns,
            is_dir=info.is_dir,
            nlink=info.nlink,
            attributes=fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
            file_id=info.file_id,
        )

    opened: list[str] = []

    def tripwire(path: str) -> int:
        opened.append(path)
        raise AssertionError("gateway opened a dehydrated file")

    monkeypatch.setattr(fsio, "stat", dehydrated_stat)
    monkeypatch.setattr(fsio, "_open_fd_posix", tripwire)
    monkeypatch.setattr(fsio, "_open_fd_windows", tripwire)

    with pytest.raises(DehydratedFileError):
        fsio.open_read(target)
    assert not opened


@pytest.mark.parametrize(
    "attributes",
    [
        fsio.FILE_ATTRIBUTE_OFFLINE,
        fsio.FILE_ATTRIBUTE_RECALL_ON_OPEN,
        fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    ],
)
def test_dehydration_predicate(attributes: int) -> None:
    assert fsio.is_dehydrated(attributes)


def test_ordinary_attributes_are_not_dehydrated() -> None:
    assert not fsio.is_dehydrated(0)
    assert not fsio.is_dehydrated(fsio.FILE_ATTRIBUTE_COMPRESSED)
    assert not fsio.is_dehydrated(fsio.FILE_ATTRIBUTE_SPARSE_FILE)


def test_reparse_predicate() -> None:
    assert fsio.is_reparse_point(fsio.FILE_ATTRIBUTE_REPARSE_POINT)
    assert not fsio.is_reparse_point(fsio.FILE_ATTRIBUTE_DIRECTORY)


def test_large_file_reads_in_full(sample_tree: Path) -> None:
    """Buffered multi-chunk reads must return every byte, not just the first."""
    target = sample_tree / "media" / "large.bin"
    with fsio.open_read(str(target)) as handle:
        data = handle.read()
    assert len(data) == target.stat().st_size
    assert data == target.read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 path prefixing")
def test_long_path_prefixing() -> None:
    assert fsio.long_path("C:\\temp\\file.txt").startswith("\\\\?\\")
    already = "\\\\?\\C:\\temp\\file.txt"
    assert fsio.long_path(already) == already
    assert fsio.long_path("\\\\server\\share\\f").startswith("\\\\?\\UNC\\")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX passthrough")
def test_long_path_is_identity_off_windows() -> None:
    assert fsio.long_path("/mnt/data/file.txt") == "/mnt/data/file.txt"


# -- REVIEW F-16: the handle is checked, not just the path ---------------------

def _make_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")


def test_a_link_is_not_opened(sample_tree: Path, tmp_path: Path) -> None:
    """Links are reported, never followed, and that includes reading them."""
    outside = tmp_path / "outside-scope.txt"
    outside.write_bytes(b"not in scope")
    link = sample_tree / "docs" / "looks-like-a-file.txt"
    _make_link(link, outside)

    with pytest.raises(UnreadableError, match="link"):
        fsio.open_read(str(link))


def test_a_file_swapped_for_a_link_after_the_check_is_not_read(
    sample_tree: Path, tmp_path: Path, monkeypatch
) -> None:
    """The time-of-check to time-of-use race: checked as a file, opened as a link."""
    outside = tmp_path / "outside-scope.txt"
    outside.write_bytes(b"not in scope")
    target = sample_tree / "docs" / "report.txt"
    real_stat = fsio.stat

    def stat_then_swap(path: str, **kwargs):
        info = real_stat(path, **kwargs)
        if path == str(target):
            target.unlink()
            _make_link(target, outside)
        return info

    monkeypatch.setattr(fsio, "stat", stat_then_swap)
    with pytest.raises(UnreadableError):
        with fsio.open_read(str(target)) as handle:
            pytest.fail(f"read through a swapped-in link: {handle.read()!r}")


def test_a_handle_that_is_not_the_checked_file_is_refused_and_closed(
    sample_tree: Path, monkeypatch
) -> None:
    """Portable form of the race: the handle's identity differs from the check."""
    target = str(sample_tree / "docs" / "report.txt")
    real_stat = fsio.stat

    def other_identity(path: str, **kwargs):
        info = real_stat(path, **kwargs)
        return type(info)(
            size=info.size, mtime_ns=info.mtime_ns, ctime_ns=info.ctime_ns,
            atime_ns=info.atime_ns, is_dir=False, nlink=info.nlink,
            attributes=info.attributes, file_id=(info.file_id or 0) + 1,
            device=info.device or 1,
        )

    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(fsio, "stat", other_identity)
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd)))

    with pytest.raises(UnreadableError, match="changed between"):
        fsio.open_read(target)
    assert closed, "the refused handle was left open"


def test_a_handle_showing_recall_attributes_is_refused(sample_tree: Path, monkeypatch) -> None:
    """A placeholder reached after the path check must still not be read."""
    target = str(sample_tree / "docs" / "report.txt")
    real_fstat = os.fstat

    class Recalled:
        def __init__(self, st):
            self._st = st
            self.st_file_attributes = fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

        def __getattr__(self, name):
            return getattr(self._st, name)

    monkeypatch.setattr(fsio.os, "fstat", lambda fd: Recalled(real_fstat(fd)))
    with pytest.raises(DehydratedFileError):
        fsio.open_read(target)
