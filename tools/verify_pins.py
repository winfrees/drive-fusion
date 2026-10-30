#!/usr/bin/env python3
"""CI check: every hash the build pinned is the hash of the file it shipped.

The GUI refuses to run a bundled program whose SHA-256 differs from its pin
(REVIEW F-07, ROADMAP M5.3), so a pin that doesn't match the shipped file
would break the feature it protects. Run after PyInstaller:

    python tools/verify_pins.py build/drivefusion/helper-pin/drivefusion_helper_pin.py dist/DriveFusion
"""

from __future__ import annotations

import hashlib
import runpy
import sys
from pathlib import Path

#: pin name -> where the one-folder build puts the pinned file
SHIPPED = {
    "HELPER_SHA256": "dfscan-helper.exe",
    "SMARTCTL_SHA256": "_internal/smartctl.exe",
}


def main(argv: list[str]) -> int:
    pin_module, dist = Path(argv[0]), Path(argv[1])
    pins = runpy.run_path(str(pin_module))
    failed = False
    for name, relative in SHIPPED.items():
        pinned = pins.get(name)
        path = dist / relative
        if not path.exists() and path.suffix == ".exe":
            path = path.with_suffix("")      # a build on Linux, for local checks
        if pinned is None:
            print(f"{name}: not pinned (not bundled)")
            failed |= path.exists()     # shipped but unpinned is a failure
            continue
        shipped = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        ok = shipped == pinned
        print(f"{name}: pinned {pinned}, shipped {shipped} ({'ok' if ok else 'MISMATCH'})")
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
