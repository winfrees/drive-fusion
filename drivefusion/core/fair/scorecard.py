"""FAIR and NDSA scorecards per collection (PLAN §8.3, ROADMAP M6.7).

Each criterion of a rubric (``core/fair/rubrics.py``) gets one of five
results, and every result but a pass names the gap and what would close it:

* ``pass``, ``partial``, ``fail``: the catalog holds evidence either way;
* ``planned``: the capability that would show it is not built yet (exports,
  format identification, the planner), so it counts neither way;
* ``judgement``: Drive Fusion cannot see it (who may write the drives,
  virus checks, stable storage), so it is listed for the person to decide.

A FAIR score is the share of assessed criteria met (partial counts half).
An NDSA result is a level per functional area: the highest level whose
assessed statements, and every lower level's, all pass. Statements left to
judgement or planned don't hold a level back, and are listed with it; an
area with nothing Drive Fusion can assess (control) has no level at all.

A scorecard says a collection is *consistent with* a rubric's criteria, to a
degree; it certifies nothing. It names the rubric version it used and whether
that rubric's wording has been checked or reviewed (ROADMAP D14), and how much of the archive is in
collections at all (M6.4): a scorecard over 12 % of the bytes is not a
scorecard over the archive.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass, field

from drivefusion.core.curate import confirm
from drivefusion.core.fair import rubrics
from drivefusion.core.health import metadata
from drivefusion.core.store import curation

PASS, PARTIAL, FAIL, PLANNED, JUDGEMENT = "pass", "partial", "fail", "planned", "judgement"
ASSESSED = (PASS, PARTIAL, FAIL)

#: A verification older than this no longer counts as "at fixed intervals".
VERIFY_WITHIN_DAYS = 365


@dataclass
class Result:
    criterion: str
    status: str
    detail: str = ""
    remedy: str = ""


@dataclass
class Scorecard:
    rubric: str                     # e.g. fair-2016
    rubric_accepted: bool
    collection_id: int
    title: str
    results: list[Result] = field(default_factory=list)
    levels: dict[str, int | None] = field(default_factory=dict)  # NDSA; None: not assessed
    rubric_retrieval: str = "unconfirmed"     # confirmed | reviewed | unconfirmed

    def result(self, criterion: str) -> Result:
        return next(r for r in self.results if r.criterion == criterion)

    @property
    def score(self) -> float | None:
        assessed = [r for r in self.results if r.status in ASSESSED]
        if not assessed:
            return None
        return sum({PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}[r.status] for r in assessed) / len(assessed)


# -- the evidence, gathered once per collection ---------------------------------------

@dataclass
class Evidence:
    row: object
    files: int
    items: int
    hashed_items: int
    verified_items: int             # an ok fixity check within VERIFY_WITHIN_DAYS
    ever_verified: bool
    unidentified_items: int         # a copy on a volume whose drive is unverified
    copies: list[int]               # proven drives per item
    zones: list[int]                # distinct recorded zones per item
    unknown_zone_items: int
    batches: list[int]              # distinct batch keys per item
    drives: dict[int, object]
    relations: int
    completeness: tuple[int, int, list[str]]


def gather(catalog, collection_id: int, *, today: dt.date | None = None) -> Evidence:
    row = curation.collection(catalog, collection_id)
    if row is None:
        raise curation.CurationError(f"no collection {collection_id}")
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=VERIFY_WITHIN_DAYS)).isoformat()
    tree = curation._MEMBER_DIRS
    members = catalog.conn.execute(
        tree + " SELECT f.id, f.content_id, v.drive_id FROM file f "
        "JOIN volume v ON v.id = f.volume_id "
        "WHERE f.vanished_at IS NULL AND f.dir_id IN (SELECT id FROM tree)",
        (collection_id,)).fetchall()

    # An item is a content, or a file with none; its copies are counted over
    # every live path to it, inside the collection or not.
    item_files: dict[object, list[int]] = defaultdict(list)
    for m in members:
        item_files[m["content_id"] if m["content_id"] is not None else ("f", m["id"])].append(m["id"])
    content_ids = [k for k in item_files if not isinstance(k, tuple)]
    holders: dict[object, set] = defaultdict(set)
    unidentified: set = set()
    for chunk in range(0, len(content_ids), 500):
        part = content_ids[chunk:chunk + 500]
        for r in catalog.conn.execute(
            "SELECT f.content_id, v.drive_id FROM file f JOIN volume v ON v.id = f.volume_id "
            f"WHERE f.vanished_at IS NULL AND f.content_id IN ({','.join('?' * len(part))})",
            part):
            if r["drive_id"] is None:
                unidentified.add(r["content_id"])
            else:
                holders[r["content_id"]].add(r["drive_id"])
    for m in members:
        if m["content_id"] is None:
            key = ("f", m["id"])
            if m["drive_id"] is None:
                unidentified.add(key)
            else:
                holders[key].add(m["drive_id"])

    drive_ids = {d for ds in holders.values() for d in ds}
    drives = {d: catalog.drive(d) for d in drive_ids}
    zones, batches, copies, unknown_zone = [], [], [], 0
    for key in item_files:
        ds = holders.get(key, set())
        copies.append(max(1, len(ds)))
        recorded = {drives[d]["disaster_zone"] for d in ds if drives[d]["disaster_zone"]}
        if len(recorded) < len(ds):
            unknown_zone += 1
        zones.append(len(recorded))
        batches.append(len({metadata.batch_key(drives[d])[0] or f"drive-{d}" for d in ds}))

    verified = {r["content_id"] for r in catalog.conn.execute(
        "SELECT DISTINCT content_id FROM fixity_check WHERE result = 'ok' AND checked_at >= ?",
        (since,))}
    ever = catalog.conn.execute(
        tree + " SELECT 1 FROM fixity_check fc JOIN file f ON f.content_id = fc.content_id "
        "WHERE f.vanished_at IS NULL AND f.dir_id IN (SELECT id FROM tree) LIMIT 1",
        (collection_id,)).fetchone() is not None
    return Evidence(
        row=row, files=len(members), items=len(item_files),
        hashed_items=len(content_ids),
        verified_items=sum(1 for c in content_ids if c in verified),
        ever_verified=ever,
        unidentified_items=sum(1 for k in item_files if k in unidentified and not holders.get(k)),
        copies=copies, zones=zones, unknown_zone_items=unknown_zone, batches=batches,
        drives=drives,
        relations=catalog.conn.execute(
            "SELECT COUNT(*) FROM collection_relation WHERE from_collection = ?",
            (collection_id,)).fetchone()[0],
        completeness=confirm.completeness(row),
    )


def _share(n: int, of: int) -> str:
    return f"{n:,} of {of:,}"


def _all(n: int, of: int, *, partial_from: int = 1) -> str:
    if of == 0:
        return FAIL
    return PASS if n == of else PARTIAL if n >= partial_from else FAIL


# -- FAIR ---------------------------------------------------------------------------

def fair(catalog, collection_id: int, *, today: dt.date | None = None) -> Scorecard:
    e = gather(catalog, collection_id, today=today)
    rubric = rubrics.current("fair")
    filled, total, missing = e.completeness
    row = e.row
    card = Scorecard(rubric.name, rubric.accepted, collection_id, row["title"],
                     rubric_retrieval=rubric.retrieval)
    add = lambda *a: card.results.append(Result(*a))  # noqa: E731

    add("F1", PASS if row["local_pid"] else FAIL,
        f"local PID {row['local_pid']}; {_share(e.hashed_items, e.items)} items carry a "
        "content hash", "" if e.hashed_items == e.items else
        "files of unique size are never hashed; they are named by PID and path only")
    meta = PASS if not missing else PARTIAL if filled >= 4 else FAIL
    add("F2", meta, f"{filled} of {total} descriptive fields filled",
        f"fill in: {', '.join(missing)} (`drivefusion collections edit`)" if missing else "")
    add("F3", PLANNED, "no manifest binds the PID to checksums, paths and drives yet",
        "per-drive manifests arrive with exports (M8)")
    add("F4", PARTIAL, "indexed in the local catalog (`drivefusion find`)",
        "a JSON-LD export for external indexing arrives with exports (M8)")
    located = e.items - e.unidentified_items
    add("A1", _all(located, e.items),
        f"{_share(located, e.items)} items are on a drive the catalog can name, connected or not",
        "" if located == e.items else "confirm which drive the unverified volumes are on "
        "(`drivefusion drives`)")
    add("A1.1", JUDGEMENT, "files on local drives; no retrieval protocol is involved",
        "decide whether a protocol applies when the collection is shared")
    add("A1.2", JUDGEMENT, "as A1.1", "")
    add("A2", PASS, "the catalog keeps a tombstone, with metadata, for every file that vanishes")
    add("I1", PLANNED, "no RO-Crate or DataCite description is written yet",
        "arrives with exports (M8)")
    add("I2", PLANNED, "file formats are not yet identified against PRONOM",
        "format identification is not built yet")
    add("I3", PASS if e.relations else FAIL,
        f"{e.relations} qualified relation(s) recorded",
        "" if e.relations else "record how this collection relates to others "
        "(IsVersionOf, IsPartOf, ...)")
    add("R1", meta, f"{filled} of {total} descriptive fields filled",
        f"fill in: {', '.join(missing)}" if missing else "")
    add("R1.1", PASS if row["license"] else FAIL,
        f"license {row['license']}" if row["license"] else "no license recorded: the data "
        "is not reusable as it stands",
        "" if row["license"] else "`drivefusion collections edit ID --license ...`")
    provenance = (PASS if e.items and e.verified_items == e.hashed_items == e.items
                  else PARTIAL if e.ever_verified or e.hashed_items else FAIL)
    add("R1.2", provenance,
        f"scan history and the audit log; {_share(e.verified_items, e.items)} items verified "
        f"in the last {VERIFY_WITHIN_DAYS} days",
        "" if provenance == PASS else "hash and verify the collection "
        "(`drivefusion hash`, `drivefusion verify`)")
    add("R1.3", PLANNED, "format-risk flags need format identification",
        "format identification is not built yet")
    return card


# -- NDSA ---------------------------------------------------------------------------

def ndsa(catalog, collection_id: int, *, today: dt.date | None = None) -> Scorecard:
    e = gather(catalog, collection_id, today=today)
    rubric = rubrics.current("ndsa")
    filled, total, missing = e.completeness
    card = Scorecard(rubric.name, rubric.accepted, collection_id, e.row["title"],
                     rubric_retrieval=rubric.retrieval)
    add = lambda *a: card.results.append(Result(*a))  # noqa: E731
    n = e.items

    def count(test) -> int:
        return sum(1 for i in range(n) if test(i))

    two = count(lambda i: e.copies[i] >= 2)
    add("storage-1a", _all(two, n), f"{_share(two, n)} items on two or more drives",
        "" if two == n else "copy the single-copy items to a second drive")
    known = n - e.unidentified_items
    add("storage-1b", _all(known, n), f"{_share(known, n)} items on drives the catalog names",
        "" if known == n else "confirm the unverified volumes' drives")
    add("storage-1c", JUDGEMENT, "whether the storage is stable", "")
    geo = count(lambda i: e.copies[i] >= 3 and e.zones[i] >= 2)
    add("storage-2a", _all(geo, n),
        f"{_share(geo, n)} items on three drives with two recorded disaster zones",
        "" if geo == n else "a third copy in another zone; record zones with "
        "`drivefusion drives edit ID --zone ...`")
    add("storage-2b", JUDGEMENT, "the resources and dependencies the media need", "")
    apart = count(lambda i: e.zones[i] >= 2)
    add("storage-3a", _all(apart, n), f"{_share(apart, n)} items in two or more disaster zones",
        "" if apart == n else "keep a copy where a different disaster threatens it")
    dated = sum(1 for d in e.drives.values() if d["purchase_date"])
    add("storage-3b", _all(dated, len(e.drives)),
        f"{_share(dated, len(e.drives))} holding drives have a purchase date, so their age "
        "and failure rate are tracked",
        "confirm the unverified volumes' drives, then record their purchase dates"
        if not e.drives else
        "" if dated == len(e.drives) else "record purchase dates (`drivefusion drives edit`)")
    three_zones = count(lambda i: e.zones[i] >= 3)
    add("storage-4a", _all(three_zones, n), f"{_share(three_zones, n)} items in three zones",
        "" if three_zones == n else "three copies, each under a different disaster threat")
    diverse = count(lambda i: e.batches[i] >= 2)
    add("storage-4b", _all(diverse, n), f"{_share(diverse, n)} items on two or more batches "
        "or models", "" if diverse == n else "avoid holding every copy on one batch")
    add("storage-4c", PLANNED, "acting on obsolescence", "the planner arrives in M7")

    add("integrity-1a", JUDGEMENT, "integrity information that came with the content", "")
    add("integrity-1b", _all(e.hashed_items, n),
        f"{_share(e.hashed_items, n)} items have a content hash",
        "" if e.hashed_items == n else "files of unique size are not hashed by design; "
        "their size and path identify them")
    add("integrity-1c", JUDGEMENT, "virus checking", "")
    add("integrity-2a", JUDGEMENT, "Drive Fusion never moves or copies content", "")
    add("integrity-2b", PASS, "Drive Fusion opens every drive read-only, by construction")
    add("integrity-2c", PASS, "the catalog, with the hashes, lives off the scanned drives")
    recent = e.verified_items
    add("integrity-3a", _all(recent, n),
        f"{_share(recent, n)} items verified in the last {VERIFY_WITHIN_DAYS} days",
        "" if recent == n else "run `drivefusion verify` on a schedule")
    add("integrity-3b", PASS if e.ever_verified else FAIL,
        "verification outcomes are recorded per file" if e.ever_verified
        else "never verified", "" if e.ever_verified else "run `drivefusion verify`")
    add("integrity-3c", PASS if e.hashed_items else FAIL, "`drivefusion verify` on demand",
        "" if e.hashed_items else "hash the collection first")
    add("integrity-4a", JUDGEMENT, "verification triggered by events", "")
    add("integrity-4b", PLANNED, "repairing from a good copy", "the planner arrives in M7")

    for cid in ("control-1a", "control-2a", "control-3a", "control-4a"):
        add(cid, JUDGEMENT, "who may read, write, move and delete is outside what Drive "
            "Fusion can see", "")

    add("metadata-1a", PASS, f"{e.files:,} files inventoried with their drives")
    add("metadata-1b", PASS, "the catalog lives off the scanned drives")
    add("metadata-2a", PASS if not missing else PARTIAL if filled >= 4 else FAIL,
        f"{filled} of {total} descriptive fields filled",
        f"fill in: {', '.join(missing)}" if missing else "")
    add("metadata-3a", JUDGEMENT, "which metadata standards apply", "")
    add("metadata-3b", PASS if not missing else FAIL, "gaps against the collection profile",
        f"fill in: {', '.join(missing)}" if missing else "")
    add("metadata-4a", PASS, "every scan, hash, verification and decision is in the audit log")
    add("metadata-4b", PLANNED, "writing the chosen standards", "exports arrive in M8")

    add("content-1a", PLANNED, "documenting file formats", "format identification is not "
        "built yet")
    add("content-2a", PLANNED, "verifying file formats", "format identification is not "
        "built yet")
    add("content-2b", JUDGEMENT, "relationships with content creators", "")
    add("content-3a", PLANNED, "monitoring formats for obsolescence", "not built yet")
    add("content-4a", JUDGEMENT, "migration or emulation: Drive Fusion never converts", "")

    for group in rubric.groups:
        if not any(card.result(c.id).status in ASSESSED
                   for c in rubric.criteria if c.group == group):
            card.levels[group] = None          # nothing Drive Fusion can assess
            continue
        reached = 0
        for level in (1, 2, 3, 4):
            statuses = [card.result(c.id).status for c in rubric.criteria
                        if c.group == group and c.level == level]
            if any(s in (PARTIAL, FAIL) for s in statuses):
                break
            reached = level
        card.levels[group] = reached
    return card


def score(catalog, collection_id: int, rubric_id: str, **kw) -> Scorecard:
    return {"fair": fair, "ndsa": ndsa}[rubric_id](catalog, collection_id, **kw)


def to_json(card: Scorecard) -> str:
    return json.dumps({"rubric": card.rubric, "rubric_accepted": card.rubric_accepted,
                       "rubric_retrieval": card.rubric_retrieval,
                       "collection_id": card.collection_id, "score": card.score,
                       "levels": card.levels,
                       "results": [r.__dict__ for r in card.results]}, indent=1)
