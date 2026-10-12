"""Collections: inference that proposes, confirmation that assigns.

The load-bearing assertion in this file is that **inference never creates a
collection**. Everything else — scoring, coverage, rules — rests on a user
having said yes, and a proposal engine that quietly filed things would make
every subsequent number describe decisions nobody made.

The second theme is coverage. At 50M files most bytes start unfiled, and a
scorecard over the filed share presented as a scorecard for the archive would
be the most misleading thing this tool could print.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from drivefusion.core.curate import candidates, collections
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

DAY = 86_400


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def scan_tree(catalog: Catalog, root: Path, *, guid: str = "cur:1") -> int:
    volume_id = catalog.upsert_volume(volume_guid=guid, fs_type="ntfs")
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)
    return volume_id


def write(path: Path, payload: bytes, *, days_ago: float = 10.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    when = time.time() - days_ago * DAY
    import os

    os.utime(path, (when, when))


def project_tree(root: Path) -> Path:
    """A subtree that looks like one project: one type, one period, dated parts."""
    project = root / "microscopy-study"
    for month in ("2024-03", "2024-04", "2024-05"):
        for index in range(10):
            write(
                project / f"{month}-imaging" / f"scan-{index:03d}.tif",
                b"T" * (300_000 + index),
                days_ago=400,
            )
    return project


def dumping_ground(root: Path) -> Path:
    """Mixed types over years, under a name that is pure container."""
    junk = root / "misc"
    for index, extension in enumerate(
        ["psd", "doc", "mp3", "txt", "exe", "zip", "jpg", "mov", "xls", "dll"]
    ):
        write(
            junk / f"thing-{index}.{extension}",
            b"J" * (200_000 + index),
            days_ago=200 + index * 500,
        )
    return junk


# -- inference proposes, and only proposes ------------------------------------

def test_proposing_creates_no_collections(catalog: Catalog, tmp_path: Path) -> None:
    """The rule the whole milestone rests on (§8.4).

    If inference could assign, every scorecard afterwards would describe
    groupings nobody chose.
    """
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    proposals = candidates.propose(catalog)
    assert proposals, "nothing was proposed, so this test asserts nothing"
    assert collections.all_collections(catalog) == []

    candidates.record(catalog, proposals)
    assert collections.all_collections(catalog) == [], (
        "recording a proposal must not create a collection"
    )


def test_a_coherent_project_outranks_a_dumping_ground(
    catalog: Catalog, tmp_path: Path
) -> None:
    """The signals have to separate these two, or the queue is noise."""
    root = tmp_path / "drive"
    project_tree(root)
    dumping_ground(root)
    scan_tree(catalog, root)

    proposals = candidates.propose(catalog, min_confidence=0.0)
    by_name = {Path(p.path).name: p for p in proposals}

    assert "microscopy-study" in by_name, "the coherent project was not proposed"
    project = by_name["microscopy-study"]
    junk = by_name.get("misc")
    if junk is not None:
        assert project.confidence > junk.confidence
    assert project.confidence > 0.6


def test_a_container_name_is_not_proposed_as_a_subject(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "drive"
    dumping_ground(root)
    scan_tree(catalog, root)

    names = {Path(p.path).name for p in candidates.propose(catalog)}
    assert "misc" not in names


def test_only_one_candidate_per_path(catalog: Catalog, tmp_path: Path) -> None:
    """A project and each of its dated sub-folders must not all be proposed.

    Accepting the parent would otherwise leave stale proposals behind, and
    accepting all of them would create overlapping collections over the same
    bytes. The first version of the suppression deduplicated on `dir_id`, which
    is unique by construction, so it did nothing at all.
    """
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    paths = [p.path for p in candidates.propose(catalog, min_confidence=0.0)]
    for outer in paths:
        nested = [p for p in paths if p != outer and p.startswith(outer + "/")]
        assert not nested, f"{outer} was proposed alongside its descendants {nested}"


def test_sibling_folders_with_the_same_filenames_give_distinct_samples(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Sample *paths*, not names: three identical names tell a reviewer nothing."""
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    proposal = candidates.propose(catalog)[0]
    assert len(set(proposal.sample_paths)) == len(proposal.sample_paths)
    assert all("/" in sample or "\\\\" in sample for sample in proposal.sample_paths)


def test_every_candidate_carries_auditable_evidence(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A proposal you cannot audit is one you cannot safely accept."""
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    proposal = candidates.propose(catalog)[0]
    evidence = proposal.evidence()

    assert evidence["path"]
    assert evidence["files"] > 0
    assert evidence["signals"], "no signals recorded"
    assert all(signal["detail"] for signal in evidence["signals"])
    assert proposal.why(), "no human-readable reasoning"

    # Stored with the candidate, because the catalog may have changed by the
    # time somebody reviews the queue.
    candidates.record(catalog, [proposal])
    stored = candidates.pending(catalog)[0]
    assert json.loads(stored["evidence_json"])["signals"]


# -- confirmation -------------------------------------------------------------

def test_accepting_a_candidate_creates_a_rule_not_a_file_list(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Content that appears in the subtree later must join by itself."""
    root = tmp_path / "drive"
    project = project_tree(root)
    volume_id = scan_tree(catalog, root)

    candidates.record(catalog, candidates.propose(catalog))
    queued = candidates.pending(catalog)[0]
    collection = candidates.accept(catalog, queued["id"], title="Study")

    before = collections.stats(catalog, collection.id)
    assert before["files"] == 30

    # New files land inside the confirmed subtree, and a rescan picks them up.
    for index in range(5):
        write(project / "2024-06-imaging" / f"scan-{index}.tif", b"N" * 300_000)
    root_id = catalog.scope_roots()[0].id
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)

    after = collections.stats(catalog, collection.id)
    assert after["files"] == 35, "a membership rule must keep collecting"


def test_a_rejected_candidate_is_not_proposed_again(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Re-offering a no trains the user to click through without reading."""
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    candidates.record(catalog, candidates.propose(catalog))
    queued = candidates.pending(catalog)[0]
    assert candidates.reject(catalog, queued["id"])

    assert candidates.pending(catalog) == []
    again = [p.dir_id for p in candidates.propose(catalog)]
    assert queued["dir_id"] not in again


def test_an_accepted_subtree_is_not_proposed_again(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    candidates.record(catalog, candidates.propose(catalog))
    queued = candidates.pending(catalog)[0]
    candidates.accept(catalog, queued["id"])

    again = [p.dir_id for p in candidates.propose(catalog)]
    assert queued["dir_id"] not in again, "a filed subtree was offered again"


def test_a_candidate_cannot_be_accepted_twice(catalog: Catalog, tmp_path: Path) -> None:
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)
    candidates.record(catalog, candidates.propose(catalog))
    queued = candidates.pending(catalog)[0]

    candidates.accept(catalog, queued["id"])
    with pytest.raises(collections.CollectionError, match="already"):
        candidates.accept(catalog, queued["id"])


def test_merging_adds_a_rule_to_an_existing_collection(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "drive"
    project_tree(root)
    write(root / "second-study" / "a.tif", b"S" * 400_000, days_ago=380)
    for index in range(10):
        write(root / "second-study" / f"b{index}.tif", b"S" * 400_000, days_ago=380)
    scan_tree(catalog, root)

    candidates.record(catalog, candidates.propose(catalog, min_confidence=0.0))
    queue = candidates.pending(catalog)
    first = candidates.accept(catalog, queue[0]["id"], title="Combined")

    if len(queue) > 1:
        merged = candidates.merge_into(catalog, queue[1]["id"], first.id)
        assert merged.id == first.id
        assert len(collections.rules(catalog, first.id)) == 2


# -- identifiers and metadata -------------------------------------------------

def test_the_local_pid_survives_a_rename(catalog: Catalog, tmp_path: Path) -> None:
    """An identifier that tracked the title would break every reference to it."""
    collection = collections.create(catalog, title="Original name")
    pid = collection.local_pid

    collections.update(catalog, collection.id, title="Completely different")
    assert collections.get(catalog, collection.id).local_pid == pid


def test_the_local_pid_is_not_editable(catalog: Catalog) -> None:
    collection = collections.create(catalog, title="Fixed identity")
    with pytest.raises(collections.CollectionError, match="permanent"):
        collections.update(catalog, collection.id, local_pid="df:something-else")


def test_pids_are_unique_even_for_identical_titles(catalog: Catalog) -> None:
    first = collections.create(catalog, title="Same Title")
    second = collections.create(catalog, title="Same Title")
    assert first.local_pid != second.local_pid


def test_a_collection_needs_a_title(catalog: Catalog) -> None:
    with pytest.raises(collections.CollectionError):
        collections.create(catalog, title="   ")


def test_relations_must_use_a_datacite_type(catalog: Catalog) -> None:
    collection = collections.create(catalog, title="Related")
    collections.add_relation(
        catalog, collection.id, to_ref="df:other-1", relation_type="IsVersionOf"
    )
    assert collections.relations(catalog, collection.id)[0]["relation_type"] == (
        "IsVersionOf"
    )

    with pytest.raises(collections.CollectionError, match="relationType"):
        collections.add_relation(
            catalog, collection.id, to_ref="x", relation_type="SortOfRelatedTo"
        )


# -- membership and coverage --------------------------------------------------

def test_a_collection_with_no_rules_contains_nothing(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Not everything. A rule-less collection is empty, and must read as empty."""
    root = tmp_path / "drive"
    project_tree(root)
    scan_tree(catalog, root)

    collection = collections.create(catalog, title="Declared but unmapped")
    assert collections.stats(catalog, collection.id)["files"] == 0


def test_coverage_reports_the_unfiled_bucket(catalog: Catalog, tmp_path: Path) -> None:
    """Most bytes start unfiled; hiding that would make every grade a lie."""
    root = tmp_path / "drive"
    project_tree(root)
    dumping_ground(root)
    scan_tree(catalog, root)

    before = collections.coverage(catalog)
    assert before["filed_bytes"] == 0
    assert before["unfiled_bytes"] == before["total_bytes"]
    assert before["byte_share"] == 0.0

    candidates.record(catalog, candidates.propose(catalog))
    queued = candidates.pending(catalog)[0]
    candidates.accept(catalog, queued["id"], title="Study")

    after = collections.coverage(catalog)
    assert after["filed_bytes"] > 0
    assert after["unfiled_bytes"] > 0, "the dumping ground should still be unfiled"
    assert after["filed_bytes"] + after["unfiled_bytes"] == after["total_bytes"]
    assert 0 < after["byte_share"] < 1


def test_coverage_warns_when_collections_overlap(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Two collections over the same subtree must not double-count the bytes."""
    root = tmp_path / "drive"
    project = project_tree(root)
    scan_tree(catalog, root)

    dir_id = int(
        catalog.conn.execute(
            "SELECT id FROM dir WHERE name = ?", (project.name,)
        ).fetchone()["id"]
    )
    first = collections.create(catalog, title="One")
    second = collections.create(catalog, title="Two")
    collections.add_rule(catalog, first.id, pattern=str(dir_id))
    collections.add_rule(catalog, second.id, pattern=str(dir_id))

    report = collections.coverage(catalog)
    assert report["overlap_warning"]
    assert report["filed_bytes"] <= report["total_bytes"], (
        "overlapping rules must not inflate the filed total past the whole"
    )


def test_unfiled_filter_selects_what_no_collection_claims(
    catalog: Catalog, tmp_path: Path
) -> None:
    root = tmp_path / "drive"
    project_tree(root)
    dumping_ground(root)
    scan_tree(catalog, root)

    candidates.record(catalog, candidates.propose(catalog))
    candidates.accept(catalog, candidates.pending(catalog)[0]["id"])

    where, params = collections.unfiled_dir_filter(catalog)
    rows = catalog.conn.execute(
        f"SELECT COUNT(*) AS n FROM file f WHERE f.vanished_at IS NULL AND {where}",
        params,
    ).fetchone()
    assert rows["n"] == 10, "the unfiled count should be the dumping ground only"


def test_extension_rules_select_by_type(catalog: Catalog, tmp_path: Path) -> None:
    root = tmp_path / "drive"
    project_tree(root)
    dumping_ground(root)
    scan_tree(catalog, root)

    collection = collections.create(catalog, title="Everything TIFF")
    collections.add_rule(catalog, collection.id, kind="extension", pattern="tif")
    assert collections.stats(catalog, collection.id)["files"] == 30


def test_an_unknown_rule_kind_is_refused(catalog: Catalog) -> None:
    collection = collections.create(catalog, title="Strict")
    with pytest.raises(collections.CollectionError):
        collections.add_rule(catalog, collection.id, kind="telepathy", pattern="x")
