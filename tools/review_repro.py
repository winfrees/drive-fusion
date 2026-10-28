#!/usr/bin/env python3
"""Reproductions for the findings in docs/REVIEW.md.

Each check prints FOUND while the defect is present and NOT FOUND once it is
fixed. Everything runs in a temporary directory; nothing outside it is touched.

    python tools/review_repro.py
"""
import ctypes
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivefusion.core.analysis import redundancy_summary, under_protected
from drivefusion.core.identity import hash_pass, verify_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog
from drivefusion.core import fsio
from drivefusion.core.enum.batchwalk import fsio_lister
from drivefusion.core.enum.records import DirEntry
from tools.ro_lint import check_source

work = Path(tempfile.mkdtemp())


def fresh(name):
    root = work / name
    root.mkdir()
    cat = Catalog(work / f"{name}.db")
    vol = cat.upsert_volume(volume_guid=f"vol:{name}")
    rid = cat.add_scope_root(vol, str(root))
    return root, cat, vol, rid


def scan(cat, root, vol, rid, **kw):
    return scan_root(cat, root_path=str(root), root_id=rid, volume_id=vol, **kw)


# R1: root missing at scan time (drive unplugged / letter reassigned)
root, cat, vol, rid = fresh("r1")
for i in range(5):
    (root / f"f{i}.txt").write_text("x" * i)
scan(cat, root, vol, rid)
shutil.move(str(root), str(work / "r1-away"))   # simulate unplugged drive
res = scan(cat, root, vol, rid)
print("R1 missing root -> vanished:", res["merged"]["vanished"],
      "| coverage:", res["coverage"],
      "|", "FOUND" if res["merged"]["vanished"] == 5 else "NOT FOUND")

# R2: one unreadable subdirectory during a full scan
root, cat, vol, rid = fresh("r2")
(root / "locked").mkdir()
for i in range(3):
    (root / "locked" / f"g{i}.txt").write_text("y" * (i + 1))
scan(cat, root, vol, rid)
def flaky(path):
    if path.endswith("locked"):
        raise PermissionError("access denied")
    return fsio_lister(path)
res = scan(cat, root, vol, rid, lister=flaky)
print("R2 unreadable dir -> vanished:", res["merged"]["vanished"],
      "|", "FOUND" if res["merged"]["vanished"] == 3 else "NOT FOUND")

# R3: catalog inside the scanned root -> the tool writes into the tree
root = work / "r3"
root.mkdir()
(root / "data.bin").write_bytes(b"d" * 10)
before = sorted(p.name for p in root.iterdir())
cat = Catalog(root / "catalog.db")
vol = cat.upsert_volume(volume_guid="vol:r3")
rid = cat.add_scope_root(vol, str(root))
scan(cat, root, vol, rid)
after = sorted(p.name for p in root.iterdir())
print("R3 catalog inside root -> tree before", before, "after", after,
      "|", "FOUND" if before != after else "NOT FOUND")
cat.close()

# R4: unique-size single-copy files are absent from under-protected
root, cat, vol, rid = fresh("r4")
(root / "a1.txt").write_bytes(b"same-size-A")   # 11 bytes
(root / "a2.txt").write_bytes(b"same-size-B")   # 11 bytes, different content
(root / "solo.bin").write_bytes(b"z" * 999)     # unique size: one copy only
scan(cat, root, vol, rid)
hash_pass(cat)
summary = redundancy_summary(cat, min_copies=2)
listed = {g.size_bytes for g in under_protected(cat, min_copies=2)}
print("R4 under-protected items:", summary["under_protected_items"],
      "unhashed:", summary["unhashed_files"], "| 999-byte solo listed:", 999 in listed,
      "|", "FOUND" if 999 not in listed else "NOT FOUND")

# R5: verify reports a legitimate edit (mtime changed) as a fixity mismatch
root, cat, vol, rid = fresh("r5")
(root / "p.txt").write_bytes(b"original")
(root / "q.txt").write_bytes(b"other!!!")
scan(cat, root, vol, rid)
hash_pass(cat)
(root / "p.txt").write_bytes(b"EDITED!!")           # same size, new mtime
os.utime(root / "p.txt", None)
v = verify_pass(cat)
print("R5 verify after a normal edit -> mismatched:", v["mismatched"],
      "|", "FOUND" if v["mismatched"] else "NOT FOUND")

# R6: two serial-less volumes on one physical disk count as two drives
cat = Catalog(work / "r6.db")
c = cat.upsert_content(size_bytes=5, quick_hash=b"q", full_hash=None, hash_algo="t")
for n in (1, 2):
    v_id = cat.upsert_volume(volume_guid=f"part{n}")   # no drive_id: serial unread
    d = cat.intern_dir(volume_id=v_id, root_id=None, parent_id=None,
                       name=f"P{n}:", depth=0, scan_id=0)
    cat.conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes, content_id)"
                     " VALUES (?, ?, 'x', 5, ?)", (v_id, d, c))
g = [x for x in under_protected(cat, min_copies=2)]
print("R6 two partitions, no serial -> under-protected groups:", len(g),
      "|", "FOUND" if not g else "NOT FOUND")

# R7: cancellation granularity — progress (where Cancel is checked) never
# fires for a root with fewer than BATCH_ROWS files
root, cat, vol, rid = fresh("r7")
for d in range(200):
    (root / f"d{d}").mkdir()
    (root / f"d{d}" / "f").write_bytes(b"1")
calls = []
scan(cat, root, vol, rid, progress=lambda c: calls.append(c.dirs))
print("R7 progress callbacks during a 201-dir scan:", len(calls),
      "|", "FOUND" if not calls else "NOT FOUND")

# R8: lint blind spots (each should be a finding, and is not)
probes = {
    "subprocess": "import subprocess\nsubprocess.run(['cmd','/c','del','x'])\n",
    "os.open numeric flags": "import os\nos.open('x', 0x241)\n",
    "io.open write": "import io\nio.open('x', 'wb')\n",
    "ctypes DeleteFileW": "import ctypes\nctypes.windll.kernel32.DeleteFileW('x')\n",
    "sqlite3.connect": "import sqlite3\nsqlite3.connect('E:/data/x.db')\n",
    "os.write on fd": "import os\nos.write(3, b'x')\n",
    "open via getattr": "import os\ngetattr(os, 're' + 'move')('x')\n",
}
for label, src in probes.items():
    found = check_source("drivefusion/core/scan/probe.py", src)
    print(f"R8 lint {label:22} -> findings: {len(found)}",
          "| FOUND (gap)" if not found else "| caught")

# R9: ctypes default restype truncates INVALID_HANDLE_VALUE comparison
returned = ctypes.c_int(0xFFFFFFFF).value       # what a c_int restype yields
invalid = ctypes.c_void_p(-1).value             # what winio compares against
print("R9 c_int(-1)", returned, "== c_void_p(-1).value", invalid, "->",
      returned == invalid, "|", "FOUND" if returned != invalid else "NOT FOUND")

# R10 (F-10): a file copy of a WAL-mode database misses committed rows
wal = work / "wal.db"
live = sqlite3.connect(wal, isolation_level=None)
live.execute("PRAGMA journal_mode=WAL")
live.execute("CREATE TABLE t(x)")
live.execute("INSERT INTO t VALUES (1), (2), (3)")
shutil.copy2(wal, work / "wal.db.bak")            # what backup_catalog does
try:
    rows = sqlite3.connect(work / "wal.db.bak").execute("SELECT COUNT(*) FROM t").fetchone()[0]
except sqlite3.Error:
    rows = None
print("R10 WAL copy backup -> rows in backup:", rows, "(live: 3)",
      "|", "FOUND" if rows != 3 else "NOT FOUND")
live.close()

# R11 (F-11): a placeholder that also carries the reparse attribute
root, cat, vol, rid = fresh("r11")
(root / "cloud.docx").write_bytes(b"c" * 10)
def placeholder_lister(path):
    return [
        DirEntry(name=e.name,
                 attributes=fsio.FILE_ATTRIBUTE_REPARSE_POINT
                 | fsio.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                 size=e.size, alloc_size=0, mtime_ns=e.mtime_ns,
                 ctime_ns=e.ctime_ns, file_id=None)
        for e in fsio_lister(path)
    ]
scan(cat, root, vol, rid, lister=placeholder_lister)
files = cat.counts()["files"]
print("R11 reparse+recall placeholder -> files catalogued:", files,
      "|", "FOUND" if files == 0 else "NOT FOUND")

# R12 (F-12): a scan does not refresh the volume's "last scanned" time
root, cat, vol, rid = fresh("r12")
(root / "a.txt").write_text("a")
cat.conn.execute("UPDATE volume SET last_seen_at = '2000-01-01T00:00:00+00:00'")
scan(cat, root, vol, rid)
seen = cat.conn.execute("SELECT last_seen_at FROM volume").fetchone()[0]
print("R12 last_seen_at after scan:", seen,
      "|", "FOUND" if seen.startswith("2000") else "NOT FOUND")

shutil.rmtree(work, ignore_errors=True)
