"""Per-drive parallel reading (ROADMAP M5.6)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from drivefusion.core.identity import readers
from drivefusion.core.store.catalog import Catalog


class Probe:
    """Records how many reads run at once, per device and overall."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.now: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.overall = self.overall_peak = 0
        self.order: list = []

    def __call__(self, item):
        device, n = item
        with self.lock:
            self.now[device] = self.now.get(device, 0) + 1
            self.peak[device] = max(self.peak.get(device, 0), self.now[device])
            self.overall += 1
            self.overall_peak = max(self.overall_peak, self.overall)
            self.order.append(item)
        time.sleep(self.delay)
        with self.lock:
            self.now[device] -= 1
            self.overall -= 1
        if n == "boom":
            raise OSError("unreadable")
        return f"{device}{n}"


def groups(**devices):
    return [(media, [(name, i) for i in range(count)])
            for name, (media, count) in devices.items()]


def test_each_drive_gets_readers_for_its_media() -> None:
    probe = Probe()
    found = list(readers.read_all(
        groups(h=("hdd", 12), s=("ssd", 30), u=("unknown", 12)), probe, "quick"))
    assert len(found) == 54 and all(error is None for _, _, error in found)
    assert probe.peak == {"h": 2, "s": 6, "u": 2}
    assert probe.overall_peak > 6, "the drives were read at the same time"


def test_a_hard_drive_is_read_by_one_reader_in_order_for_full_hashes() -> None:
    probe = Probe(delay=0.002)
    list(readers.read_all(groups(h=("hdd", 20)), probe, "full"))
    assert probe.peak == {"h": 1}
    assert probe.order == [("h", i) for i in range(20)], "physical order kept"


def test_an_error_comes_back_with_its_item() -> None:
    probe = Probe(delay=0)
    found = list(readers.read_all([("ssd", [("s", 1), ("s", "boom")])], probe, "quick"))
    errors = {item: error for item, _, error in found}
    assert errors[("s", 1)] is None and isinstance(errors[("s", "boom")], OSError)


def test_stopping_early_starts_nothing_new_and_waits_for_running_reads() -> None:
    probe = Probe(delay=0.02)
    results = readers.read_all(groups(s=("ssd", 200)), probe, "quick")
    next(results)
    results.close()
    started = len(probe.order)
    assert probe.overall == 0, "a read was still running after close()"
    time.sleep(0.05)
    assert len(probe.order) == started < 200, "reads went on after close()"


def test_rows_are_grouped_by_physical_drive(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.db") as catalog:
        hdd = catalog.upsert_drive(serial="H", media="hdd")
        conn = catalog.conn
        add = "INSERT INTO volume (volume_guid, drive_id) VALUES (?, ?)"
        a = conn.execute(add, ("a", hdd)).lastrowid
        b = conn.execute(add, ("b", hdd)).lastrowid          # a second partition
        x = conn.execute(add, ("x", None)).lastrowid         # drive unverified
        y = conn.execute(add, ("y", None)).lastrowid
        rows = [{"volume_id": v, "n": i} for i, v in enumerate((a, x, b, y, a))]
        found = readers.by_device(catalog, rows)
    assert [(media, [r["n"] for r in items]) for media, items in found] == [
        ("hdd", [0, 2, 4]), ("unknown", [1]), ("unknown", [3])]


@pytest.mark.parametrize(("media", "tier", "count"), [
    ("hdd", "quick", 2), ("hdd", "full", 1), ("ssd", "full", 4), (None, "full", 2),
])
def test_reader_counts(media, tier, count) -> None:
    assert readers.readers_for(media, tier) == count
