"""Collections, controlled vocabularies, and metadata completeness.

Inference proposes; a person confirms. Nothing reaches the ``collection`` table
except through :func:`collections.create`, which is only ever called from an
explicit user action (docs/PLAN.md §8.4).
"""

from drivefusion.core.curate.candidates import (
    Candidate,
    accept,
    merge_into,
    pending,
    propose,
    record,
    reject,
)
from drivefusion.core.curate.collections import (
    Collection,
    CollectionError,
    add_relation,
    add_rule,
    all_collections,
    coverage,
    create,
    get,
    stats,
    update,
)

__all__ = [
    "Candidate", "accept", "merge_into", "pending", "propose", "record", "reject",
    "Collection", "CollectionError", "add_relation", "add_rule",
    "all_collections", "coverage", "create", "get", "stats", "update",
]
