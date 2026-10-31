"""The versioned rubric files scorecards are scored against (PLAN §8.3, ROADMAP M6.6).

Each rubric is a file under ``drivefusion/standards/<id>/``, naming its source,
version, citation and URL, and how it was obtained: ``retrieval`` is
``confirmed`` once someone has checked it line by line against the source,
``reviewed`` once its owner has reviewed the wording and accepted it (with the
date in ``retrieved``), ``unconfirmed`` until then (ROADMAP D14). A scorecard
says a collection is *consistent with* a rubric's criteria, never more. Adopting a revised standard is a data change: add
its file, and reports name the version they used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

RUBRICS = ("fair", "ndsa")
RETRIEVALS = ("confirmed", "reviewed", "unconfirmed")


@dataclass(frozen=True)
class Criterion:
    id: str
    group: str
    text: str
    section: str
    level: int | None = None


@dataclass(frozen=True)
class Rubric:
    id: str
    source: str
    version: str
    citation: str
    url: str
    retrieval: str
    retrieved: str | None
    groups: dict[str, str]
    criteria: tuple[Criterion, ...]
    levels: dict[int, str]

    @property
    def accepted(self) -> bool:
        """Checked against the source, or its wording reviewed and accepted."""
        return self.retrieval in ("confirmed", "reviewed")

    def provenance(self) -> str:
        if self.retrieval == "confirmed":
            return f"confirmed against the source {self.retrieved}"
        if self.retrieval == "reviewed":
            return f"wording reviewed {self.retrieved}"
        return "not yet confirmed against the source"

    @property
    def name(self) -> str:
        return f"{self.id}-{self.version}"

    def criterion(self, criterion_id: str) -> Criterion:
        return next(c for c in self.criteria if c.id == criterion_id)


def parse(data: dict) -> Rubric:
    return Rubric(
        data["id"], data["source"], str(data["version"]), data["citation"], data["url"],
        data["retrieval"], data.get("retrieved"),
        {g["id"]: g["title"] for g in data["groups"]},
        tuple(Criterion(c["id"], c["group"], c["text"], c["section"], c.get("level"))
              for c in data["criteria"]),
        {lv["level"]: lv["title"] for lv in data.get("levels", [])},
    )


@lru_cache(maxsize=None)
def versions(rubric_id: str) -> tuple[Rubric, ...]:
    """Every shipped version of a rubric, newest first."""
    folder = resources.files(f"drivefusion.standards.{rubric_id}")
    found = [parse(json.loads(entry.read_text(encoding="utf-8")))
             for entry in folder.iterdir()
             if entry.name.startswith("rubric-") and entry.name.endswith(".json")]
    return tuple(sorted(found, key=lambda r: r.version, reverse=True))


def current(rubric_id: str) -> Rubric:
    return versions(rubric_id)[0]
