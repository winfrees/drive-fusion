"""What a person does with the review queue (PLAN §8.4, ROADMAP M6.3).

Every function here is an explicit act, taken by a person through the CLI or
the GUI, and each is audited by ``core/store/curation.py``:

* **accept** a proposal: it becomes a collection whose membership is a
  subtree rule on the proposed folder, so files that appear there later join
  it. Accepting *into* an existing collection adds the folder to it instead;
  that is how two proposals are merged into one collection.
* **reject** it: it stays rejected however often inference runs again.
* **split** it: it is set aside, and each subfolder holding files is proposed
  in its place, with its own evidence.
* **accept all** proposals scoring at least a threshold: still an act the
  person takes, one decision per proposal, each audited.

Renaming and the rest of a collection's metadata go through
``curation.edit_collection``.
"""

from __future__ import annotations

import json
import os

from drivefusion.core.curate import infer
from drivefusion.core.store import curation
from drivefusion.core.store.curation import CurationError


def _transaction(catalog):
    class _Tx:
        def __enter__(self):
            catalog.conn.execute("BEGIN")

        def __exit__(self, kind, *_):
            catalog.conn.execute("ROLLBACK" if kind else "COMMIT")
    return _Tx()


def _pending(catalog, candidate_id: int):
    row = curation.candidate(catalog, candidate_id)
    if row is None:
        raise CurationError(f"no proposal {candidate_id}")
    if row["status"] != "proposed":
        raise CurationError(f"proposal {candidate_id} was already {row['status']}")
    return row


def default_title(row) -> str:
    """The proposed folder's name: a starting point the person can rename."""
    path = json.loads(row["evidence_json"]).get("path") or ""
    return os.path.basename(path.rstrip("/\\")) or f"Collection from proposal {row['id']}"


def accept(catalog, candidate_id: int, *, title: str | None = None,
           into: int | None = None, **metadata) -> int:
    """Accept a proposal as a new collection, or into an existing one.
    Returns the collection id."""
    row = _pending(catalog, candidate_id)
    with _transaction(catalog):
        if into is not None:
            if title or metadata:
                raise CurationError("accepting into a collection keeps its title and "
                                    "metadata; edit them separately")
            curation.add_subtree_rule(catalog, into, row["dir_id"])
            collection_id = into
        else:
            collection_id = curation.create_collection(
                catalog, title or default_title(row), dir_ids=[row["dir_id"]], **metadata)
        curation.decide(catalog, candidate_id, "accepted", collection_id=collection_id)
    return collection_id


def reject(catalog, candidate_id: int) -> None:
    _pending(catalog, candidate_id)
    curation.decide(catalog, candidate_id, "rejected")


def split(catalog, candidate_id: int) -> list[int]:
    """Set a proposal aside and propose each of its subfolders that holds
    files instead. Returns the new proposals' ids."""
    row = _pending(catalog, candidate_id)
    nodes = infer.load(catalog)
    children = [nodes[c] for c in nodes[row["dir_id"]].children if nodes[c].files]
    if not children:
        raise CurationError(f"proposal {candidate_id} has no subfolders holding files to "
                            "split into")
    shapes = infer.Counter(infer._shape(c.name) for c in children)
    made = []
    with _transaction(catalog):
        curation.decide(catalog, candidate_id, "rejected")
        catalog.record("collection.candidate.split", {
            "candidate_id": candidate_id, "into_dirs": [c.id for c in children]})
        for child in children:
            child.signals = infer.signals(child, shapes)
            child.score = infer.score(child.signals)
            new_id = curation.propose(catalog, child.id, child.score,
                                      infer.evidence(catalog, child))
            if new_id is not None:
                made.append(new_id)
    return made


def accept_all(catalog, *, min_score: float) -> list[int]:
    """Accept every pending proposal scoring at least ``min_score``, each as
    its own collection titled after its folder. Returns the collection ids."""
    chosen = [r for r in curation.candidates(catalog) if r["score"] >= min_score]
    made = []
    for row in chosen:
        made.append(accept(catalog, row["id"]))
    return made


#: The descriptive fields a collection should have (PLAN §7.3, FAIR F2/R1),
#: and what each is called on the command line.
DESCRIPTIVE = {"title": "title", "description": "description", "creators_json": "creators",
               "contact": "contact", "license": "license", "data_type": "data-type",
               "project": "project"}


def completeness(row) -> tuple[int, int, list[str]]:
    """How many descriptive fields are filled, of how many, and which are missing."""
    missing = [name for column, name in DESCRIPTIVE.items() if not row[column]]
    return len(DESCRIPTIVE) - len(missing), len(DESCRIPTIVE), missing
