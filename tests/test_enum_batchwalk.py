"""The parallel batch walker: traversal, concurrency, and error reporting."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from drivefusion.core import fsio
from drivefusion.core.enum.batchwalk import (
    DirectoryResult,
    ParallelWalker,
    fsio_lister,
)
from drivefusion.core.enum.records import DirEntry

DIRECTORY = fsio.FILE_ATTRIBUTE_DIRECTORY


def p(*parts: str) -> str:
    """Join with the platform separator.

    The walker builds child paths with ``os.path.join``, so fixtures that
    hardcode "/" only match on POSIX and silently mismatch on Windows.
    """
    return os.path.join(*parts)


def fake_tree(layout: dict[str, list[tuple[str, bool]]]):
    """Build a lister over an in-memory tree of {path: [(name, is_dir)]}."""

    def lister(path: str):
        if path not in layout:
            raise FileNotFoundError(f"no such directory: {path}")
        return [
            DirEntry(
                name=name,
                attributes=DIRECTORY if is_dir else 0,
                size=0 if is_dir else 100,
                alloc_size=0,
                mtime_ns=0,
                ctime_ns=0,
            )
            for name, is_dir in layout[path]
        ]

    return lister


def test_visits_every_directory() -> None:
    layout = {
        p("root"): [("a", True), ("b", True), ("f1.txt", False)],
        p("root", "a"): [("a1", True), ("f2.txt", False)],
        p("root", "b"): [("f3.txt", False)],
        p("root", "a", "a1"): [("f4.txt", False)],
    }
    walker = ParallelWalker(fake_tree(layout), workers=4)
    results = list(walker.walk(p("root")))

    assert {r.path for r in results} == set(layout)
    assert all(r.ok for r in results)

    files = sum(1 for r in results for e in r.entries if not e.is_dir)
    assert files == 4


def test_unreadable_directories_are_reported_not_dropped() -> None:
    layout = {
        p("root"): [("ok", True), ("denied", True)],
        p("root", "ok"): [("f.txt", False)],
        # p("root", "denied") is deliberately absent, so listing it raises
    }
    results = list(ParallelWalker(fake_tree(layout), workers=2).walk(p("root")))

    failed = [r for r in results if not r.ok]
    assert len(failed) == 1
    assert failed[0].path == p("root", "denied")
    assert "no such directory" in failed[0].error

    # The failure must not abort the rest of the traversal.
    assert {r.path for r in results if r.ok} == {p("root"), p("root", "ok")}


def test_links_are_not_traversed() -> None:
    def lister(path: str):
        if path == p("root"):
            return [
                DirEntry(
                    name="loop",
                    attributes=DIRECTORY | fsio.FILE_ATTRIBUTE_REPARSE_POINT,
                    size=0, alloc_size=0, mtime_ns=0, ctime_ns=0,
                )
            ]
        raise AssertionError(f"walker followed a reparse point into {path}")

    results = list(ParallelWalker(lister, workers=2).walk(p("root")))
    assert len(results) == 1
    assert results[0].entries[0].name == "loop"


def test_dot_entries_are_not_descended() -> None:
    """FAT-family listings include . and ..; descending them never terminates."""

    def lister(path: str):
        if path != p("root"):
            raise AssertionError(f"descended into {path}")
        return [
            DirEntry(name=".", attributes=DIRECTORY, size=0, alloc_size=0,
                     mtime_ns=0, ctime_ns=0),
            DirEntry(name="..", attributes=DIRECTORY, size=0, alloc_size=0,
                     mtime_ns=0, ctime_ns=0),
        ]

    assert len(list(ParallelWalker(lister, workers=2).walk(p("root")))) == 1


def test_directories_are_listed_concurrently() -> None:
    """Depth is the whole point: serial listing would take workers x longer."""
    width = 8
    layout = {p("root"): [(f"d{i}", True) for i in range(width)]}
    for i in range(width):
        layout[p("root", f"d{i}")] = [("f.txt", False)]

    base = fake_tree(layout)
    concurrent = 0
    peak = 0
    lock = threading.Lock()

    def slow_lister(path: str):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        try:
            time.sleep(0.05)
            return base(path)
        finally:
            with lock:
                concurrent -= 1

    walker = ParallelWalker(slow_lister, workers=width)
    start = time.perf_counter()
    results = list(walker.walk(p("root")))
    elapsed = time.perf_counter() - start

    assert len(results) == width + 1
    assert peak > 1, "directories were listed serially"
    # Serial would be (width + 1) * 0.05; parallel should be far below.
    assert elapsed < (width + 1) * 0.05 * 0.75


def test_inflight_submissions_are_bounded() -> None:
    """A wide tree must not queue a future per directory up front."""
    width = 200
    layout = {p("root"): [(f"d{i}", True) for i in range(width)]}
    for i in range(width):
        layout[p("root", f"d{i}")] = []

    base = fake_tree(layout)
    live = 0
    peak = 0
    lock = threading.Lock()

    def counting_lister(path: str):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            return base(path)
        finally:
            with lock:
                live -= 1

    walker = ParallelWalker(counting_lister, workers=4, max_inflight=8)
    results = list(walker.walk(p("root")))

    assert len(results) == width + 1
    assert peak <= 8


def test_empty_directory_terminates() -> None:
    results = list(ParallelWalker(fake_tree({p("root"): []})).walk(p("root")))
    assert results == [DirectoryResult(path=p("root"), entries=())]


def test_fsio_lister_reads_a_real_tree(sample_tree: Path) -> None:
    """The portable lister must agree with the gateway it wraps."""
    entries = fsio_lister(str(sample_tree))
    names = {e.name for e in entries}
    assert {"docs", "media", "empty-dir"} <= names

    docs = [e for e in entries if e.name == "docs"][0]
    assert docs.is_dir


def test_walker_over_a_real_tree(sample_tree: Path) -> None:
    walker = ParallelWalker(fsio_lister, workers=4)
    results = list(walker.walk(str(sample_tree)))

    listed = {r.path for r in results if r.ok}
    expected = {str(sample_tree)} | {
        str(child)
        for child in sample_tree.rglob("*")
        if child.is_dir() and not child.is_symlink()
    }
    assert listed == expected
