"""Proposing collections from the folder tree (PLAN §8.4, ROADMAP M6.2).

Nothing here assigns anything. Inference scores subtrees and puts the best of
them in the review queue (``core/store/curation.py``), each with the evidence
a person needs to accept or reject it: why it was proposed, sample paths,
file and byte counts, the types in it and the span of its dates.

The signals, each between 0 and 1, from PLAN §8.4:

* **homogeneity**: the share of files in the subtree's two commonest types;
* **time clustering**: how tightly its modification times bunch, between the
  10th and 90th percentile, in months: one project, one period;
* **series**: whether the folder is one of a run of siblings named alike
  (``2021-03 Survey``, ``2021-04 Survey``) or carries a date in its name;
* **size band**: a subtree too small to matter or too big to be one dataset
  scores lower.

Choosing among nested subtrees is where content starts and a container
stops: walking down from each scan root, a subtree is proposed when it
scores above the threshold and no subtree inside it scores better, and the
walk then stops there. Candidates therefore never nest, and a scan root
itself, a whole drive or tree, is never proposed. Subtrees already in a
collection, or accepted from the queue, are left out; a rejected one stays
rejected (``curation.propose``), though folders inside it may be proposed.

The tree is read once, bottom up, with per-folder type and month counts
merged upward; no per-file list is kept (PLAN §5, memory rule).
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from drivefusion.core.store import curation

#: A subtree needs at least this many files to be worth a person's review.
MIN_FILES = 5
#: Size band, in bytes: inside it a subtree scores 1 for size, outside it
#: OUTSIDE_BAND, a nudge rather than a veto: small datasets are datasets too.
BAND = (64 << 10, 1 << 40)
OUTSIDE_BAND = 0.75
#: Score needed to be proposed.
THRESHOLD = 0.6
#: A subtree must score at least this close to the best inside it to be the
#: one proposed, so near-ties go to the larger, enclosing folder.
TIE = 0.05
WEIGHTS = {"homogeneity": 0.4, "time": 0.35, "series": 0.25}
SAMPLES = 5

_DATE = re.compile(r"(?:19|20)\d\d[-_. ]?(?:0[1-9]|1[0-2])")
_DIGITS = re.compile(r"\d+")


@dataclass
class _Node:
    id: int
    parent: int | None
    name: str
    depth: int
    files: int = 0
    bytes: int = 0
    types: Counter = field(default_factory=Counter)
    months: Counter = field(default_factory=Counter)
    children: list[int] = field(default_factory=list)
    score: float = 0.0
    signals: dict = field(default_factory=dict)
    best_inside: float = 0.0


def _month(mtime_ns: int | None) -> int | None:
    if mtime_ns is None:
        return None
    when = dt.datetime.fromtimestamp(mtime_ns / 1e9, dt.timezone.utc)
    return when.year * 12 + when.month - 1


def _month_text(month: int) -> str:
    return f"{month // 12:04d}-{month % 12 + 1:02d}"


def _percentile(months: Counter, q: float) -> int:
    total = sum(months.values())
    seen = 0
    for month in sorted(months):
        seen += months[month]
        if seen >= q * total:
            return month
    return max(months)


def _shape(name: str) -> str:
    return _DIGITS.sub("#", name.lower()).strip()


def signals(node: _Node, sibling_shapes: Counter) -> dict:
    """The four signals for one subtree; see the module docstring."""
    top_two = sum(n for _, n in node.types.most_common(2))
    homogeneity = top_two / node.files if node.files else 0.0
    if node.months:
        span = _percentile(node.months, 0.9) - _percentile(node.months, 0.1)
        time = 1.0 if span <= 3 else max(0.0, 1 - (span - 3) / 33)
    else:
        time = 0.0
    shape = _shape(node.name)
    if "#" in shape and sibling_shapes[shape] >= 3:
        series = 1.0
    elif _DATE.search(node.name):
        series = 0.5
    else:
        series = 0.0
    size = 1.0 if BAND[0] <= node.bytes <= BAND[1] else OUTSIDE_BAND
    return {"homogeneity": round(homogeneity, 3), "time": round(time, 3),
            "series": series, "size": size}


def score(s: dict) -> float:
    return round(s["size"] * sum(WEIGHTS[k] * s[k] for k in WEIGHTS), 4)


def _why(s: dict, node: _Node) -> list[str]:
    why = []
    if s["homogeneity"] >= 0.8:
        kinds = ", ".join(t or "(no extension)" for t, _ in node.types.most_common(2))
        why.append(f"{s['homogeneity']:.0%} of its files are {kinds}")
    if s["time"] >= 0.8:
        why.append("its files were last changed within a few months of each other")
    if s["series"] == 1.0:
        why.append("it is one of a series of folders named alike")
    elif s["series"]:
        why.append("its name carries a date")
    if s["size"] < 1:
        why.append("it is outside the usual size of one dataset")
    return why


def load(catalog) -> dict[int, _Node]:
    nodes = {
        row["id"]: _Node(row["id"], row["parent_id"], row["name"], row["depth"])
        for row in catalog.conn.execute(
            "SELECT id, parent_id, name, depth FROM dir WHERE vanished_at IS NULL")
    }
    for node in nodes.values():
        if node.parent in nodes:
            nodes[node.parent].children.append(node.id)
    for row in catalog.conn.execute(
        "SELECT dir_id, ext, size_bytes, mtime_ns FROM file WHERE vanished_at IS NULL"
    ):
        node = nodes.get(row["dir_id"])
        if node is None:
            continue
        node.files += 1
        node.bytes += row["size_bytes"] or 0
        node.types[(row["ext"] or "").lower()] += 1
        month = _month(row["mtime_ns"])
        if month is not None:
            node.months[month] += 1
    for node in sorted(nodes.values(), key=lambda n: n.depth, reverse=True):
        parent = nodes.get(node.parent)
        if parent is not None:
            parent.files += node.files
            parent.bytes += node.bytes
            parent.types.update(node.types)
            parent.months.update(node.months)
    return nodes


def _covered(catalog, nodes: dict[int, _Node]) -> set[int]:
    """Folders already in a collection, or accepted from the queue: their
    subtrees are curated and not proposed again."""
    tops = {r["dir_id"] for r in catalog.conn.execute(
        "SELECT dir_id FROM collection_rule WHERE kind = 'subtree'")}
    tops |= {r["dir_id"] for r in catalog.conn.execute(
        "SELECT dir_id FROM collection_candidate WHERE status = 'accepted'")}
    covered, stack = set(), [d for d in tops if d in nodes]
    while stack:
        current = stack.pop()
        covered.add(current)
        stack.extend(nodes[current].children)
    return covered


def evidence(catalog, node: _Node) -> dict:
    s = node.signals
    samples = [r["name"] for r in catalog.conn.execute(
        "WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT d.id FROM dir d "
        "JOIN tree t ON d.parent_id = t.id WHERE d.vanished_at IS NULL) "
        "SELECT f.dir_id, f.name FROM file f WHERE f.vanished_at IS NULL "
        "AND f.dir_id IN (SELECT id FROM tree) ORDER BY f.size_bytes DESC, f.id LIMIT ?",
        (node.id, SAMPLES))]
    months = node.months
    return {
        "path": catalog.dir_path(node.id),
        "why": _why(s, node),
        "signals": s,
        "files": node.files,
        "bytes": node.bytes,
        "date_span": ([_month_text(min(months)), _month_text(max(months))]
                      if months else None),
        "types": [[t or "", n] for t, n in node.types.most_common(5)],
        "samples": samples,
    }


@dataclass
class Proposal:
    dir_id: int
    score: float
    evidence: dict
    queued: bool          # False: already decided, left as it was


def infer(catalog, *, threshold: float = THRESHOLD) -> list[Proposal]:
    """Score the tree and queue the best subtrees. One audit entry for the run."""
    nodes = load(catalog)
    shapes: dict[int | None, Counter] = defaultdict(Counter)
    for node in nodes.values():
        shapes[node.parent][_shape(node.name)] += 1
    for node in nodes.values():
        if node.parent is not None and node.files >= MIN_FILES:
            node.signals = signals(node, shapes[node.parent])
            node.score = score(node.signals)
    for node in sorted(nodes.values(), key=lambda n: n.depth, reverse=True):
        parent = nodes.get(node.parent)
        if parent is not None:
            parent.best_inside = max(parent.best_inside, node.score, node.best_inside)

    covered = _covered(catalog, nodes)
    chosen: list[_Node] = []
    stack = [n.id for n in nodes.values() if n.parent is None]
    while stack:
        node = nodes[stack.pop()]
        if node.id in covered:
            continue
        if (node.parent is not None and node.score >= threshold
                and node.score >= node.best_inside - TIE):
            chosen.append(node)
            continue
        stack.extend(node.children)

    proposals = []
    conn = catalog.conn
    conn.execute("BEGIN")
    try:
        for node in sorted(chosen, key=lambda n: -n.score):
            found = evidence(catalog, node)
            queued = curation.propose(catalog, node.id, node.score, found)
            proposals.append(Proposal(node.id, node.score, found, queued is not None))
        catalog.record("collection.infer", {
            "proposed": sum(p.queued for p in proposals),
            "already_decided": sum(not p.queued for p in proposals),
            "threshold": threshold})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return proposals
