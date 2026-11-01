#!/usr/bin/env python3
"""The version of each release, and stamping it into a build.

Every commit to ``main`` is released (``.github/workflows/ci.yml``: ``version``,
``main-build`` and ``release``). Its version is the next patch number after the
newest release tag that shares the source's major and minor numbers:

* ``drivefusion/__init__.py`` holds the *base* version, ``X.Y.Z``. A PR changes
  it only to start a new minor or major series (``0.2.0``) or to skip ahead.
* With no tag ``vX.Y.*`` yet, the release is ``vX.Y.Z``.
* Otherwise it is one patch past the highest such tag, and never below
  ``X.Y.Z``: tags ``v0.1.0`` and ``v0.1.1`` make the next release ``v0.1.2``.
* A commit that already carries a release tag (a re-run) keeps it.

Nothing is committed back to ``main``: the version lives in the tag, and the
build stamps it into its own copy of the source before packaging, so the
executables, ``--version``, the window title and every export say which
release they are. A source checkout reports the base version.

    python tools/release_version.py next            # print the next version, e.g. 0.1.3
    python tools/release_version.py stamp 0.1.3     # write it into the files that carry it
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "drivefusion" / "__init__.py"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# Each file that names the version, and how to find it there.
STAMPS: list[tuple[Path, re.Pattern[str], str]] = [
    (SOURCE, re.compile(r'^__version__ = "[^"]*"$', re.M), '__version__ = "{v}"'),
    (ROOT / "pyproject.toml", re.compile(r'^version = "[^"]*"$', re.M), 'version = "{v}"'),
    (ROOT / "packaging" / "drivefusion.manifest", re.compile(r'(name="DriveFusion" version=)"[^"]*"'),
     r'\g<1>"{v}.0"'),
]


def base_version(text: str | None = None) -> str:
    text = SOURCE.read_text(encoding="utf-8") if text is None else text
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not match or not _VERSION.match(match.group(1)):
        raise ValueError("drivefusion/__init__.py must set __version__ = \"X.Y.Z\"")
    return match.group(1)


def next_version(base: str, tags: list[str], head_tags: list[str] = ()) -> str:
    """The release version for a commit, from the base version and the existing tags."""
    for tag in head_tags:
        if _TAG.match(tag):
            return tag[1:]
    major, minor, patch = (int(n) for n in _VERSION.match(base).groups())
    patches = [int(m.group(3)) for m in map(_TAG.match, tags)
               if m and (int(m.group(1)), int(m.group(2))) == (major, minor)]
    if patches:
        patch = max(patch, max(patches) + 1)
    return f"{major}.{minor}.{patch}"


def _name(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def stamp(version: str) -> None:
    if not _VERSION.match(version):
        raise ValueError(f"not a version: {version!r}")
    for path, pattern, replacement in STAMPS:
        text = path.read_text(encoding="utf-8")
        new, count = pattern.subn(replacement.format(v=version), text)
        if count != 1:
            raise ValueError(f"{_name(path)}: expected one version to replace, found {count}")
        path.write_text(new, encoding="utf-8", newline="")
        print(f"{_name(path)}: {version}")


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.split()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next", help="print the version this commit is released as")
    s = sub.add_parser("stamp", help="write a version into the files that carry it")
    s.add_argument("version")
    args = parser.parse_args(argv)
    if args.command == "next":
        print(next_version(base_version(), _git("tag", "--list", "v*"),
                           _git("tag", "--points-at", "HEAD", "--list", "v*")))
    else:
        stamp(args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
