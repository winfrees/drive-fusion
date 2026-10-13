"""Loading the versioned reference files under ``standards/``.

§8.3: every rubric is a versioned, citable file, and reports name the version
they scored against. Adopting a revised standard is then a data change rather
than a release.

This module is the shared loader. ``health/afr.py`` predates it and keeps its own
typed accessor, but resolves its directory through here so there is one answer to
"where do the standards live" — a question with two answers in a frozen build,
where PyInstaller unpacks data files under ``sys._MEIPASS`` rather than beside
the modules.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class StandardsError(RuntimeError):
    """A rubric is missing or malformed. Never silently substituted.

    Substituting a default would make a broken rubric invisible, and every
    number scored against it would look as trustworthy as any other.
    """


def standards_dir() -> Path:
    """The root of the shipped reference files, in a checkout or a bundle.

    Anchored on the ``drivefusion`` package rather than a counted number of
    ``parents`` from this file: ``standards/`` is a sibling of the package in a
    checkout, and that relationship holds wherever within the package this
    module lives. A hand-counted depth is wrong the moment a file moves, which
    is how the first version of this broke.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "standards"

    import drivefusion

    return Path(drivefusion.__file__).resolve().parent.parent / "standards"


@dataclass(frozen=True)
class Standard:
    """One loaded reference file, with the provenance a report must cite."""

    id: str
    version: str
    retrieved_at: str
    kind: str
    document: dict
    path: Path

    @property
    def citation(self) -> str:
        return f"{self.id} v{self.version} ({self.kind}, retrieved {self.retrieved_at})"

    def __getitem__(self, key: str):
        return self.document[key]

    def get(self, key: str, default=None):
        return self.document.get(key, default)


@lru_cache(maxsize=16)
def load(relative: str) -> Standard:
    """Load ``standards/<relative>``. Cached; these are read per report."""
    path = standards_dir() / relative
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StandardsError(
            f"no standards file at {path}. A build that omits standards/ will "
            "fail here rather than scoring against a guess."
        ) from exc
    except json.JSONDecodeError as exc:
        raise StandardsError(f"{path} is not valid JSON: {exc}") from exc

    try:
        return Standard(
            id=document["id"],
            version=document["version"],
            retrieved_at=document["retrieved_at"],
            kind=document.get("kind", "unknown"),
            document=document,
            path=path,
        )
    except (KeyError, TypeError) as exc:
        raise StandardsError(
            f"{path} is missing the provenance fields every rubric must carry "
            f"(id, version, retrieved_at): {exc}"
        ) from exc


FAIR_RUBRIC = "fair/fair-rubric-v2.json"
NDSA_LEVELS = "ndsa/ndsa-levels-v2-v2.json"
VOCABULARIES = "vocab/vocabularies-v1.json"


def fair_rubric() -> Standard:
    return load(FAIR_RUBRIC)


def ndsa_levels() -> Standard:
    return load(NDSA_LEVELS)


def vocabularies() -> Standard:
    return load(VOCABULARIES)
