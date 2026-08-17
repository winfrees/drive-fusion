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

# The elevated enumerator ships as its own binary, not a mode of the main
# application: only it ever needs administrator rights, and keeping it separate
# keeps that privileged surface to a few hundred auditable lines
# (docs/PLAN.md §6.5).
helper_analysis = Analysis(
    [os.path.join(ROOT, "drivefusion", "core", "enum", "helper", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["drivefusion.core.enum.journal", "drivefusion.core.enum.winio"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)
helper_pyz = PYZ(helper_analysis.pure)

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

helper_exe = EXE(
    helper_pyz,
    helper_analysis.scripts,
    [],
    exclude_binaries=True,
    name="dfscan-helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # asInvoker as well: the helper is launched by an already-elevated parent
    # rather than prompting on its own. See core/enum/helper/client.py.
    manifest=(
        os.path.join(SPECPATH, "drivefusion.manifest") if IS_WINDOWS else None
    ),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    helper_exe,
    helper_analysis.binaries,
    helper_analysis.datas,
    strip=False,
    upx=False,
    name="DriveFusion",
)
