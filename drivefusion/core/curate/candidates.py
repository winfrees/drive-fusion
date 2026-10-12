"""Collection inference: proposing, with the evidence attached.

§8.4. There is no directory convention to import, so candidates are scored from
signals that survive at 50M files, and **every one of them is a proposal**. The
only path into the ``collection`` table is an explicit confirmation.

The signals, and why each one:

* **Size band.** A subtree of 40 bytes is not a collection and neither is a
  whole drive. The band is where "a body of work" usually lands.
* **Type homogeneity.** One project tends to be one kind of file. A directory of
  mixed everything is usually a container, not a collection.
* **Temporal clustering.** One project, one period. Modification times spread
  over nine years suggest a dumping ground.
* **Sibling-name patterns.** Dates, repeated tokens and sequence numbers across
  siblings mean somebody already imposed a structure; the collection is probably
  the parent, and the siblings are its items.
* **Depth.** Candidates are taken at the depth where a subtree stops looking
  like a container and starts looking like content.

Two rules about *not* proposing matter as much as the scoring:

* **Nothing already covered by a confirmed rule is proposed again.** It is
  already in a collection; re-offering it would invite a user to create a
  duplicate.
* **A rejected subtree stays rejected.** Re-proposing it after every scan would
  train the user to click through the queue without reading it, which is worse
  than not having a queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from drivefusion.core.curate import collections as collections_module
from drivefusion.core.store.catalog import Catalog
from drivefusion.core.store.schema import utcnow

#: Subtree sizes that look like a body of work rather than a stray folder or a
#: whole volume. Generous on purpose: this is a prior, not a filter.
MIN_BYTES = 1 * 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024 * 1024 * 1024
IDEAL_LOW = 50 * 1024 * 1024
IDEAL_HIGH = 500 * 1024 * 1024 * 1024

#: A subtree needs at least this many files to be worth describing.
MIN_FILES = 8

#: Candidates are not proposed below this score. Low enough to surface the
#: unobvious, high enough that the queue is readable.
MIN_CONFIDENCE = 0.35

#: Directory names that are structure rather than subject matter.
CONTAINER_NAMES = frozenset(
    {
        "", "data", "files", "documents", "downloads", "desktop", "users",
        "home", "temp", "tmp", "misc", "stuff", "new folder", "untitled",
        "backup", "backups", "archive", "archives", "old", "copy", "copies",
        "work", "projects", "shared", "public", "volumes", "media", "mnt",
    }
)

_DATE_TOKEN = re.compile(r"(19|20)\d{2}([-_.]?(0[1-9]|1[0-2]))?")
_SEQUENCE = re.compile(r"\d{2,}")
_NANOSECONDS_PER_DAY = 86_400 * 1_000_000_000


@dataclass(frozen=True)
class Signal:
    """One scored observation, with words a person can check."""

    name: str
    score: float
    weight: float
    detail: str


@dataclass(frozen=True)
class Candidate:
    """A proposed collection and the reasoning behind it."""

    dir_id: int
    path: str
    proposed_title: str
    files: int
    bytes: int
    confidence: float
    signals: tuple[Signal, ...] = ()
    sample_paths: tuple[str, ...] = ()
    date_span_days: int | None = None
    top_extensions: tuple[tuple[str, int], ...] = ()

    def evidence(self) -> dict:
        """Everything the proposal rests on, for storage and for display.

        A proposal you cannot audit is a proposal you cannot safely accept, so
        this is stored with the candidate rather than recomputed later — the
        catalog may have changed by the time somebody reviews the queue.
        """
        return {
            "path": self.path,
            "files": self.files,
            "bytes": self.bytes,
            "confidence": round(self.confidence, 4),
            "date_span_days": self.date_span_days,
            "top_extensions": [list(item) for item in self.top_extensions],
            "sample_paths": list(self.sample_paths),
            "signals": [
                {
                    "name": s.name,
                    "score": round(s.score, 4),
                    "weight": s.weight,
                    "detail": s.detail,
                }
                for s in self.signals
            ],
        }

    def why(self) -> str:
        """One line per signal, strongest first."""
        ordered = sorted(
            self.signals, key=lambda s: s.score * s.weight, reverse=True
        )
        return "; ".join(f"{s.detail}" for s in ordered if s.score > 0)


def _band_score(size: int) -> tuple[float, str]:
    if size < MIN_BYTES or size > MAX_BYTES:
        return 0.0, f"{size:,} bytes is outside the plausible range"
    if IDEAL_LOW <= size <= IDEAL_HIGH:
        return 1.0, "size sits in the band a body of work usually occupies"
    if size < IDEAL_LOW:
        return 0.5, "smaller than most collections, but not implausibly so"
    return 0.6, "larger than most collections; may be several"


def _homogeneity(extensions: list[tuple[str, int]], total: int) -> tuple[float, str]:
    if not extensions or not total:
        return 0.0, "no file types recorded"
    top_ext, top_count = extensions[0]
    share = top_count / total
    label = top_ext or "no extension"
    if share >= 0.8:
        return 1.0, f"{share:.0%} of files are .{label} — one kind of thing"
    if share >= 0.5:
        return 0.7, f"{share:.0%} of files are .{label}"
    if share >= 0.3:
        return 0.4, f"mixed types, most common .{label} at {share:.0%}"
    return 0.15, "many different file types — looks like a container"


def _temporal(span_days: int | None) -> tuple[float, str]:
    if span_days is None:
        return 0.0, "no modification times recorded"
    if span_days <= 120:
        return 1.0, f"everything modified within {span_days} days — one period"
    if span_days <= 730:
        return 0.7, f"modified over about {span_days // 30} months"
    if span_days <= 2200:
        return 0.4, f"modified over about {span_days // 365} years"
    return 0.1, f"modified across {span_days // 365} years — likely accumulated"


def _name_score(name: str, siblings: list[str]) -> tuple[float, str]:
    lowered = name.strip().lower()
    if lowered in CONTAINER_NAMES:
        return 0.0, f"'{name}' names a container, not a subject"

    patterned = [s for s in siblings if _DATE_TOKEN.search(s) or _SEQUENCE.search(s)]
    if siblings and len(patterned) >= max(2, int(0.6 * len(siblings))):
        return (
            0.9,
            f"{len(patterned)} of {len(siblings)} sibling folders follow a "
            "dated or numbered pattern, so this looks like their parent",
        )
    if _DATE_TOKEN.search(name):
        return 0.6, f"'{name}' carries a date, which usually marks one body of work"
    return 0.4, f"'{name}' reads as a subject rather than a container"


def _child_names(catalog: Catalog, dir_id: int) -> list[str]:
    return [
        row["name"]
        for row in catalog.conn.execute(
            "SELECT name FROM dir WHERE parent_id = ? AND vanished_at IS NULL "
            "ORDER BY name LIMIT 200",
            (dir_id,),
        )
    ]


def _extensions(catalog: Catalog, dir_ids: list[int]) -> list[tuple[str, int]]:
    if not dir_ids:
        return []
    placeholders = ",".join("?" * len(dir_ids))
    return [
        (row["ext"] or "", int(row["n"]))
        for row in catalog.conn.execute(
            f"SELECT ext, COUNT(*) AS n FROM file "
            f"WHERE vanished_at IS NULL AND dir_id IN ({placeholders}) "
            "GROUP BY ext ORDER BY n DESC LIMIT 10",
            dir_ids,
        )
    ]


def _span_and_samples(
    catalog: Catalog, dir_ids: list[int]
) -> tuple[int | None, tuple[str, ...]]:
    if not dir_ids:
        return None, ()
    placeholders = ",".join("?" * len(dir_ids))
    row = catalog.conn.execute(
        f"SELECT MIN(mtime_ns) AS lo, MAX(mtime_ns) AS hi FROM file "
        f"WHERE vanished_at IS NULL AND dir_id IN ({placeholders})",
        dir_ids,
    ).fetchone()

    span = None
    if row and row["lo"] is not None and row["hi"] is not None:
        span = int((int(row["hi"]) - int(row["lo"])) // _NANOSECONDS_PER_DAY)

    # Paths, not bare names: sibling folders of one project routinely hold
    # identically named files, and a sample list reading "scan-011.tif" three
    # times tells a reviewer nothing about what the subtree contains.
    samples = tuple(
        f"{catalog.dir_path(int(r['dir_id']))}/{r['name']}".replace("//", "/")
        for r in catalog.conn.execute(
            f"SELECT dir_id, name FROM file WHERE vanished_at IS NULL "
            f"AND dir_id IN ({placeholders}) ORDER BY size_bytes DESC, name LIMIT 5",
            dir_ids,
        )
    )
    return span, samples


def _covered_dirs(catalog: Catalog) -> set[int]:
    """Directories already inside a confirmed collection, or already decided.

    Both are reasons not to propose: the first is already filed, and the second
    was already shown to a person who said no.
    """
    covered: set[int] = set()
    for collection in collections_module.all_collections(catalog):
        covered |= set(collections_module.member_dir_ids(catalog, collection.id))

    covered |= {
        int(row["dir_id"])
        for row in catalog.conn.execute(
            "SELECT dir_id FROM collection_candidate WHERE status != 'pending'"
        )
    }
    return covered


def propose(
    catalog: Catalog,
    *,
    limit: int = 50,
    min_confidence: float = MIN_CONFIDENCE,
    max_depth: int = 6,
) -> list[Candidate]:
    """Score subtrees and return the best proposals. Writes nothing.

    Reads ``dir_rollup``, which the scanner already maintains, so this is a
    query over a few hundred thousand directories rather than a walk over 50M
    files.
    """
    covered = _covered_dirs(catalog)

    rows = catalog.conn.execute(
        "SELECT d.id, d.name, d.depth, d.parent_id, "
        "       r.subtree_files, r.subtree_bytes "
        "FROM dir d JOIN dir_rollup r ON r.dir_id = d.id "
        "WHERE d.vanished_at IS NULL AND d.depth <= ? "
        "  AND r.subtree_files >= ? AND r.subtree_bytes BETWEEN ? AND ? "
        "ORDER BY r.subtree_bytes DESC LIMIT 2000",
        (max_depth, MIN_FILES, MIN_BYTES, MAX_BYTES),
    ).fetchall()

    candidates: list[Candidate] = []
    for row in rows:
        dir_id = int(row["id"])
        if dir_id in covered:
            continue

        dir_ids = collections_module.subtree_dir_ids(catalog, dir_id)
        extensions = _extensions(catalog, dir_ids)
        files = int(row["subtree_files"])
        size = int(row["subtree_bytes"])
        span, samples = _span_and_samples(catalog, dir_ids)

        band, band_detail = _band_score(size)
        homogeneity, homogeneity_detail = _homogeneity(extensions, files)
        temporal, temporal_detail = _temporal(span)
        naming, naming_detail = _name_score(row["name"], _child_names(catalog, dir_id))

        signals = (
            Signal("size", band, 1.0, band_detail),
            Signal("homogeneity", homogeneity, 1.5, homogeneity_detail),
            Signal("temporal", temporal, 1.2, temporal_detail),
            Signal("naming", naming, 1.3, naming_detail),
        )
        weight = sum(s.weight for s in signals)
        confidence = sum(s.score * s.weight for s in signals) / weight

        if confidence < min_confidence:
            continue

        path = catalog.dir_path(dir_id)
        candidates.append(
            Candidate(
                dir_id=dir_id,
                path=path,
                proposed_title=row["name"] or path,
                files=files,
                bytes=size,
                confidence=confidence,
                signals=signals,
                sample_paths=samples,
                date_span_days=span,
                top_extensions=tuple(extensions[:5]),
            )
        )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return _drop_nested(candidates)[:limit]


def _is_within(inner: str, outer: str) -> bool:
    """True when ``inner`` is ``outer`` or sits beneath it.

    Compared with a separator guard so ``/data/project-2`` is not treated as
    living inside ``/data/project``.
    """
    if inner == outer:
        return True
    for separator in ("/", "\\"):
        if inner.startswith(outer.rstrip(separator) + separator):
            return True
    return False


def _drop_nested(candidates: list[Candidate]) -> list[Candidate]:
    """Keep the strongest candidate on any path, not its relatives as well.

    Candidates arrive in confidence order, so the first on a path wins and
    anything above or below it is suppressed. Both directions matter: without
    this a project folder *and* each of its five dated sub-folders appear in the
    queue, accepting the parent leaves five stale proposals behind, and
    accepting all six creates six overlapping collections over the same bytes.

    An earlier version of this function deduplicated on ``dir_id``, which is
    unique by construction — so it did nothing at all, and the queue showed
    every level of the tree.
    """
    kept: list[Candidate] = []
    for candidate in candidates:
        if any(
            _is_within(candidate.path, other.path)
            or _is_within(other.path, candidate.path)
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


# -- the queue ----------------------------------------------------------------

def record(catalog: Catalog, candidates: list[Candidate]) -> int:
    """Store proposals in the review queue. Still not collections."""
    stored = 0
    for candidate in candidates:
        existing = catalog.conn.execute(
            "SELECT id, status FROM collection_candidate WHERE dir_id = ?",
            (candidate.dir_id,),
        ).fetchone()
        if existing is not None:
            # A pending proposal is refreshed; a decided one is left alone, so
            # a rejection is not undone by the next scan.
            if existing["status"] == "pending":
                catalog.conn.execute(
                    "UPDATE collection_candidate SET proposed_title = ?, "
                    "confidence = ?, evidence_json = ?, files = ?, bytes = ?, "
                    "proposed_at = ? WHERE id = ?",
                    (
                        candidate.proposed_title,
                        candidate.confidence,
                        json.dumps(candidate.evidence()),
                        candidate.files,
                        candidate.bytes,
                        utcnow(),
                        existing["id"],
                    ),
                )
                stored += 1
            continue

        catalog.conn.execute(
            "INSERT INTO collection_candidate (dir_id, proposed_title, confidence, "
            "evidence_json, files, bytes, proposed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.dir_id,
                candidate.proposed_title,
                candidate.confidence,
                json.dumps(candidate.evidence()),
                candidate.files,
                candidate.bytes,
                utcnow(),
            ),
        )
        stored += 1
    return stored


def pending(catalog: Catalog, limit: int = 100) -> list[dict]:
    return [
        dict(row)
        for row in catalog.conn.execute(
            "SELECT * FROM collection_candidate WHERE status = 'pending' "
            "ORDER BY confidence DESC, bytes DESC LIMIT ?",
            (limit,),
        )
    ]


def accept(
    catalog: Catalog,
    candidate_id: int,
    *,
    title: str | None = None,
    confirmed_by: str = "user",
    **fields,
) -> collections_module.Collection:
    """Turn a proposal into a confirmed collection. An explicit act.

    The subtree becomes a membership *rule*, so content that appears inside it
    later joins the collection without anyone revisiting this decision.
    """
    row = catalog.conn.execute(
        "SELECT * FROM collection_candidate WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise collections_module.CollectionError(f"no candidate {candidate_id}")
    if row["status"] != "pending":
        raise collections_module.CollectionError(
            f"candidate {candidate_id} was already {row['status']}"
        )

    collection = collections_module.create(
        catalog,
        title=title or row["proposed_title"],
        confirmed_by=confirmed_by,
        **fields,
    )
    collections_module.add_rule(
        catalog,
        collection.id,
        kind="subtree",
        pattern=str(row["dir_id"]),
        note=f"accepted from candidate {candidate_id}",
    )
    catalog.conn.execute(
        "UPDATE collection_candidate SET status = 'accepted', decided_at = ?, "
        "collection_id = ? WHERE id = ?",
        (utcnow(), collection.id, candidate_id),
    )
    return collection


def reject(catalog: Catalog, candidate_id: int) -> bool:
    """Remember a no, so the same subtree is not offered after every scan."""
    cursor = catalog.conn.execute(
        "UPDATE collection_candidate SET status = 'rejected', decided_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (utcnow(), candidate_id),
    )
    return cursor.rowcount > 0


def merge_into(
    catalog: Catalog, candidate_id: int, collection_id: int
) -> collections_module.Collection:
    """Add a proposal's subtree to an existing collection rather than a new one."""
    row = catalog.conn.execute(
        "SELECT * FROM collection_candidate WHERE id = ? AND status = 'pending'",
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise collections_module.CollectionError(
            f"no pending candidate {candidate_id}"
        )
    collection = collections_module.get(catalog, collection_id)
    collections_module.add_rule(
        catalog,
        collection_id,
        kind="subtree",
        pattern=str(row["dir_id"]),
        note=f"merged from candidate {candidate_id}",
    )
    catalog.conn.execute(
        "UPDATE collection_candidate SET status = 'accepted', decided_at = ?, "
        "collection_id = ? WHERE id = ?",
        (utcnow(), collection_id, candidate_id),
    )
    return collection
