"""Collections: confirmed groupings, and membership by rule.

A collection is what the archive is *about*, as against where it sits. There is
no directory convention to import (§8.4), so collections are inferred and then
**confirmed by a person**. This module owns the confirmed side; ``candidates.py``
owns the proposing.

**Membership is a rule, not a list.** A confirmed subtree keeps collecting: a
file that lands inside it next month belongs to the collection without anyone
re-running anything. Materialising membership as rows would make that a batch
job that is always slightly out of date, and at 50M files it would also be the
largest table in the catalog for no gain.

**A local_pid is permanent.** Titles get rewritten and directories get moved;
the identifier is what exports, relations and history hang off, so it is
assigned once at confirmation and never regenerated from the title.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store.schema import utcnow

#: Fields FAIR F2 and R1 score for completeness, and what each is for.
DESCRIPTIVE_FIELDS = (
    ("title", "what this is"),
    ("description", "what it contains and why it exists"),
    ("creators_json", "who made it"),
    ("contact", "who to ask"),
    ("data_type", "what kind of data"),
)
REUSE_FIELDS = (
    ("description", "what it contains"),
    ("data_type", "what kind of data"),
    ("project", "what it belongs to"),
    ("retention_note", "how long it should be kept"),
)


class CollectionError(RuntimeError):
    """A collection operation that would leave the catalog inconsistent."""


@dataclass(frozen=True)
class Collection:
    id: int
    local_pid: str
    title: str
    license: str | None
    data_type: str | None
    description: str | None = None
    project: str | None = None
    ndsa_target: int | None = None
    min_copies: int | None = None
    row: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Collection":
        data = dict(row)
        return cls(
            id=int(data["id"]),
            local_pid=data["local_pid"],
            title=data["title"],
            license=data.get("license"),
            data_type=data.get("data_type"),
            description=data.get("description"),
            project=data.get("project"),
            ndsa_target=data.get("ndsa_target"),
            min_copies=data.get("min_copies"),
            row=data,
        )


def slugify(text: str) -> str:
    """A readable, stable fragment for a local identifier."""
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:48] or "collection"


def mint_pid(catalog: Catalog, title: str) -> str:
    """A persistent local identifier (FAIR F1).

    Shaped ``df:<slug>-<n>`` so it is readable, but the number — not the slug —
    is what makes it unique. Renaming the collection later does not change it:
    an identifier that tracked the title would break every reference that used
    it, which is the one thing an identifier must not do.
    """
    slug = slugify(title)
    row = catalog.conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS next FROM collection"
    ).fetchone()
    return f"df:{slug}-{int(row['next'])}"


# -- the confirmed side -------------------------------------------------------

def create(
    catalog: Catalog,
    *,
    title: str,
    confirmed_by: str = "user",
    **fields,
) -> Collection:
    """Confirm a collection into existence.

    Nothing reaches the ``collection`` table except through here, and this is
    only ever called from an explicit user action — accepting a candidate or
    creating one by hand. That is what makes "inference proposes, never
    assigns" true of the data and not just of the documentation.
    """
    if not title.strip():
        raise CollectionError("a collection needs a title")

    allowed = {
        "description", "creators_json", "contact", "license", "external_pid",
        "data_type", "project", "ndsa_target", "min_copies",
        "require_diversity", "require_offsite", "retention_note",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise CollectionError(f"unknown field(s): {', '.join(sorted(unknown))}")

    now = utcnow()
    local_pid = mint_pid(catalog, title)
    columns = ["local_pid", "title", "created_at", "confirmed_at", "confirmed_by"]
    values: list = [local_pid, title.strip(), now, now, confirmed_by]
    for name, value in fields.items():
        columns.append(name)
        values.append(value)

    cursor = catalog.conn.execute(
        f"INSERT INTO collection ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(values))})",
        values,
    )
    return get(catalog, int(cursor.lastrowid))


def get(catalog: Catalog, collection_id: int) -> Collection:
    row = catalog.conn.execute(
        "SELECT * FROM collection WHERE id = ?", (collection_id,)
    ).fetchone()
    if row is None:
        raise CollectionError(f"no collection {collection_id}")
    return Collection.from_row(row)


def by_pid(catalog: Catalog, local_pid: str) -> Collection | None:
    row = catalog.conn.execute(
        "SELECT * FROM collection WHERE local_pid = ?", (local_pid,)
    ).fetchone()
    return None if row is None else Collection.from_row(row)


def all_collections(catalog: Catalog) -> list[Collection]:
    return [
        Collection.from_row(row)
        for row in catalog.conn.execute("SELECT * FROM collection ORDER BY title")
    ]


EDITABLE_FIELDS = (
    "title", "description", "creators_json", "contact", "license",
    "external_pid", "data_type", "project", "ndsa_target", "min_copies",
    "require_diversity", "require_offsite", "retention_note",
)


def update(catalog: Catalog, collection_id: int, **fields) -> int:
    """Edit descriptive metadata. ``local_pid`` is deliberately not editable."""
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise CollectionError(
            f"not editable: {', '.join(sorted(unknown))}. The local_pid is "
            "permanent — exports and relations refer to it."
        )
    if not fields:
        return 0
    assignments = ", ".join(f"{name} = ?" for name in fields)
    cursor = catalog.conn.execute(
        f"UPDATE collection SET {assignments} WHERE id = ?",
        (*fields.values(), collection_id),
    )
    return cursor.rowcount


# -- rules and membership -----------------------------------------------------

def add_rule(
    catalog: Catalog,
    collection_id: int,
    *,
    kind: str = "subtree",
    pattern: str,
    note: str | None = None,
) -> int:
    if kind not in ("subtree", "glob", "extension"):
        raise CollectionError(f"unknown rule kind: {kind}")
    cursor = catalog.conn.execute(
        "INSERT INTO collection_rule (collection_id, kind, pattern, added_at, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (collection_id, kind, str(pattern), utcnow(), note),
    )
    return int(cursor.lastrowid)


def rules(catalog: Catalog, collection_id: int) -> list[dict]:
    return [
        dict(row)
        for row in catalog.conn.execute(
            "SELECT * FROM collection_rule WHERE collection_id = ? ORDER BY id",
            (collection_id,),
        )
    ]


#: Every directory at or beneath one root, by id. The pattern the catalog has
#: used since M1 for subtree work; collections resolve membership the same way
#: rather than storing a path prefix that a rename would invalidate.
SUBTREE_SQL = (
    "WITH RECURSIVE tree(id) AS ("
    "  SELECT id FROM dir WHERE id = ?"
    "  UNION ALL"
    "  SELECT d.id FROM dir d JOIN tree ON d.parent_id = tree.id"
    ") SELECT id FROM tree"
)


def subtree_dir_ids(catalog: Catalog, dir_id: int) -> list[int]:
    return [int(row["id"]) for row in catalog.conn.execute(SUBTREE_SQL, (dir_id,))]


def member_dir_ids(catalog: Catalog, collection_id: int) -> list[int]:
    """Directories the collection's subtree rules cover."""
    out: list[int] = []
    for rule in rules(catalog, collection_id):
        if rule["kind"] == "subtree":
            out.extend(subtree_dir_ids(catalog, int(rule["pattern"])))
    return out


def member_file_filter(catalog: Catalog, collection_id: int) -> tuple[str, list]:
    """A ``WHERE`` fragment and parameters selecting the collection's files.

    Returned as SQL rather than as a list of ids so counting a collection's
    bytes stays one statement. ``("0", [])`` — matching nothing — is returned
    for a collection with no rules, which is a real state: a confirmed
    collection whose rules were all removed contains nothing, and must not
    silently contain everything.
    """
    clauses: list[str] = []
    params: list = []

    dir_ids = member_dir_ids(catalog, collection_id)
    if dir_ids:
        clauses.append(f"f.dir_id IN ({','.join('?' * len(dir_ids))})")
        params.extend(dir_ids)

    for rule in rules(catalog, collection_id):
        if rule["kind"] == "extension":
            extensions = [e.strip().lower() for e in rule["pattern"].split(",") if e.strip()]
            if extensions:
                clauses.append(f"f.ext IN ({','.join('?' * len(extensions))})")
                params.extend(extensions)
        elif rule["kind"] == "glob":
            clauses.append("f.name GLOB ?")
            params.append(rule["pattern"])

    if not clauses:
        return "0", []
    return "(" + " OR ".join(clauses) + ")", params


def stats(catalog: Catalog, collection_id: int) -> dict:
    """Counts and bytes for a collection, resolved from its rules."""
    where, params = member_file_filter(catalog, collection_id)
    row = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(f.size_bytes), 0) AS bytes, "
        "  COUNT(f.content_id) AS identified, "
        "  MIN(f.mtime_ns) AS first_mtime, MAX(f.mtime_ns) AS last_mtime "
        f"FROM file f WHERE f.vanished_at IS NULL AND {where}",
        params,
    ).fetchone()
    return {
        "files": int(row["files"]),
        "bytes": int(row["bytes"]),
        "identified": int(row["identified"]),
        "first_mtime": row["first_mtime"],
        "last_mtime": row["last_mtime"],
    }


# -- relations ----------------------------------------------------------------

def add_relation(
    catalog: Catalog, collection_id: int, *, to_ref: str, relation_type: str
) -> int:
    from drivefusion.core import standards

    allowed = standards.vocabularies()["relation_types"]["allowed"]
    if relation_type not in allowed:
        raise CollectionError(
            f"{relation_type} is not a DataCite relationType in the shipped "
            f"vocabulary; allowed: {', '.join(allowed)}"
        )
    cursor = catalog.conn.execute(
        "INSERT INTO collection_relation (from_collection, to_ref, relation_type, "
        "added_at) VALUES (?, ?, ?, ?)",
        (collection_id, to_ref, relation_type, utcnow()),
    )
    return int(cursor.lastrowid)


def relations(catalog: Catalog, collection_id: int) -> list[dict]:
    return [
        dict(row)
        for row in catalog.conn.execute(
            "SELECT * FROM collection_relation WHERE from_collection = ? ORDER BY id",
            (collection_id,),
        )
    ]


# -- coverage: the unfiled bucket is first-class ------------------------------

def coverage(catalog: Catalog) -> dict:
    """How much of the archive is in a confirmed collection, and how much is not.

    At 50M files most bytes start unfiled, and hiding that would make every
    scorecard a lie — a FAIR grade over 12% of an archive is not a FAIR grade
    for the archive. "Unfiled" is also a legitimate permanent state for content
    nobody intends to curate, so it is reported, not nagged about.
    """
    total = catalog.conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes "
        "FROM file WHERE vanished_at IS NULL"
    ).fetchone()

    filed_files = 0
    filed_bytes = 0
    per_collection = []
    seen_dirs: set[int] = set()

    for collection in all_collections(catalog):
        item = stats(catalog, collection.id)
        # Overlapping rules would double-count, so directories are attributed
        # to the first collection claiming them and the overlap is reported.
        dir_ids = set(member_dir_ids(catalog, collection.id))
        overlap = dir_ids & seen_dirs
        seen_dirs |= dir_ids

        per_collection.append(
            {
                "id": collection.id,
                "local_pid": collection.local_pid,
                "title": collection.title,
                "files": item["files"],
                "bytes": item["bytes"],
                "overlapping_dirs": len(overlap),
            }
        )
        filed_files += item["files"]
        filed_bytes += item["bytes"]

    total_files = int(total["files"])
    total_bytes = int(total["bytes"])
    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "filed_files": min(filed_files, total_files),
        "filed_bytes": min(filed_bytes, total_bytes),
        "unfiled_files": max(0, total_files - filed_files),
        "unfiled_bytes": max(0, total_bytes - filed_bytes),
        "byte_share": (filed_bytes / total_bytes) if total_bytes else 0.0,
        "collections": per_collection,
        "overlap_warning": any(c["overlapping_dirs"] for c in per_collection),
    }


def unfiled_dir_filter(catalog: Catalog) -> tuple[str, list]:
    """A ``WHERE`` fragment selecting files in no confirmed collection."""
    claimed: list[int] = []
    for collection in all_collections(catalog):
        claimed.extend(member_dir_ids(catalog, collection.id))
    if not claimed:
        return "1", []
    return f"f.dir_id NOT IN ({','.join('?' * len(claimed))})", claimed


def creators(collection: Collection) -> list[str]:
    """Creator names, tolerating both a JSON list and a plain string."""
    raw = collection.row.get("creators_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return [str(raw)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]
