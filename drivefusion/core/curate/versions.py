"""Version sets: near-duplicates and which one to keep in view (PLAN §8.1, ROADMAP M6.5).

A report, never an action. Files in one folder that share a type and a name
once version markers are stripped (``thesis_v2.docx``, ``thesis (1).docx``,
``thesis final.docx``, ``thesis 2021-03-04.docx``) and hold different content
are proposed as one version set. Each set names a **preferred member** and why:
the highest version number when every member carries one, otherwise the most
recently modified. Its **confidence** falls as the members' sizes drift
apart, since versions of one document are usually close in size.

Method ``name-stem``: what it can't see, it says. Versions in different
folders, or renamed beyond their markers, are not found; identical copies
are duplicates (``core/analysis``), not versions.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from drivefusion.core.store import curation
from drivefusion.core.store.curation import VersionMember

METHOD = "name-stem"

_SEP = r"[ _.\-]*"
#: Trailing markers, stripped repeatedly: "report_v2 final (1)" → "report".
_MARKERS = [
    re.compile(_SEP + r"\(\d+\)$"),
    re.compile(_SEP + r"(?:v|ver|version|rev|r)" + _SEP + r"(\d+)$", re.I),
    re.compile(_SEP + r"(?:final|draft|copy|old|new|latest|edited|revised|backup)$", re.I),
    re.compile(_SEP + r"(?:19|20)\d\d[-_.]?\d\d[-_.]?\d\d$"),
]
_VERSION = re.compile(_SEP + r"(?:v|ver|version|rev|r)" + _SEP + r"(\d+)(?:\b|_|$)", re.I)


def split_name(name: str) -> tuple[str, str, bool]:
    """(stem with markers stripped, extension, whether any marker was found)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    base, found = stem.lower(), False
    changed = True
    while changed:
        changed = False
        for marker in _MARKERS:
            shorter = marker.sub("", base)
            if shorter != base and shorter:
                base, found, changed = shorter, True, True
    return base.strip(), ext.lower(), found


def version_number(name: str) -> int | None:
    stem = name.rpartition(".")[0] or name
    found = _VERSION.findall(stem)
    return int(found[-1]) if found else None


@dataclass
class _Member:
    file_id: int
    content_id: int | None
    name: str
    size: int
    mtime_ns: int | None
    marked: bool


def find(catalog) -> list[dict]:
    """Group the catalog's near-duplicates into version sets (not stored)."""
    groups: dict[tuple, dict] = defaultdict(dict)
    for row in catalog.conn.execute(
        "SELECT id, dir_id, name, size_bytes, mtime_ns, content_id FROM file "
        "WHERE vanished_at IS NULL AND read_state = 'ok' AND size_bytes > 0"
    ):
        stem, ext, marked = split_name(row["name"])
        if not stem:
            continue
        members = groups[(row["dir_id"], stem, ext)]
        # One member per distinct content: two paths to the same bytes are a
        # duplicate, not two versions. A file of unique size has no content
        # id and is distinct by construction.
        key = row["content_id"] if row["content_id"] is not None else ("file", row["id"])
        current = members.get(key)
        if current is None or (row["mtime_ns"] or 0) > (current.mtime_ns or 0):
            members[key] = _Member(row["id"], row["content_id"], row["name"],
                                   row["size_bytes"], row["mtime_ns"], marked)

    found = []
    for (_dir, stem, ext), by_content in groups.items():
        members = list(by_content.values())
        # Two different files that merely share a name stem are not versions
        # unless at least one says it is a version of something.
        if len(members) < 2 or not any(m.marked for m in members):
            continue
        numbers = [version_number(m.name) for m in members]
        if all(n is not None for n in numbers) and len(set(numbers)) == len(numbers):
            preferred = members[numbers.index(max(numbers))]
            why = f"highest version number ({preferred.name})"
        else:
            preferred = max(members, key=lambda m: (m.mtime_ns or 0, m.size))
            why = f"most recently modified ({preferred.name})"
        sizes = [m.size for m in members]
        confidence = round(0.6 + 0.4 * (min(sizes) / max(sizes)), 3)
        found.append({
            "label": f"{stem}.{ext}" if ext else stem,
            "confidence": confidence,
            "rationale": f"same folder, same name once version markers are stripped; "
                         f"preferred: {why}",
            "members": [VersionMember(m.file_id, m.content_id, m is preferred)
                        for m in sorted(members, key=lambda m: m.name)],
        })
    found.sort(key=lambda s: (-s["confidence"], s["label"]))
    return found


def refresh(catalog) -> int:
    """Find version sets and store them in place of the last run's. Audited."""
    sets = find(catalog)
    catalog.conn.execute("BEGIN")
    try:
        stored = curation.replace_version_sets(catalog, sets, method=METHOD)
        catalog.conn.execute("COMMIT")
    except BaseException:
        catalog.conn.execute("ROLLBACK")
        raise
    return stored
