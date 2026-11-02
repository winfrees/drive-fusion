"""Frozen entry point: drivefusion-vv.exe, the V&V test bench on the command line.

The same commands as ``python -m vv`` (docs/VV.md), run against the Drive Fusion
bundled beside it.
"""

from vv.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
