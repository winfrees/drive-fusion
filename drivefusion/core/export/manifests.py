"""Per-drive manifests: what a shelved drive says about itself.

§10.2. The point of these is a drive in a box in a closet, three years from now,
with no Drive Fusion installed and possibly no catalog left. Plugged in, it
should be able to answer: what is this, what else is on it, how many other
copies exist, where are they, and were these bytes ever checked.

Two files per drive, because two audiences:

* ``_DRIVE-FUSION.json`` for a program — identity, contents by collection,
  counts, sizes, checksums, sibling copies.
* ``_CATALOG.md`` for a person with a text editor and no tooling at all, which
  is the realistic recovery scenario.

**Written into the bundle, not onto the drive** (§3.3). The names begin with an
underscore because that is where the user will *copy* them to, and the tool
will not do it for them: writing to the drive is the one thing it does not do,
and generating a file whose stated purpose is to live on the media is exactly
where a well-meaning future change would add "…and put it there for you".
So the bundle lays them out under ``drives/<name>/`` and the instructions say
to copy them across yourself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from drivefusion.core.analysis.duplicates import DRIVE_KEY, drive_key
from drivefusion.core.store.catalog import Catalog

#: The filename a drive should carry, if the user copies it across.
MACHINE_MANIFEST = "_DRIVE-FUSION.json"
HUMAN_MANIFEST = "_CATALOG.md"


def _safe_name(text: str, *, fallback: str = "drive") -> str:
    """A directory name for a drive. Conservative: this becomes a path.

    The fallback matters more than it looks. A label made entirely of
    characters a path cannot carry — ``/`` is a real volume label on POSIX,
    and a Windows label can be stranger — sanitises to nothing, and two such
    volumes would land in ``drive/`` and ``drive-2/`` with nothing to say
    which was which. So callers pass something that identifies the volume.
    """
    keep = []
    for character in str(text or ""):
        keep.append(character if character.isalnum() or character in "-_." else "-")
    cleaned = "".join(keep).strip("-.")
    return (cleaned or fallback)[:64]


def drives(catalog: Catalog) -> list[dict]:
    """Every drive, or volume standing in for one, with its catalogued content."""
    rows = catalog.conn.execute(
        "SELECT v.id AS volume_id, v.volume_guid, v.label, v.fs_type, "
        "       v.cluster_bytes, v.capacity_bytes, v.free_bytes, v.last_seen_at, "
        "       d.id AS drive_id, d.serial, d.model, d.manufacturer, d.nickname, "
        "       d.bus, d.media, d.purchase_date, d.location, d.role, "
        "       d.batch_key, d.disaster_zone, "
        f"      {DRIVE_KEY} AS drive_key "
        "FROM volume v LEFT JOIN drive d ON d.id = v.drive_id "
        "ORDER BY COALESCE(d.nickname, d.model, v.label, v.volume_guid)"
    ).fetchall()

    out = []
    for row in rows:
        entry = dict(row)
        entry["name"] = (
            entry["nickname"]
            or entry["model"]
            or entry["label"]
            or entry["volume_guid"]
        )
        # Named for the drive where that is possible, and for the volume
        # otherwise, so every folder in the bundle identifies its subject.
        entry["dir_name"] = _safe_name(
            entry["name"],
            fallback=_safe_name(
                entry["volume_guid"], fallback=f"volume-{entry['volume_id']}"
            ),
        )
        out.append(entry)

    # Two drives with the same nickname would otherwise share a directory and
    # the second write would be refused — correctly, but with a confusing
    # message about a file that already exists.
    seen: dict[str, int] = {}
    for entry in out:
        base = entry["dir_name"]
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count > 1:
            entry["dir_name"] = f"{base}-{count}"
    return out


def _contents(catalog: Catalog, volume_id: int) -> list[dict]:
    """What is on this volume, grouped by collection.

    Membership comes from ``collections.member_file_filter`` rather than a
    query written here. Collection membership is a *rule* — subtree, glob or
    extension — and a second implementation of it in the export package would
    drift from the one the scorecards use, so a drive manifest would disagree
    with the report it shipped beside.
    """
    from drivefusion.core.curate import collections as collections_module

    out = []
    for collection in collections_module.all_collections(catalog):
        where, params = collections_module.member_file_filter(catalog, collection.id)
        row = catalog.conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
            "       SUM(CASE WHEN f.content_id IS NOT NULL THEN 1 ELSE 0 END) "
            "         AS hashed "
            f"FROM file f WHERE f.volume_id = ? AND f.vanished_at IS NULL "
            f"  AND {where}",
            [volume_id, *params],
        ).fetchone()

        if not row or not int(row["files"] or 0):
            continue
        out.append(
            {
                "id": collection.id,
                "local_pid": collection.local_pid,
                "title": collection.title,
                "license": collection.license,
                "files": int(row["files"]),
                "bytes": int(row["bytes"] or 0),
                "hashed": int(row["hashed"] or 0),
            }
        )
    out.sort(key=lambda entry: entry["bytes"], reverse=True)
    return out


def _totals(catalog: Catalog, volume_id: int) -> dict:
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes, "
        "       SUM(CASE WHEN content_id IS NOT NULL THEN 1 ELSE 0 END) AS hashed "
        "FROM file WHERE volume_id = ? AND vanished_at IS NULL",
        (volume_id,),
    ).fetchone()
    return dict(row) if row else {"files": 0, "bytes": 0, "hashed": 0}


def _siblings(catalog: Catalog, volume_id: int, limit: int = 12) -> list[dict]:
    """Where else the content on this volume lives.

    The answer a shelved drive most needs to carry: "there are two other copies
    of this, on these drives". Grouped by physical drive rather than by volume,
    because two copies on one disk are one copy against that disk failing — the
    same rule the duplicate analysis uses.
    """
    rows = catalog.conn.execute(
        "SELECT COALESCE(d.nickname, d.model, v.label, v.volume_guid) AS name, "
        "       d.serial AS serial, "
        f"      {DRIVE_KEY} AS drive_key, "
        "       COUNT(DISTINCT mine.content_id) AS shared_contents, "
        "       COALESCE(SUM(DISTINCT c.size_bytes), 0) AS shared_bytes "
        "FROM file mine "
        "JOIN file theirs ON theirs.content_id = mine.content_id "
        "JOIN volume v ON v.id = theirs.volume_id "
        "LEFT JOIN drive d ON d.id = v.drive_id "
        "JOIN content c ON c.id = mine.content_id "
        "WHERE mine.volume_id = ? AND mine.vanished_at IS NULL "
        "  AND mine.content_id IS NOT NULL AND theirs.vanished_at IS NULL "
        f"  AND {DRIVE_KEY} != ("
        "    SELECT COALESCE(v2.drive_id, -v2.id) FROM volume v2 WHERE v2.id = ?"
        "  ) "
        f"GROUP BY {DRIVE_KEY} "
        "ORDER BY shared_contents DESC LIMIT ?",
        (volume_id, volume_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _fixity(catalog: Catalog, volume_id: int) -> dict:
    rows = catalog.conn.execute(
        "SELECT x.result, COUNT(*) AS n FROM fixity_check x "
        "JOIN file f ON f.id = x.file_id WHERE f.volume_id = ? "
        "GROUP BY x.result",
        (volume_id,),
    ).fetchall()
    return {str(dict(row)["result"]): int(dict(row)["n"]) for row in rows}


def _sole_copies(catalog: Catalog, volume_id: int) -> dict:
    """Content on this volume that exists on no other physical drive.

    The number this manifest exists to carry. "No other drive held any of this
    content" is the wrong framing for two common cases: a drive with nothing
    catalogued on it has nothing to lose, and a drive where most content is
    replicated and a little is not needs the *little* called out rather than an
    all-or-nothing verdict.
    """
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(c.size_bytes), 0) AS bytes "
        "FROM file mine JOIN content c ON c.id = mine.content_id "
        "WHERE mine.volume_id = ? AND mine.vanished_at IS NULL "
        "  AND mine.content_id IS NOT NULL "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM file other JOIN volume ov ON ov.id = other.volume_id "
        "    WHERE other.content_id = mine.content_id "
        "      AND other.vanished_at IS NULL "
        f"      AND {drive_key('ov')} != ("
        "        SELECT COALESCE(mv.drive_id, -mv.id) FROM volume mv "
        "        WHERE mv.id = mine.volume_id"
        "      )"
        "  )",
        (volume_id,),
    ).fetchone()

    unhashed = catalog.conn.execute(
        "SELECT COUNT(*) AS files FROM file "
        "WHERE volume_id = ? AND vanished_at IS NULL AND content_id IS NULL",
        (volume_id,),
    ).fetchone()

    return {
        "files": int(dict(row).get("files", 0)) if row else 0,
        "bytes": int(dict(row).get("bytes", 0) or 0) if row else 0,
        # Files with no checksum cannot be matched against another drive at
        # all, so their replication is unknown rather than absent. Saying so is
        # the difference between a manifest that is cautious and one that is
        # wrong.
        "unknown_files": int(dict(unhashed).get("files", 0)) if unhashed else 0,
    }


def machine_manifest(catalog: Catalog, drive: dict, *, version: str) -> str:
    """``_DRIVE-FUSION.json``: everything a program needs to identify this drive."""
    volume_id = int(drive["volume_id"])
    totals = _totals(catalog, volume_id)

    return json.dumps(
        {
            "format": "drive-fusion-drive-manifest/1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": f"Drive Fusion {version}",
            "read_only_note": (
                "Drive Fusion read this drive and wrote nothing to it. This "
                "file was generated into an export folder; if it is on the "
                "drive, a person copied it here."
            ),
            "drive": {
                "nickname": drive.get("nickname"),
                "model": drive.get("model"),
                "manufacturer": drive.get("manufacturer"),
                "serial": drive.get("serial"),
                "bus": drive.get("bus"),
                "media": drive.get("media"),
                "purchase_date": drive.get("purchase_date"),
                "location": drive.get("location"),
                "role": drive.get("role"),
                "batch_key": drive.get("batch_key"),
                "disaster_zone": drive.get("disaster_zone"),
            },
            "volume": {
                "volume_guid": drive.get("volume_guid"),
                "label": drive.get("label"),
                "filesystem": drive.get("fs_type"),
                "cluster_bytes": drive.get("cluster_bytes"),
                "capacity_bytes": drive.get("capacity_bytes"),
                "free_bytes": drive.get("free_bytes"),
                "last_catalogued_at": drive.get("last_seen_at"),
            },
            "contents": {
                "files": int(totals.get("files") or 0),
                "bytes": int(totals.get("bytes") or 0),
                "files_with_checksums": int(totals.get("hashed") or 0),
                "checksum_note": (
                    "A file has no checksum when its size is unique in the "
                    "catalog: nothing could be a duplicate of it, so it was "
                    "never opened. That is a deliberate saving, not a gap in "
                    "coverage."
                ),
                "collections": _contents(catalog, volume_id),
            },
            "other_copies": {
                "note": (
                    "Drives holding content that is also on this one, counted "
                    "over distinct physical drives. Two copies on one disk are "
                    "one copy against that disk failing."
                ),
                "drives": _siblings(catalog, volume_id),
                "held_nowhere_else": _sole_copies(catalog, volume_id),
            },
            "fixity_checks": _fixity(catalog, volume_id),
        },
        indent=2,
    )


def human_manifest(catalog: Catalog, drive: dict, *, version: str) -> str:
    """``_CATALOG.md``: the same thing, for someone with only a text editor."""
    volume_id = int(drive["volume_id"])
    totals = _totals(catalog, volume_id)
    collections = _contents(catalog, volume_id)
    siblings = _siblings(catalog, volume_id)
    fixity = _fixity(catalog, volume_id)

    def size(value) -> str:
        value = float(value or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:,.1f} {unit}"
            value /= 1024
        return f"{value:,.1f} TB"

    lines = [
        f"# {drive['name']}",
        "",
        "This drive was catalogued by Drive Fusion, which reads and never "
        "writes. Nothing on it was modified, moved or deleted. This file was "
        "generated into an export folder; if you are reading it on the drive, "
        "somebody copied it here.",
        "",
        "## What this drive is",
        "",
        f"- Model: {drive.get('model') or 'not recorded'}",
        f"- Serial: {drive.get('serial') or 'not recorded'}",
        f"- Filesystem: {drive.get('fs_type') or 'not recorded'}"
        + (
            f", {int(drive['cluster_bytes']):,}-byte clusters"
            if drive.get("cluster_bytes")
            else ""
        ),
        f"- Capacity: {size(drive.get('capacity_bytes'))}",
        f"- Kept at: {drive.get('location') or drive.get('disaster_zone') or 'not recorded'}",
        f"- Last catalogued: {drive.get('last_seen_at') or 'not recorded'}",
        "",
        "## What is on it",
        "",
        f"{int(totals.get('files') or 0):,} files, {size(totals.get('bytes'))}. "
        f"{int(totals.get('hashed') or 0):,} of them have a recorded checksum.",
        "",
        "A file has no checksum when nothing else in the archive shares its "
        "size — nothing could be a duplicate of it, so it was never opened. "
        "That is a deliberate saving rather than a gap in coverage.",
        "",
    ]

    if collections:
        lines += ["### Collections", "", "| Collection | Files | Size | Licence |", "|---|---|---|---|"]
        for item in collections:
            lines.append(
                f"| {item['title']} ({item['local_pid']}) | {item['files']:,} | "
                f"{size(item['bytes'])} | {item.get('license') or 'none recorded'} |"
            )
        lines.append("")
    else:
        lines += [
            "### Collections",
            "",
            "No confirmed collections on this drive. Content nobody has chosen "
            "to curate is a legitimate state, not a backlog.",
            "",
        ]

    lines += ["## Where the other copies are", ""]
    sole = _sole_copies(catalog, volume_id)
    total_files = int(totals.get("files") or 0)

    if not total_files:
        lines += [
            "Nothing on this drive is in the catalog. Either it has not been "
            "scanned, or it was empty when it was.",
            "",
        ]
    else:
        if siblings:
            lines += ["| Drive | Shared items | Serial |", "|---|---|---|"]
            for item in siblings:
                lines.append(
                    f"| {item['name']} | {item['shared_contents']:,} | "
                    f"{item.get('serial') or 'not recorded'} |"
                )
            lines += [
                "",
                "Counted over distinct physical drives: two copies on one disk "
                "are one copy against that disk failing.",
                "",
            ]

        if sole["files"]:
            lines += [
                f"**{sole['files']:,} file(s) here, {size(sole['bytes'])}, were "
                "on no other drive in the catalog.** Losing this drive would "
                "lose them. That is the most important line in this file.",
                "",
            ]
        elif not siblings:
            lines += [
                "No other drive in the catalog shared any of this content, and "
                "none of it could be matched by checksum either — see below.",
                "",
            ]
        else:
            lines += [
                "Every checksummed file on this drive also exists on at least "
                "one other drive in the catalog.",
                "",
            ]

        if sole["unknown_files"]:
            lines += [
                f"{sole['unknown_files']:,} file(s) have no checksum, so "
                "whether they exist elsewhere is unknown rather than no. A "
                "file is left unhashed when no other file in the archive "
                "shares its size, which means nothing could be a byte-for-byte "
                "duplicate of it — but a differently sized near-copy would not "
                "be found this way.",
                "",
            ]

    lines += ["## Whether the bytes were ever checked", ""]
    if fixity:
        for result, count in sorted(fixity.items()):
            lines.append(f"- {result}: {count:,}")
        if fixity.get("mismatch"):
            lines.append("")
            lines.append(
                f"**{fixity['mismatch']:,} file(s) re-read to a different "
                "checksum than the one recorded.** The bytes on disk changed "
                "after they were catalogued, and Drive Fusion did not change "
                "them. Treat this drive as suspect."
            )
    else:
        lines.append(
            "No fixity check has been run against the files on this drive, so "
            "nothing here says the bytes are still the bytes."
        )
    lines.append("")

    return "\n".join(lines)
