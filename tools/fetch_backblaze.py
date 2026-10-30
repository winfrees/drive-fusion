#!/usr/bin/env python3
"""Build step: the pinned Backblaze Drive Stats quarter as an AFR table (ROADMAP M5.4).

Reads ``packaging/backblaze.json``, downloads the ZIP into
``packaging/vendor/backblaze/`` (reusing a copy already there), refuses it
unless its SHA-256 is the pinned one, and writes
``packaging/vendor/afr/rates-backblaze-<version>.json`` for the spec to bundle.

With no hash pinned yet, it prints the download's SHA-256 and ships no fleet
table: pinning is a reviewed change to the lock file, never automatic.

    python tools/fetch_backblaze.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drivefusion.core.health import backblaze  # noqa: E402

LOCK = ROOT / "packaging" / "backblaze.json"
DOWNLOADS = ROOT / "packaging" / "vendor" / "backblaze"
OUT = ROOT / "packaging" / "vendor" / "afr"


def sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def main() -> int:
    lock = json.loads(LOCK.read_text())
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / lock["url"].rsplit("/", 1)[-1]
    if not target.exists():
        print(f"downloading {lock['url']}")
        partial = target.with_suffix(".part")
        try:
            with urllib.request.urlopen(lock["url"], timeout=600) as response, \
                    open(partial, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
        except OSError as exc:
            if lock["sha256"] is None:      # nothing promised yet: ship without it
                print(f"Backblaze {lock['version']}: download failed ({exc}); "
                      "no fleet table shipped")
                return 0
            raise
        partial.replace(target)
    actual = sha256(target)
    if lock["sha256"] is None:
        print(f"Backblaze {lock['version']}: {actual} (NOT PINNED: no fleet table shipped). "
              f"To ship it, set \"sha256\" in {LOCK.relative_to(ROOT)} to that value.")
        return 0
    if actual != lock["sha256"]:
        print(f"Backblaze {lock['version']}: {actual} does not match the pinned "
              f"{lock['sha256']}; refusing it", file=sys.stderr)
        target.unlink()
        return 1
    print(f"Backblaze {lock['version']}: {actual} (verified)")
    out = OUT / f"rates-backblaze-{lock['version']}.json"
    if out.exists() and json.loads(out.read_text()).get("inputs") == {target.name: actual}:
        print(f"{out.relative_to(ROOT)}: already built from this download")
        return 0
    table = backblaze.import_files([target], version=lock["version"],
                                   retrieved=lock["retrieved"],
                                   min_drive_days=lock["min_drive_days"])
    OUT.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    print(f"{out.relative_to(ROOT)}: {len(table['models'])} models, "
          f"fleet AFR {table['classes']['hdd']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
