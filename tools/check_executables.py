#!/usr/bin/env python3
"""Check a Windows build holds each executable, by its exact name and kind.

The build ships five programs in one folder. Two of them once differed only in
case (``DriveFusion.exe`` and ``drivefusion.exe``), which Windows treats as one
name: the second build overwrote the first, so the windowed program was lost
and every check that looked for it by name still passed. This reads the folder
listing, so the names must match exactly, and each file's PE header, so the
windowed program really is windowed and the console ones really are console.

    python tools/check_executables.py dist/DriveFusion
"""

from __future__ import annotations

import os
import struct
import sys

GUI, CONSOLE = 2, 3                    # IMAGE_SUBSYSTEM_WINDOWS_GUI / _CUI
EXPECTED = {
    "DriveFusion.exe": GUI,            # the window
    "drivefusion-cli.exe": CONSOLE,    # the command line
    "dfscan-helper.exe": CONSOLE,      # the elevated helper
    "DriveFusion-TestBench.exe": GUI,  # the V&V test bench window
    "drivefusion-vv.exe": CONSOLE,     # the V&V test bench command line
}
_NAMES = {GUI: "windowed", CONSOLE: "console"}


def subsystem(data: bytes) -> int:
    """The Subsystem field of a PE image's optional header."""
    if data[:2] != b"MZ":
        raise ValueError("not an executable (no MZ header)")
    (pe,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE image")
    (value,) = struct.unpack_from("<H", data, pe + 24 + 68)   # same offset in PE32 and PE32+
    return value


def check(folder: str) -> list[str]:
    problems = []
    names = os.listdir(folder)
    lowered = [n.lower() for n in EXPECTED]
    if len(set(lowered)) != len(lowered):
        problems.append("two expected names differ only in case, which Windows treats as one")
    for name, wanted in EXPECTED.items():
        if name not in names:
            near = [n for n in names if n.lower() == name.lower()]
            problems.append(f"{name} is missing" + (f" (found {near[0]})" if near else ""))
            continue
        with open(os.path.join(folder, name), "rb") as fh:
            got = subsystem(fh.read(4096))
        if got != wanted:
            problems.append(f"{name} is {_NAMES.get(got, got)}, not {_NAMES[wanted]}")
        else:
            print(f"{name}: {_NAMES[got]}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    problems = check(argv[0])
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
