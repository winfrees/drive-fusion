"""Naming templates as a reviewable mapping — and its inverse.

§10.1. The user renames files themselves, so what this produces is a table: old
path, new path, and why. Two properties make it safe to act on.

**It is reviewable before anything happens.** Every row is visible, the
collisions are called out, and no row is applied by this tool.

**The inverse is generated alongside it.** This is the part that is easy to skip
and expensive to omit. Someone who renames four thousand files from a mapping
table and then decides it was wrong has no way back unless the reverse table
was written down at the time — the information is gone once the old names are.
So ``rename-plan.csv`` always ships with ``rename-undo.csv``, and a mapping
whose inverse is not well defined is refused rather than exported, because an
irreversible rename presented as reversible is worse than no mapping at all.

The templates deliberately rearrange what is already known — collection, date,
original stem — and never invent an identifier. A name that encodes a number
nobody can look up is not findable, it is just tidy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from drivefusion.core.store.catalog import Catalog

#: Available placeholders. Each resolves to something already in the catalog;
#: there is no counter or hash that would make a name undecodable later.
PLACEHOLDERS = {
    "{collection}": "the collection's title, reduced to a safe token",
    "{pid}": "the collection's local persistent identifier",
    "{project}": "the collection's project, or 'unfiled'",
    "{year}": "the file's modification year",
    "{month}": "the file's modification month, two digits",
    "{date}": "the file's modification date, YYYY-MM-DD",
    "{stem}": "the original filename without its extension",
    "{ext}": "the original extension, including the dot",
    "{parent}": "the name of the containing directory",
}

DEFAULT_TEMPLATE = "{collection}/{year}/{stem}{ext}"

#: Characters Windows forbids in a filename, plus the ones that make a path
#: painful to handle in a shell or a script.
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class NamingError(Exception):
    """A template or mapping that must not be exported."""


def token(text: str, *, limit: int = 48) -> str:
    """A filename-safe token from arbitrary text."""
    cleaned = UNSAFE.sub("-", str(text or "")).strip(" .")
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return (cleaned or "untitled")[:limit]


@dataclass
class Rename:
    """One proposed rename, with the reason it is proposed."""

    file_id: int
    old_path: str
    new_relative: str
    reason: str
    collision: bool = False


@dataclass
class Mapping:
    """A reviewable rename plan and its inverse."""

    template: str
    renames: list[Rename] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def collisions(self) -> list[Rename]:
        return [entry for entry in self.renames if entry.collision]

    def forward_rows(self) -> list[list[str]]:
        return [
            [str(entry.file_id), entry.old_path, entry.new_relative, entry.reason]
            for entry in self.renames
        ]

    def inverse_rows(self) -> list[list[str]]:
        """The mapping back. Generated with the forward table, never after.

        Once the files are renamed the old names exist nowhere else, so this is
        the only record of how to undo it.
        """
        return [
            [str(entry.file_id), entry.new_relative, entry.old_path, "undo"]
            for entry in self.renames
        ]

    def assert_reversible(self) -> None:
        """Refuse a mapping that cannot be undone.

        Two files mapping to one name is the case that matters: applying it
        destroys one of them, and the inverse table would contain two rows
        claiming the same source. Reported as a refusal rather than a warning,
        because the whole value of the inverse is that it is complete.
        """
        targets: dict[str, str] = {}
        clashes = []
        for entry in self.renames:
            existing = targets.get(entry.new_relative)
            if existing is not None and existing != entry.old_path:
                clashes.append((existing, entry.old_path, entry.new_relative))
            targets[entry.new_relative] = entry.old_path

        if clashes:
            shown = "; ".join(
                f"{a} and {b} both become {target}" for a, b, target in clashes[:5]
            )
            raise NamingError(
                f"{len(clashes)} collision(s) make this mapping irreversible: "
                f"{shown}. Applying it would lose a file, and the undo table "
                "could not name which one. Add {parent} or {date} to the "
                "template so the names stay distinct."
            )

        sources = {entry.old_path for entry in self.renames}
        if len(sources) != len(self.renames):
            raise NamingError(
                "the same source path appears twice, so the undo table would "
                "be ambiguous"
            )


def _stem_token(stem: str) -> str:
    """A safe token that keeps a leading dot when the original had one.

    ``token`` strips leading dots, which it must: a name of ``.`` or ``..`` is
    a path component, not a filename. But a dotfile is an ordinary file whose
    leading dot is load-bearing, and turning ``.hidden`` into ``hidden`` is a
    change to how the file behaves — made silently, by a tool whose promise is
    that it changes nothing.
    """
    if stem.startswith(".") and stem.strip(".") :
        return "." + token(stem.lstrip("."), limit=95)
    return token(stem, limit=96)


def validate_template(template: str) -> None:
    """Refuse a template before it is used on thousands of files."""
    if not template or not template.strip():
        raise NamingError("the template is empty")

    unknown = [
        found
        for found in re.findall(r"\{[a-z_]+\}", template)
        if found not in PLACEHOLDERS
    ]
    if unknown:
        raise NamingError(
            f"unknown placeholder(s) {', '.join(unknown)}. Available: "
            + ", ".join(sorted(PLACEHOLDERS))
        )

    if "{stem}" not in template:
        # Without the original stem a name is not decodable back to what it was,
        # and the user loses the one piece of information they actually chose.
        raise NamingError(
            "the template must include {stem}, so every new name still "
            "contains the name its owner gave the file"
        )
    if ".." in template:
        raise NamingError("the template must not contain '..'")
    if template.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", template):
        raise NamingError("the template must be relative")


def _iso_date(mtime_ns) -> str:
    """A nanosecond timestamp as ``YYYY-MM-DD``, or empty when unrecorded.

    Empty rather than today's date: a file whose modification time was never
    captured must not be filed under the year the export happened to run.
    """
    if not mtime_ns:
        return ""
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(
            int(mtime_ns) / 1_000_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def build(
    catalog: Catalog,
    *,
    collection_id: int | None = None,
    template: str = DEFAULT_TEMPLATE,
    limit: int = 20_000,
) -> Mapping:
    """Render the template over a collection's files, or a sample of the catalog.

    Capped, because a mapping table nobody can read is not a review. The cap is
    reported in the bundle rather than silently applied.
    """
    validate_template(template)
    mapping = Mapping(template=template)

    if collection_id is not None:
        from drivefusion.core.curate import collections as collections_module

        collection = collections_module.get(catalog, collection_id)
        where, params = collections_module.member_file_filter(catalog, collection_id)
        context = {
            "collection": token(collection.title),
            "pid": token(collection.local_pid),
            "project": token(collection.project or "unfiled"),
        }
    else:
        collection = None
        where, params = "1", []
        context = {"collection": "unfiled", "pid": "unfiled", "project": "unfiled"}

    rows = catalog.conn.execute(
        "SELECT f.id, f.name, f.ext, f.dir_id, f.mtime_ns "
        f"FROM file f WHERE f.vanished_at IS NULL AND {where} "
        "ORDER BY f.id LIMIT ?",
        [*params, limit + 1],
    ).fetchall()

    truncated = len(rows) > limit
    for row in rows[:limit]:
        entry = dict(row)
        directory = (
            catalog.dir_path(int(entry["dir_id"])) if entry["dir_id"] else ""
        )
        if not directory:
            mapping.skipped.append((str(entry["id"]), "path could not be resolved"))
            continue
        old_path = f"{directory}/{entry['name']}"

        name = str(entry["name"] or "")
        # Split the real filename rather than trusting the stored ``ext``: the
        # catalog keeps it lowercased and *without* the dot, so using it
        # directly produced "m0csv" from "m0.csv" — every proposed name in
        # every rename plan had its extension welded on. A leading dot means a
        # hidden file, not an extension, hence rfind > 0.
        dot = name.rfind(".")
        if dot > 0:
            stem, extension = name[:dot], name[dot:]
        else:
            stem, extension = name, ""
        mtime = _iso_date(entry["mtime_ns"])
        parent = directory.rsplit("/", 1)[-1]

        values = dict(context)
        values.update(
            {
                "year": mtime[:4] or "undated",
                "month": mtime[5:7] or "00",
                "date": mtime[:10] or "undated",
                "stem": _stem_token(stem),
                "ext": extension,
                "parent": token(parent),
            }
        )

        rendered = template
        for placeholder in PLACEHOLDERS:
            rendered = rendered.replace(placeholder, values[placeholder.strip("{}")])

        mapping.renames.append(
            Rename(
                file_id=int(entry["id"]),
                old_path=old_path,
                new_relative=rendered.replace("\\", "/"),
                reason=(
                    f"template {template!r}"
                    + (f" for collection {collection.local_pid}" if collection else "")
                ),
            )
        )

    seen: dict[str, Rename] = {}
    for entry in mapping.renames:
        first = seen.get(entry.new_relative)
        if first is not None:
            first.collision = True
            entry.collision = True
        else:
            seen[entry.new_relative] = entry

    if truncated:
        mapping.skipped.append(
            (
                f"{limit}+",
                f"only the first {limit:,} files are shown; a table longer than "
                "that is not something a person can review",
            )
        )
    return mapping
