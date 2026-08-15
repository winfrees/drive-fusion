# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build: one-folder, 64-bit Windows.

One-folder rather than one-file, deliberately (docs/PLAN.md §12): one-file mode
unpacks the whole application to a temp directory on every launch, which is
slow and is exactly the behaviour antivirus heuristics treat as suspicious —
a bad combination for a tool that also opens raw volume handles.

Build with::

    pyinstaller packaging/drivefusion.spec --noconfirm
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_WINDOWS = sys.platform == "win32"

analysis = Analysis(
    [os.path.join(ROOT, "drivefusion", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["drivefusion.cli.main"],
    hookspath=[],
    runtime_hooks=[],
    # The core must not drag in the GUI stack for a CLI-only run, and nothing
    # may pull in a network or packaging module we did not ask for.
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DriveFusion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX compression is another antivirus false-positive magnet.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=(
        os.path.join(SPECPATH, "drivefusion.manifest") if IS_WINDOWS else None
    ),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="DriveFusion",
)
