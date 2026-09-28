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

# Two front ends, one analysis. On Windows a GUI built with console=True flashes
# a console behind the window, and a CLI built with console=False has nowhere to
# print — so the same code ships as a windowed DriveFusion.exe and a console
# drivefusion.exe rather than compromising on one of them.
analysis = Analysis(
    [os.path.join(ROOT, "drivefusion", "__main__.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[
        "drivefusion.cli.main",
        # Reached only through `drivefusion gui`, so PyInstaller cannot see them.
        "drivefusion.gui.app",
        "drivefusion.gui.main_window",
        "drivefusion.gui.screens.browser",
        "drivefusion.gui.screens.dashboard",
        "drivefusion.gui.screens.drives",
        "drivefusion.gui.screens.scope",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Nothing may pull in a network, GUI-toolkit or packaging module we did not
    # ask for. PySide6 brings Qt's own web and multimedia stacks along unless they
    # are named here, which would roughly double the download for code that never
    # runs: this tool renders tables, not video.
    excludes=[
        "tkinter",
        "test",
        "unittest",
        "pydoc_data",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
    ],
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

# The window. asInvoker via the shared manifest: the app never silently
# elevates, and elevation is offered per scan through dfscan-helper.exe.
gui_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DriveFusion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=(
        os.path.join(SPECPATH, "drivefusion.manifest") if IS_WINDOWS else None
    ),
)

cli_exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="drivefusion",
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
    gui_exe,
    cli_exe,
    analysis.binaries,
    analysis.datas,
    helper_exe,
    helper_analysis.binaries,
    helper_analysis.datas,
    strip=False,
    upx=False,
    name="DriveFusion",
)
