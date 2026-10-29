"""Tiered hashing: decide what to open, and open as little as possible.

The tiers are described in ``hasher.py``. This module owns the decisions:
which files are candidates, in what order they are read, and how a hash
becomes content identity in the catalog.

Two properties matter more than speed:

* **Never claim identity that was not established.** A file whose hash could
  not be computed keeps ``content_id IS NULL`` rather than being folded in with
  something that merely looks similar.
* **Never open what must not be opened.** Cloud placeholders and unreadable
  files are excluded by the candidate query and re-checked at the gateway.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from drivefusion.core import fsio
from drivefusion.core.errors import DehydratedFileError, UnreadableError
from drivefusion.core.identity import hasher
from drivefusion.core.store.catalog import Catalog, root_clause

#: Commit after this many assignments so an interrupted pass keeps its work.
CHECKPOINT_ROWS = 5_000


@dataclass
class HashCounters:
    candidates: int = 0
    quick_hashed: int = 0
    full_hashed: int = 0
    quick_bytes: int = 0
    full_bytes: int = 0
    unreadable: int = 0
    dehydrated: int = 0
    resized: int = 0
    unreadable_paths: list[str] = field(default_factory=list)

    @property
    def bytes_read(self) -> int:
        return self.quick_bytes + self.full_bytes

    def summary(self) -> str:
        parts = [
            f"{self.candidates:,} candidates",
            f"{self.quick_hashed:,} quick-hashed",
        ]
        if self.full_hashed:
            parts.append(f"{self.full_hashed:,} fully hashed")
        if self.unreadable:
            parts.append(f"{self.unreadable:,} unreadable")
        if self.dehydrated:
            parts.append(f"{self.dehydrated:,} cloud placeholders skipped")
        if self.resized:
            parts.append(f"{self.resized:,} changed size while reading")
        return "; ".join(parts)


def _path_of(catalog: Catalog, row) -> str:
    return os.path.join(catalog.dir_path(int(row["dir_id"]), os.sep), row["name"])


def hash_pass(
    catalog: Catalog,
    *,
    volume_id: int | None = None,
    limit: int | None = None,
    progress: Callable[[HashCounters], None] | None = None,
    root_ids=None,
    cancel: Callable[[], None] | None = None,
) -> HashCounters:
    """Establish content identity for everything that needs it.

    Runs tier 1 over the candidates, then tier 2 over the groups tier 1 could
    not separate. Both passes read through the read-only gateway.

    ``root_ids``, when given, confines every read to files under those roots.
    Callers pass the roots whose volume they have just confirmed
    (``core.scope.bind_roots``), so bytes from whatever drive now holds a
    letter are never recorded as another drive's content.
    """
    counters = HashCounters()
    candidates = catalog.hash_candidates(
        volume_id=volume_id, limit=limit, root_ids=root_ids
    )
    counters.candidates = len(candidates)

    _quick_pass(catalog, candidates, counters, progress, cancel)
    _full_pass(catalog, counters, progress, root_ids, cancel)
    catalog.analyze()
    catalog.record("hash.pass", {
        "candidates": counters.candidates, "quick_hashed": counters.quick_hashed,
        "full_hashed": counters.full_hashed, "unreadable": counters.unreadable,
        "dehydrated": counters.dehydrated,
        "root_ids": sorted(root_ids) if root_ids is not None else None,
    })
    return counters


def _quick_pass(
    catalog: Catalog, candidates, counters: HashCounters, progress, cancel=None
) -> None:
    conn = catalog.conn
    pending: list[tuple[int, int]] = []

    conn.execute("BEGIN")
    try:
        for row in candidates:
            if cancel:
                cancel()        # per file (REVIEW F-13); committed work is kept
            path = _path_of(catalog, row)
            size = int(row["size_bytes"])
            try:
                digest = hasher.quick_hash(path, size)
            except DehydratedFileError:
                counters.dehydrated += 1
                continue
            except (UnreadableError, OSError) as exc:
                counters.unreadable += 1
                if len(counters.unreadable_paths) < 50:
                    counters.unreadable_paths.append(f"{path}: {exc}")
                continue

            counters.quick_hashed += 1
            counters.quick_bytes += min(size, hasher.SAMPLE_BYTES * 3)

            content_id = catalog.upsert_content(
                size_bytes=size,
                quick_hash=digest,
                full_hash=None,
                hash_algo=hasher.HASH_ALGO,
            )
            pending.append((content_id, int(row["id"])))

            if len(pending) >= CHECKPOINT_ROWS:
                catalog.assign_content(pending)
                pending.clear()
                conn.execute("COMMIT")
                conn.execute("BEGIN")
                if progress:
                    progress(counters)

        if pending:
            catalog.assign_content(pending)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _full_pass(
    catalog: Catalog, counters: HashCounters, progress, root_ids=None, cancel=None
) -> None:
    """Separate files that shared a quick hash but may differ in full.

    Groups whose quick hash already covered every byte are exact and are left
    alone — re-reading them would be pure cost for a result already known.
    """
    conn = catalog.conn

    for group in catalog.quick_hash_collisions():
        size = int(group["size_bytes"])
        if hasher.is_quick_hash_exact(size):
            continue

        members = catalog.files_for_content(int(group["id"]), root_ids)
        conn.execute("BEGIN")
        try:
            for row in members:
                if cancel:
                    cancel()
                path = _path_of(catalog, row)
                try:
                    digest, read = hasher.full_hash(path)
                except DehydratedFileError:
                    counters.dehydrated += 1
                    continue
                except (UnreadableError, OSError) as exc:
                    counters.unreadable += 1
                    if len(counters.unreadable_paths) < 50:
                        counters.unreadable_paths.append(f"{path}: {exc}")
                    # Leave it attached to the quick-hash group rather than
                    # asserting an identity that was never established.
                    continue

                counters.full_hashed += 1
                counters.full_bytes += read

                if read != size:
                    # The file changed under us. Record what was actually read;
                    # the next scan will pick up the new size.
                    counters.resized += 1

                content_id = catalog.upsert_content(
                    size_bytes=read,
                    quick_hash=_quick_of(catalog, int(group["id"])),
                    full_hash=digest,
                    hash_algo=hasher.HASH_ALGO,
                )
                catalog.assign_content([(content_id, int(row["id"]))])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if progress:
            progress(counters)


def _quick_of(catalog: Catalog, content_id: int) -> bytes:
    row = catalog.conn.execute(
        "SELECT quick_hash FROM content WHERE id = ?", (content_id,)
    ).fetchone()
    return row["quick_hash"]


def verify_pass(
    catalog: Catalog,
    *,
    volume_id: int | None = None,
    limit: int | None = None,
    root_ids=None,
    cancel: Callable[[], None] | None = None,
) -> dict:
    """Re-hash content against what was recorded and report what was found.

    This is the fixity check: content whose bytes changed while its
    modification time did not is silent corruption, and it is exactly what a
    catalog is for — nobody notices bit rot by looking at a folder.

    Both tiers are verifiable, and both are verified. A full hash covers every
    byte by definition; below ``SMALL_FILE_BYTES`` the *quick* hash also read
    the whole file, so it is an equally exact witness. Checking only full
    hashes would silently exclude most of a real catalog — small files are the
    majority — and a fixity report that quietly skips the majority of the data
    is worse than none, because it reads as a clean bill of health.

    ``root_ids`` confines the check to files under those roots, as in
    :func:`hash_pass`.

    Silent corruption means the bytes changed while the modification time did
    not, so that is compared first (docs/REVIEW.md F-06). A file whose mtime
    differs from the catalog was *edited* since the last scan: it is recorded
    as ``modified``, not hashed, and not reported as a fixity failure, because
    false alarms train people to ignore the real ones. A rescan picks up the
    new content. A size change alone is not an edit: software that writes a
    file updates its mtime, while truncation by filesystem damage leaves it,
    so such a file is hashed and reported as the mismatch it is.
    """
    sql = (
        "SELECT f.id, f.dir_id, f.name, f.size_bytes, f.mtime_ns, f.content_id, "
        "       c.full_hash, c.quick_hash, c.size_bytes AS content_size "
        "FROM file f JOIN content c ON c.id = f.content_id "
        "WHERE f.vanished_at IS NULL AND f.read_state = 'ok' "
        "  AND (c.full_hash IS NOT NULL OR c.size_bytes <= ?)"
    )
    params_head: list = [hasher.SMALL_FILE_BYTES]
    params: list = list(params_head)
    if volume_id is not None:
        sql += " AND f.volume_id = ?"
        params.append(volume_id)
    clause, clause_params = root_clause(root_ids)
    sql += clause
    params.extend(clause_params)
    sql += " ORDER BY f.volume_id, f.frn IS NULL, f.frn"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    checked = mismatched = unreadable = 0
    modified: list[str] = []
    incidents: list[dict] = []

    for row in list(catalog.conn.execute(sql, params)):
        if cancel:
            cancel()
        path = _path_of(catalog, row)
        expected = row["full_hash"]
        try:
            now = fsio.stat(path)
            if row["mtime_ns"] is not None and now.mtime_ns != int(row["mtime_ns"]):
                catalog.record_fixity(
                    int(row["content_id"]), int(row["id"]), expected, None, "modified"
                )
                modified.append(path)
                continue
            if expected is not None:
                digest, _read = hasher.full_hash(path)
            else:
                # Exact by construction: the quick hash read every byte. The
                # size folded in is the one recorded with the hash, not the
                # file's current size, so this reproduces the original
                # computation exactly — a file that has since changed length
                # then reports as a mismatch, which is what it is.
                expected = row["quick_hash"]
                digest = hasher.quick_hash(path, int(row["content_size"]))
        except (DehydratedFileError, UnreadableError, OSError) as exc:
            unreadable += 1
            catalog.record_fixity(
                int(row["content_id"]), int(row["id"]),
                expected, None, "unreadable",
            )
            incidents.append({"path": path, "result": "unreadable", "detail": str(exc)})
            continue

        checked += 1
        if digest == expected:
            catalog.record_fixity(
                int(row["content_id"]), int(row["id"]), expected, digest, "ok"
            )
            continue

        mismatched += 1
        catalog.record_fixity(
            int(row["content_id"]), int(row["id"]), expected, digest, "mismatch"
        )
        incidents.append({"path": path, "result": "mismatch", "detail": ""})

    catalog.record("verify.pass", {
        "checked": checked, "mismatched": mismatched, "unreadable": unreadable,
        "modified": len(modified),
        "root_ids": sorted(root_ids) if root_ids is not None else None,
    })
    return {
        "checked": checked,
        "mismatched": mismatched,
        "unreadable": unreadable,
        # Edited since the last scan: not checked, and not a failure.
        "modified": len(modified),
        "modified_paths": modified[:50],
        "incidents": incidents,
    }
