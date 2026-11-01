"""Frozen entry point: DriveFusion-TestBench.exe, the V&V test bench window.

The same window as ``python -m vv gui`` (docs/REAL-DRIVE-TESTING.md), run against
the Drive Fusion bundled beside it.
"""

from vv.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
