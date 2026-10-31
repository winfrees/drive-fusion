"""Authenticode-sign Windows executables when a certificate is configured (ROADMAP M8.8, D21).

Signing needs a code-signing certificate, which only the project's owner can
obtain. With one configured (``DRIVEFUSION_SIGN_PFX`` naming a .pfx file, and
``DRIVEFUSION_SIGN_PASSWORD``), each file is signed with SHA-256 and an RFC 3161
timestamp, then verified. Without one, nothing is signed, and that is said
plainly: a ``::warning::`` in CI and ``(unsigned build)`` in ``--version``.

The helper is signed from packaging/drivefusion.spec *before* its SHA-256 is
pinned (REVIEW F-07), so the pin is the signed file's; the two main
executables are signed after the build, which pins nothing of theirs.

    python tools/sign.py dist/DriveFusion/DriveFusion.exe dist/DriveFusion/drivefusion.exe
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

TIMESTAMP = "http://timestamp.digicert.com"


def configured(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return bool(environ.get("DRIVEFUSION_SIGN_PFX"))


def signtool() -> str:
    found = shutil.which("signtool")
    if found:
        return found
    kits = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    if kits:
        return kits[-1]
    raise SystemExit("a certificate is configured but signtool.exe was not found")


def command(paths, environ=None, tool: str = "signtool") -> list[str]:
    environ = os.environ if environ is None else environ
    return [tool, "sign", "/fd", "SHA256", "/f", environ["DRIVEFUSION_SIGN_PFX"],
            "/p", environ.get("DRIVEFUSION_SIGN_PASSWORD", ""),
            "/tr", environ.get("DRIVEFUSION_SIGN_TIMESTAMP", TIMESTAMP), "/td", "SHA256",
            *map(str, paths)]


def sign(paths, environ=None, run=subprocess.run) -> bool:
    """Sign and verify ``paths``; False, and a warning, when unconfigured."""
    if not configured(environ):
        print("::warning::No code-signing certificate is configured, so this build is "
              "unsigned (ROADMAP D21).")
        return False
    tool = signtool()
    run(command(paths, environ, tool), check=True)
    run([tool, "verify", "/pa", *map(str, paths)], check=True)
    print(f"signed and verified: {', '.join(map(str, paths))}")
    return True


if __name__ == "__main__":
    sign(sys.argv[1:])
