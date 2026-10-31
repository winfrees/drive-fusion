"""Where copies can go, and how much room each destination has (ROADMAP M7.3, D17).

A destination is a drive the catalog can name, through the volume on it with
the most free space. Its room is that volume's free space less a reserve:
10 % of its capacity, and never less than 1 GiB, so a drive is never planned
to the last byte. A drive is not a destination when it is retired, when its
role is ``working`` (the disk in use every day is not where archive copies
go), or when its free space or capacity is unknown; each says why.
"""

from __future__ import annotations

from dataclasses import dataclass

RESERVE_SHARE = 0.10
RESERVE_FLOOR = 1 << 30
#: Assumed when a volume doesn't report its cluster size (NTFS's default).
DEFAULT_CLUSTER = 4096


@dataclass
class Destination:
    drive_id: int
    volume_id: int | None
    label: str
    cluster_bytes: int
    capacity: int | None
    free: int | None
    reserve: int
    batch_key: str | None
    zone: str | None
    excluded: str = ""              # why it isn't a destination, if it isn't

    @property
    def room(self) -> int:
        return 0 if self.excluded else max(0, (self.free or 0) - self.reserve)


def reserve_for(capacity: int | None) -> int:
    return max(int((capacity or 0) * RESERVE_SHARE), RESERVE_FLOOR)


def destinations(catalog) -> list[Destination]:
    """Every identified drive, with its room or the reason it has none."""
    from drivefusion.core.health import metadata

    out = []
    for drive in catalog.conn.execute("SELECT * FROM drive ORDER BY id"):
        volume = catalog.conn.execute(
            "SELECT * FROM volume WHERE drive_id = ? ORDER BY COALESCE(free_bytes, -1) DESC, id "
            "LIMIT 1", (drive["id"],)).fetchone()
        label = drive["nickname"] or drive["model"] or f"drive {drive['id']}"
        capacity = volume["capacity_bytes"] if volume else None
        free = volume["free_bytes"] if volume else None
        reason = ""
        if drive["retired_at"]:
            reason = f"retired {drive['retired_at']}"
        elif drive["role"] == "working":
            reason = "its role is working: archive copies go elsewhere"
        elif volume is None:
            reason = "no volume on it has been catalogued"
        elif free is None or capacity is None:
            reason = "its capacity or free space is unknown; rescan it"
        out.append(Destination(
            drive_id=drive["id"], volume_id=volume["id"] if volume else None, label=label,
            cluster_bytes=(volume["cluster_bytes"] if volume and volume["cluster_bytes"]
                           else DEFAULT_CLUSTER),
            capacity=capacity, free=free, reserve=reserve_for(capacity),
            batch_key=metadata.batch_key(drive)[0], zone=drive["disaster_zone"],
            excluded=reason))
    return out


def footprint(sizes, cluster: int) -> int:
    """Bytes a set of files occupies on a volume with this cluster size."""
    return sum(-(-size // cluster) * cluster for size in sizes if size)
