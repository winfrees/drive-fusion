"""Catalog schema, connection settings, and migrations.

The shape here is driven by row-count arithmetic rather than taste
(docs/PLAN.md §7). At 50 million files a ``dir_path`` column on every file row
would be roughly 4 GB of duplicated text before indexes, so directories are
interned into a tree and a file row carries an 8-byte ``dir_id`` plus its own
name. Budget: ~250 bytes per file all-in, which ``tools/benchmark.py`` measures
rather than assumes.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4

#: UPDATE...FROM, upsert with RETURNING, and strict typing all need this.
MIN_SQLITE = (3, 35, 0)

#: Applied on every connection. Sized for a multi-gigabyte catalog.
CONNECTION_PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("temp_store", "MEMORY"),
    ("cache_size", "-262144"),   # 256 MB, negative means KiB
    # A memory-mapped region counts toward RSS. At 1 GB the map alone put peak
    # RSS near 1 GB on a 3M-file catalog — file-backed and reclaimable, not a
    # leak, but too close to the 2 GB budget in §13 once the page cache is
    # added. 256 MB covers the hot working set with clear headroom.
    ("mmap_size", str(256 * 1024 * 1024)),
    ("busy_timeout", "10000"),
)

#: Only settable on an empty database, so it happens before any DDL.
INITIAL_PAGE_SIZE = 8192

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT    NOT NULL
);

-- A physical device. Identity is the serial, never a drive letter.
CREATE TABLE IF NOT EXISTS drive (
    id                  INTEGER PRIMARY KEY,
    serial              TEXT UNIQUE,
    model               TEXT,
    manufacturer        TEXT,
    bus                 TEXT,
    media               TEXT,
    capacity_bytes      INTEGER,
    nickname            TEXT,
    purchase_date       TEXT,
    purchase_price_cents INTEGER,
    vendor              TEXT,
    warranty_end        TEXT,
    batch_key           TEXT,
    role                TEXT,
    location            TEXT,
    disaster_zone       TEXT,
    encryption          TEXT,
    retired_at          TEXT,
    notes               TEXT,
    first_seen_at       TEXT,
    last_seen_at        TEXT
);

-- A filesystem. Identity is the volume GUID path, never a mount point.
CREATE TABLE IF NOT EXISTS volume (
    id                  INTEGER PRIMARY KEY,
    drive_id            INTEGER REFERENCES drive(id),
    volume_guid         TEXT UNIQUE NOT NULL,
    label               TEXT,
    fs_type             TEXT,
    capacity_bytes      INTEGER,
    free_bytes          INTEGER,
    cluster_bytes       INTEGER,
    last_letter         TEXT,
    last_seen_at        TEXT,
    dirty_flag          INTEGER,
    supports_hardlink   INTEGER,
    supports_usn        INTEGER,
    supports_file_ids   INTEGER,
    rescan_cost         TEXT,
    -- NTFS change-journal cursor. Both must match the volume's current
    -- journal for a delta rescan to be trustworthy; see core/enum/journal.py.
    usn_journal_id      INTEGER,
    usn_next            INTEGER
);

-- What the user has chosen to catalog. Nothing is scanned that is not here.
CREATE TABLE IF NOT EXISTS scope_root (
    id           INTEGER PRIMARY KEY,
    volume_id    INTEGER NOT NULL REFERENCES volume(id),
    path         TEXT NOT NULL,
    excludes     TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL,
    -- Removing a root stops future scanning; it does not discard what was
    -- already catalogued, and `dir.root_id` still references this row.
    removed_at   TEXT,
    UNIQUE(volume_id, path)
);

CREATE TABLE IF NOT EXISTS scan (
    id                  INTEGER PRIMARY KEY,
    volume_id           INTEGER NOT NULL REFERENCES volume(id),
    scope_root_id       INTEGER REFERENCES scope_root(id),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,      -- running|done|aborted|failed
    method              TEXT,               -- walk|dir-info|usn-full|usn-delta
    elevated            INTEGER NOT NULL DEFAULT 0,
    dirs_seen           INTEGER NOT NULL DEFAULT 0,
    files_seen          INTEGER NOT NULL DEFAULT 0,
    bytes_seen          INTEGER NOT NULL DEFAULT 0,
    unreadable          INTEGER NOT NULL DEFAULT 0,
    dehydrated_skipped  INTEGER NOT NULL DEFAULT 0,
    excluded            INTEGER NOT NULL DEFAULT 0,
    tool_version        TEXT,
    error               TEXT
);

-- Interned directory tree: ~5% of entries, so paths are stored once.
CREATE TABLE IF NOT EXISTS dir (
    id              INTEGER PRIMARY KEY,
    volume_id       INTEGER NOT NULL REFERENCES volume(id),
    root_id         INTEGER REFERENCES scope_root(id),
    parent_id       INTEGER REFERENCES dir(id),
    name            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    frn             INTEGER,
    last_seen_scan  INTEGER,
    vanished_at     TEXT,
    UNIQUE(volume_id, parent_id, name)
);
CREATE INDEX IF NOT EXISTS ix_dir_root ON dir(root_id);
CREATE INDEX IF NOT EXISTS ix_dir_parent ON dir(parent_id);
-- Resolves a change-journal parent reference to a catalogued directory.
CREATE INDEX IF NOT EXISTS ix_dir_vol_frn ON dir(volume_id, frn)
    WHERE frn IS NOT NULL;

-- Maintained at scan time so the tree view never aggregates on demand.
CREATE TABLE IF NOT EXISTS dir_rollup (
    dir_id          INTEGER PRIMARY KEY REFERENCES dir(id),
    files           INTEGER NOT NULL DEFAULT 0,
    bytes           INTEGER NOT NULL DEFAULT 0,
    subtree_files   INTEGER NOT NULL DEFAULT 0,
    subtree_bytes   INTEGER NOT NULL DEFAULT 0
);

-- Distinct content, addressed by hash. Populated at M3; the column exists now
-- so adding hashing is not a migration of a 50M-row table.
CREATE TABLE IF NOT EXISTS content (
    id          INTEGER PRIMARY KEY,
    size_bytes  INTEGER NOT NULL,
    quick_hash  BLOB,
    full_hash   BLOB,
    hash_algo   TEXT,
    media_kind  TEXT,
    format_id   TEXT,
    format_risk TEXT
);
-- Careful: SQLite treats every NULL as distinct in a UNIQUE index, so this
-- does NOT constrain rows where full_hash is still NULL. Catalog.upsert_content
-- matches that case explicitly; see the note there before changing either.
CREATE UNIQUE INDEX IF NOT EXISTS ux_content_hash
    ON content(size_bytes, quick_hash, full_hash);

CREATE TABLE IF NOT EXISTS file (
    id              INTEGER PRIMARY KEY,
    volume_id       INTEGER NOT NULL REFERENCES volume(id),
    dir_id          INTEGER NOT NULL REFERENCES dir(id),
    content_id      INTEGER REFERENCES content(id),
    name            TEXT NOT NULL,
    ext             TEXT,
    size_bytes      INTEGER NOT NULL,
    alloc_bytes     INTEGER,
    mtime_ns        INTEGER,
    ctime_ns        INTEGER,
    frn             INTEGER,
    nlink           INTEGER,
    attrs           INTEGER NOT NULL DEFAULT 0,
    read_state      TEXT NOT NULL DEFAULT 'ok',
    first_seen_scan INTEGER,
    last_seen_scan  INTEGER,
    vanished_at     TEXT
);
-- Natural key: makes the merge a single upsert and serves directory listings.
CREATE UNIQUE INDEX IF NOT EXISTS ux_file_dir_name ON file(dir_id, name);
CREATE INDEX IF NOT EXISTS ix_file_content ON file(content_id)
    WHERE content_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_file_size ON file(size_bytes);
CREATE INDEX IF NOT EXISTS ix_file_name ON file(name);
CREATE INDEX IF NOT EXISTS ix_file_vol_frn ON file(volume_id, frn);

-- Unindexed landing table: a scan streams here, then one set-based merge
-- folds it into `file`. Random inserts across four indexes over 50M rows is
-- the classic way to make SQLite look slow.
CREATE TABLE IF NOT EXISTS stage_file (
    scan_id     INTEGER NOT NULL,
    volume_id   INTEGER NOT NULL,
    dir_id      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    ext         TEXT,
    size_bytes  INTEGER NOT NULL,
    alloc_bytes INTEGER,
    mtime_ns    INTEGER,
    ctime_ns    INTEGER,
    frn         INTEGER,
    nlink       INTEGER,
    attrs       INTEGER NOT NULL DEFAULT 0,
    read_state  TEXT NOT NULL DEFAULT 'ok'
);

-- Fixity over time: the same content re-hashed later. Content whose hash
-- changed while its modification time did not is silent corruption, which is
-- exactly what a catalog is for — nobody notices bit rot by looking at a
-- folder (docs/PLAN.md §6.2).
CREATE TABLE IF NOT EXISTS fixity_check (
    id              INTEGER PRIMARY KEY,
    content_id      INTEGER REFERENCES content(id),
    file_id         INTEGER REFERENCES file(id),
    checked_at      TEXT NOT NULL,
    expected_hash   BLOB,
    observed_hash   BLOB,
    result          TEXT NOT NULL   -- ok|mismatch|unreadable|missing
);
CREATE INDEX IF NOT EXISTS ix_fixity_result ON fixity_check(result)
    WHERE result != 'ok';

-- Directories a scan actually looked at. A full scan covers a whole root, so
-- tombstoning is scoped by root_id; a delta covers only the directories the
-- journal named, and tombstoning anything outside them would declare files
-- gone that were simply not examined.
CREATE TABLE IF NOT EXISTS stage_dir (
    scan_id     INTEGER NOT NULL,
    dir_id      INTEGER NOT NULL,
    PRIMARY KEY (scan_id, dir_id)
) WITHOUT ROWID;

-- Append-only, hash-chained (docs/PLAN.md §7.3, FAIR R1.2 provenance).
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor       TEXT,
    action      TEXT NOT NULL,
    detail_json TEXT,
    prev_hash   BLOB,
    entry_hash  BLOB
);
"""


#: Statements that bring an older catalog forward, keyed by the version they
#: produce. Fresh databases get the full DDL above instead and skip these.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE volume ADD COLUMN usn_journal_id INTEGER",
        "ALTER TABLE volume ADD COLUMN usn_next INTEGER",
    ),
    # v3 and v4 add only new tables and indexes, which the DDL above creates
    # with IF NOT EXISTS on the upgrade pass; no ALTER is needed.
    3: (),
    4: (),
}


class SchemaError(RuntimeError):
    """Raised when the catalog cannot be opened or migrated safely."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise SchemaError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))} or newer is required "
            f"(found {sqlite3.sqlite_version}); the catalog merge relies on "
            "upsert with RETURNING"
        )


def connect(path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    """Open a catalog connection with the settings a large catalog needs."""
    _check_sqlite_version()
    path = Path(path)
    fresh = not path.exists() or path.stat().st_size == 0
    if not fresh and create is False:
        pass
    if fresh and not create:
        raise SchemaError(f"no catalog at {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row

    if fresh:
        # Page size is only settable while the database is empty.
        conn.execute(f"PRAGMA page_size={INITIAL_PAGE_SIZE}")
    for name, value in CONNECTION_PRAGMAS:
        conn.execute(f"PRAGMA {name}={value}")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    result = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(result["v"] or 0)


def backup_catalog(path: Path) -> Path | None:
    """Copy the catalog aside before a schema change. Never replaces a backup."""
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.{stamp}.bak")
    if target.exists():
        return target
    shutil.copy2(path, target)
    return target


def migrate(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """Bring the catalog to SCHEMA_VERSION, backing it up first if it exists."""
    version = current_version(conn)
    if version == SCHEMA_VERSION:
        return version
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"catalog is at schema version {version}, newer than this build "
            f"supports ({SCHEMA_VERSION}); upgrade Drive Fusion"
        )
    if version > 0 and path is not None:
        backup_catalog(path)

    # Upgrades run their ALTERs first; a fresh database skips them because the
    # DDL below already declares the current shape.
    if version > 0:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(target, ()):
                conn.execute(statement)

    # executescript() issues an implicit COMMIT before running, so the DDL
    # cannot be wrapped in an explicit transaction here. Every statement is
    # written IF NOT EXISTS, so a crash mid-script leaves a partially created
    # schema that the next run completes rather than a corrupt one.
    conn.executescript(DDL)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION
