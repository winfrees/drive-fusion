#!/usr/bin/env python3
"""Fetch the pinned smartctl for the Windows build (ROADMAP M5.3).

Downloads the smartmontools installer named in packaging/smartmontools.json,
refuses it unless its SHA-256 is the pinned one, extracts smartctl.exe and the
GPL text with 7-Zip, and refuses smartctl.exe unless its SHA-256 is pinned too.
The output goes to packaging/vendor/, which packaging/drivefusion.spec bundles.

    python tools/fetch_smartctl.py            # download and extract
    python tools/fetch_smartctl.py --check    # verify what is already there

Build tooling, not part of the product: it writes files, so it lives in tools/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "smartmontools.json"
VENDOR = ROOT / "packaging" / "vendor"


class FetchError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(path: Path, expected: str, what: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise FetchError(f"{what} {path.name}: SHA-256 {actual}, pinned {expected}; refusing it")


def find_7z() -> str:
    for name in ("7z", "7za", r"C:\Program Files\7-Zip\7z.exe"):
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    raise FetchError("7-Zip is needed to unpack the smartmontools installer")


def fetch(lock: dict, vendor: Path) -> None:
    vendor.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        installer = Path(work) / "smartmontools-setup.exe"
        with urllib.request.urlopen(lock["installer_url"], timeout=120) as response:
            installer.write_bytes(response.read())
        require(installer, lock["installer_sha256"], "installer")
        out = Path(work) / "x"
        subprocess.run(
            [find_7z(), "x", "-y", f"-o{out}", str(installer),
             lock["smartctl_member"], lock["license_member"]],
            check=True, stdout=subprocess.DEVNULL,
        )
        shutil.copyfile(out / lock["smartctl_member"], vendor / "smartctl.exe")
        shutil.copyfile(out / lock["license_member"], vendor / "smartmontools-COPYING.txt")
    check(lock, vendor)


def check(lock: dict, vendor: Path) -> None:
    require(vendor / "smartctl.exe", lock["smartctl_sha256"], "smartctl")
    if not (vendor / "smartmontools-COPYING.txt").is_file():
        raise FetchError("the smartmontools licence text is missing; it ships with the binary")
    print(f"smartctl {lock['version']}: {lock['smartctl_sha256']} (verified)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only; download nothing")
    args = parser.parse_args(argv)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    try:
        check(lock, VENDOR) if args.check else fetch(lock, VENDOR)
    except (FetchError, OSError, subprocess.CalledProcessError) as exc:
        print(f"fetch_smartctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
