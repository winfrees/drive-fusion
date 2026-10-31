"""The export writer (PLAN §3.1–§3.2, ROADMAP M8.1).

Every user-facing file Drive Fusion writes goes through :class:`Bundle`:

* a bundle is a **new folder** under the export root you chose, named for the
  moment it was opened and its kind (``20261031T201500Z-manifests``); an
  existing folder is never reused;
* :meth:`Bundle.write` takes a path *relative to the bundle* and refuses one
  that is absolute, climbs out (``..``), names a drive, or passes through a
  link; it creates the file exclusively, so **nothing is ever overwritten**;
* :meth:`Bundle.close` writes ``_EXPORT.json``: the kind, the tool version,
  the standards versions used, the disclaimer, and every file with its size
  and SHA-256, and records the export in the catalog.

The export root itself must exist, and may not be inside a scope root or on
a scanned drive other than the system drive (``Catalog.check_export_root``,
REVIEW F-03). There is no delete here, by design: an export that fails part
way leaves its folder as it was, without ``_EXPORT.json``, which marks it
unfinished.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from drivefusion import __version__
from drivefusion.core import disclaimer
from drivefusion.core.errors import DriveFusionError

MANIFEST = "_EXPORT.json"
_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(
    os, "O_NOFOLLOW", 0)


class ExportError(DriveFusionError):
    """An export the rules refuse; the message says why."""


@dataclass
class Written:
    path: str            # relative to the bundle, '/'-separated
    bytes: int
    sha256: str


def _parts(relative: str) -> list[str]:
    if not isinstance(relative, str) or not relative.strip():
        raise ExportError("an export file needs a name")
    if relative.startswith(("/", "\\")) or (len(relative) > 1 and relative[1] == ":"):
        raise ExportError(f"{relative!r}: export paths are relative to the bundle")
    parts = relative.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", "..") or ":" in part:
            raise ExportError(f"{relative!r}: {part!r} may not appear in an export path")
    return list(parts)


@dataclass
class Bundle:
    catalog: object
    root: Path
    path: Path
    kind: str
    files: list[Written] = field(default_factory=list)
    checks: list[Callable[[str, bytes], None]] = field(default_factory=list)
    closed: bool = False
    _closing: bool = False

    def write(self, relative: str, data: bytes | str) -> Path:
        """Create ``relative`` inside the bundle with ``data``. Never overwrites."""
        if self.closed:
            raise ExportError("this export is closed")
        parts = _parts(relative)
        if parts == [MANIFEST] and not self._closing:
            raise ExportError(f"{MANIFEST} is written when the export closes")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        for check in self.checks:
            check(relative, payload)
        folder = self.path
        for part in parts[:-1]:
            folder = folder / part
            if folder.is_symlink():
                raise ExportError(f"{relative!r}: {folder} is a link")
            if not folder.exists():
                os.mkdir(folder)
            elif not folder.is_dir():
                raise ExportError(f"{relative!r}: {folder} is not a folder")
        target = folder / parts[-1]
        if not target.resolve().is_relative_to(self.path.resolve()):
            raise ExportError(f"{relative!r} would land outside the export {self.path}")
        try:
            fd = os.open(target, _FLAGS, 0o644)
        except FileExistsError:
            raise ExportError(f"{relative!r} already exists in this export; nothing "
                              "is ever overwritten") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        self.files.append(Written("/".join(parts), len(payload),
                                  hashlib.sha256(payload).hexdigest()))
        return target

    def write_json(self, relative: str, value) -> Path:
        return self.write(relative, json.dumps(value, indent=1, ensure_ascii=False,
                                               sort_keys=True) + "\n")

    def close(self, *, standards: dict | None = None, summary: dict | None = None) -> int:
        """Write ``_EXPORT.json`` and record the export. Returns its catalog id."""
        from drivefusion.core.store import exports

        if self.closed:
            raise ExportError("this export is already closed")
        files = list(self.files)
        record = {
            "kind": self.kind,
            "created_by": f"Drive Fusion {__version__}",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "standards": standards or {},
            "summary": summary or {},
            "disclaimer": disclaimer.TEXT,
            "read_only": "Drive Fusion wrote only this folder; nothing on any scanned "
                         "drive was changed.",
            "files": [{"path": f.path, "bytes": f.bytes, "sha256": f.sha256} for f in files],
        }
        self._closing = True
        try:
            self.write_json(MANIFEST, record)
        finally:
            self._closing = False
        self.closed = True
        return exports.record(
            self.catalog, kind=self.kind, root=str(self.root), path=str(self.path),
            files=len(files), size=sum(f.bytes for f in files),
            standards=standards or {}, summary=summary or {})

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *exc) -> None:
        # A bundle left open by an error stays as it is, unfinished: never deleted.
        return None


def stamp(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def open_bundle(catalog, root: str | Path, kind: str, *,
                now: dt.datetime | None = None) -> Bundle:
    """A new, empty bundle folder under ``root`` for an export of ``kind``."""
    if not kind or not all(c.isalnum() or c in "-_" for c in kind):
        raise ExportError(f"{kind!r} is not an export kind")
    root = Path(root)
    if not root.is_dir():
        raise ExportError(f"the export folder {root} does not exist; choose an existing "
                          "folder (Drive Fusion creates nothing outside it)")
    catalog.check_export_root(root)
    base = f"{stamp(now)}-{kind}"
    for n in range(1, 1000):
        path = root / (base if n == 1 else f"{base}-{n}")
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return Bundle(catalog, root.resolve(), path.resolve(), kind)
    raise ExportError(f"could not find a new folder name for {base} under {root}")
