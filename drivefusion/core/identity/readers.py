"""Reading several drives at once, each at the pace it can take (ROADMAP M5.6).

Hashing is bound by the drives, not the CPU, and different drives don't slow
each other down, so each physical drive gets its own small pool of readers:

* a **hard drive** gets 1–2, because a second head-seeking reader turns a
  sequential read into a seek storm: two for quick hashes, which are small
  scattered reads anyway, one for full hashes, which are long sequential ones;
* an **SSD** gets 4–6, which keeps its queue busy;
* a drive of **unknown** media, or a volume whose drive is unverified, gets 2.

Within a drive, files are handed out in the order given (volume, then file
reference number), so a single reader still reads in roughly physical order.

Only reading happens on the worker threads. Every result comes back to the
calling thread, which alone touches the catalog: a SQLite connection is not
shared across threads. When the caller stops early, by cancelling or by an
error, nothing new is started and the reads already running are waited for,
so no file is being opened once this returns.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, Iterator

#: Readers per drive, by media and hash tier.
READERS = {
    ("hdd", "quick"): 2, ("hdd", "full"): 1,
    ("ssd", "quick"): 6, ("ssd", "full"): 4,
    ("unknown", "quick"): 2, ("unknown", "full"): 2,
}

#: Files queued ahead per reader, so a reader never waits on the scheduler.
AHEAD = 4


def readers_for(media: str | None, tier: str) -> int:
    return READERS[(media if media in ("hdd", "ssd") else "unknown", tier)]


def by_device(catalog, items: Iterable, volume_of: Callable = lambda item: item["volume_id"]
              ) -> list[tuple[str, list]]:
    """Group items by the physical drive their volume is on, keeping their
    order. Returns ``(media, items)`` per device."""
    volumes = {
        row["id"]: (row["drive_id"], row["media"])
        for row in catalog.conn.execute(
            "SELECT v.id, v.drive_id, d.media FROM volume v "
            "LEFT JOIN drive d ON d.id = v.drive_id"
        )
    }
    groups: dict[tuple, tuple[str, list]] = {}
    for item in items:
        volume_id = volume_of(item)
        drive_id, media = volumes.get(volume_id, (None, None))
        # An unverified volume is its own device: it may be another disk.
        key = ("drive", drive_id) if drive_id is not None else ("volume", volume_id)
        groups.setdefault(key, (media or "unknown", []))[1].append(item)
    return list(groups.values())


def read_all(groups: list[tuple[str, list]], work: Callable, tier: str
             ) -> Iterator[tuple[object, object, BaseException | None]]:
    """Run ``work(row)`` over every row, each device with its own readers.

    Yields ``(row, result, error)`` as reads finish; ``error`` is the
    exception ``work`` raised, if any. Closing the generator early stops it
    cleanly (see the module docstring).
    """
    pools, queues, running, in_flight = [], [], {}, []
    try:
        for media, rows in groups:
            readers = readers_for(media, tier)
            pools.append((ThreadPoolExecutor(max_workers=readers,
                                             thread_name_prefix=f"read-{media}"), readers))
            queues.append(deque(rows))
            in_flight.append(0)

        def top_up(index: int) -> None:
            pool, readers = pools[index]
            queue = queues[index]
            while queue and in_flight[index] < readers * AHEAD:
                row = queue.popleft()
                running[pool.submit(work, row)] = (index, row)
                in_flight[index] += 1

        for index in range(len(pools)):
            top_up(index)
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                index, row = running.pop(future)
                in_flight[index] -= 1
                error = future.exception()
                yield row, (None if error else future.result()), error
                top_up(index)
    finally:
        for future in list(running):
            future.cancel()
        for pool, _ in pools:
            pool.shutdown(wait=True, cancel_futures=True)

