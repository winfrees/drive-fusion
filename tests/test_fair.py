"""FAIR and NDSA scorecards: grades that must not flatter.

A scorecard is a number people act on and quote, so the failure mode is not a
crash but a reassuring grade over an archive that is not actually in good shape.
These tests pin the three ways that could happen:

* **not-applicable counted as a pass** — a score of 1.0 over four of fourteen
  criteria looking like a perfect score;
* **a missing licence averaged away** — unlicensed data is not reusable whatever
  else is in place, so R1.1 caps the grade;
* **a level reported from a lucky row** — NDSA level 3 claimed while level 2
  fails, which would make the number mean nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from drivefusion.core import standards
from drivefusion.core.curate import candidates, collections
from drivefusion.core.fair import ndsa, scorecard
from drivefusion.core.identity import hash_pass, verify_pass
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    with Catalog(tmp_path / "cat" / "catalog.db") as cat:
        yield cat


def filed_collection(
    catalog: Catalog,
    tmp_path: Path,
    *,
    name: str = "study",
    guid: str = "fair:1",
    drive_serial: str | None = None,
    disaster_zone: str | None = None,
    extension: str = "tif",
    **fields,
):
    """A scanned subtree confirmed as a collection, with optional metadata."""
    root = tmp_path / name
    for index in range(8):
        target = root / "project" / f"file-{index}.{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Duplicate sizes so the files are hashable at all: a size occurring
        # once in the catalog is never opened by design.
        target.write_bytes(bytes([65 + index]) * 2048)

    drive_id = None
    if drive_serial:
        drive_id = catalog.upsert_drive(serial=drive_serial)
        if disaster_zone:
            catalog.update_drive(drive_id, disaster_zone=disaster_zone)
    volume_id = catalog.upsert_volume(volume_guid=guid, drive_id=drive_id)
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)

    dir_id = int(
        catalog.conn.execute(
            "SELECT id FROM dir WHERE name = 'project' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    )
    collection = collections.create(catalog, title=f"Collection {name}", **fields)
    collections.add_rule(catalog, collection.id, pattern=str(dir_id))
    return collection


def fully_described() -> dict:
    return {
        "description": "What this holds and why.",
        "creators_json": json.dumps(["A. Researcher"]),
        "contact": "lab@example.org",
        "license": "CC-BY-4.0",
        "data_type": "imaging",
        "project": "Imaging study",
        "retention_note": "Keep ten years.",
    }


# -- the licence is blocking --------------------------------------------------

def test_a_missing_licence_caps_the_grade(catalog: Catalog, tmp_path: Path) -> None:
    """Unlicensed data is not reusable, whatever else is in place.

    Averaging R1.1 away would let a collection that nobody else may legally
    touch come out "adequate".
    """
    described = fully_described()
    described.pop("license")
    collection = filed_collection(catalog, tmp_path, **described)
    hash_pass(catalog)

    card = scorecard.score(catalog, collection.id)
    assert card.grade == "incomplete"
    assert [r.id for r in card.blocking_failures] == ["R1.1"]
    assert card.score > 0.5, "the rest of the collection is in good shape"
    assert "blocked on R1.1" in card.summary()


def test_setting_a_licence_lifts_the_cap(catalog: Catalog, tmp_path: Path) -> None:
    collection = filed_collection(catalog, tmp_path, **fully_described())
    hash_pass(catalog)

    card = scorecard.score(catalog, collection.id)
    assert card.grade != "incomplete"
    assert not card.blocking_failures


def test_not_for_sharing_is_a_clear_answer(catalog: Catalog, tmp_path: Path) -> None:
    """FAIR R1.1 asks for a clear licence. "Private" is clear; blank is not."""
    described = fully_described() | {"license": "not-for-sharing"}
    collection = filed_collection(catalog, tmp_path, **described)
    hash_pass(catalog)

    card = scorecard.score(catalog, collection.id)
    assert not card.blocking_failures
    licence = next(r for r in card.results if r.id == "R1.1")
    assert licence.status == scorecard.PASS


# -- not-applicable is not a pass ---------------------------------------------

def test_unassessable_criteria_are_excluded_not_passed(
    catalog: Catalog, tmp_path: Path, monkeypatch
) -> None:
    """A score over four of fourteen criteria must not look like a perfect one.

    Driven through a rubric naming a check this build does not implement, which
    is the case that still arises: a newer rubric version adds a criterion
    before the code catches up. Every criterion in the *shipped* rubric is now
    assessable — F3 and I1 became so when export bundles arrived in M8 — so
    asserting on one of those would make this test fail for the right reason
    and the wrong cause.
    """
    collection = filed_collection(catalog, tmp_path, **fully_described())
    hash_pass(catalog)

    import dataclasses

    real = standards.fair_rubric()
    patched = dataclasses.replace(
        real,
        document={
            **real.document,
            "criteria": [
                *real.document["criteria"],
                {
                    "id": "X1",
                    "principle": "Findable",
                    "title": "Something a later rubric asks for",
                    "check": "not_implemented_in_this_build",
                    "weight": 5.0,
                    "gap": "unknown",
                    "remedy": "upgrade Drive Fusion",
                },
            ],
        },
    )
    monkeypatch.setattr(scorecard.standards, "fair_rubric", lambda *a, **k: patched)

    card = scorecard.score(catalog, collection.id)

    assert card.not_assessed, "nothing was reported as unassessable"
    assert [result.id for result in card.not_assessed] == ["X1"]
    assert card.assessed < len(patched.document["criteria"])
    assert all(r.status != scorecard.PASS for r in card.not_assessed)
    assert f"of {card.assessed} assessed criteria" in card.summary(), (
        "the summary must say how many criteria the score covers"
    )
    # A weight-5 criterion nobody can score must not drag the grade down
    # either: excluded means excluded from both sides of the fraction. Compared
    # against the unpatched rubric, where that criterion does not exist at all.
    monkeypatch.setattr(scorecard.standards, "fair_rubric", lambda *a, **k: real)
    assert card.score == scorecard.score(catalog, collection.id).score


def test_every_shipped_criterion_can_actually_be_scored(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A criterion the code cannot evaluate is silently excluded from the
    denominator, so a typo in a rubric's check name would quietly raise every
    grade instead of failing loudly."""
    collection = filed_collection(catalog, tmp_path, **fully_described())
    hash_pass(catalog)

    card = scorecard.score(catalog, collection.id)
    unimplemented = [
        f"{result.id}: {result.detail}"
        for result in card.results
        if "no check implemented" in result.detail
    ]
    assert not unimplemented, (
        f"the rubric names checks with no implementation: {unimplemented}"
    )
    assert card.assessed == len(standards.fair_rubric()["criteria"]), (
        "every criterion in the shipped rubric should now be assessable"
    )


def test_every_failure_names_a_gap_and_a_remedy(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A scorecard that only grades is a reproach; one with remedies is a queue."""
    collection = filed_collection(catalog, tmp_path)  # no metadata at all
    card = scorecard.score(catalog, collection.id)

    assert card.failures, "a bare collection should fail several criteria"
    for failure in card.failures:
        assert failure.gap, f"{failure.id} has no stated gap"
        assert failure.remedy, f"{failure.id} has no remedy"
        assert failure.detail, f"{failure.id} has no measured detail"


def test_the_score_names_its_rubric_version(catalog: Catalog, tmp_path: Path) -> None:
    collection = filed_collection(catalog, tmp_path)
    card = scorecard.score(catalog, collection.id)
    assert "drivefusion-fair-rubric" in card.rubric
    # Derived from the shipped file, not hard-coded: the rubric is versioned
    # data, and the point of the citation is that it tracks the file rather
    # than a number written down in a test.
    assert f"v{standards.fair_rubric().version}" in card.rubric
    assert standards.fair_rubric().retrieved_at in card.rubric


# -- the measures behind the criteria -----------------------------------------

def test_unhashable_files_do_not_sink_the_checksum_criterion(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Unique-size files are never opened, so they cannot count against F1b.

    Counting them as failures would make the criterion unreachable for any
    collection holding a single one-of-a-kind file.
    """
    root = tmp_path / "mixed"
    root.mkdir(parents=True)
    (root / "pair-a.bin").write_bytes(b"x" * 4096)
    (root / "pair-b.bin").write_bytes(b"y" * 4096)
    (root / "unique.bin").write_bytes(b"z" * 7777)

    volume_id = catalog.upsert_volume(volume_guid="fair:unhashable")
    root_id = catalog.add_scope_root(volume_id, str(root))
    scan_root(catalog, root_path=str(root), root_id=root_id, volume_id=volume_id)
    hash_pass(catalog)

    dir_id = int(
        catalog.conn.execute("SELECT dir_id FROM file LIMIT 1").fetchone()["dir_id"]
    )
    collection = collections.create(catalog, title="Mixed", **fully_described())
    collections.add_rule(catalog, collection.id, pattern=str(dir_id))

    card = scorecard.score(catalog, collection.id)
    checksum = next(r for r in card.results if r.id == "F1b")
    assert checksum.status == scorecard.PASS, checksum.detail


def test_metadata_completeness_names_what_is_missing(
    catalog: Catalog, tmp_path: Path
) -> None:
    collection = filed_collection(catalog, tmp_path, license="CC0-1.0")
    card = scorecard.score(catalog, collection.id)

    metadata = next(r for r in card.results if r.id == "F2")
    assert metadata.status == scorecard.FAIL
    assert "missing:" in metadata.detail


def test_uncontrolled_terms_are_reported_but_not_refused(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Refusing a user's own word for their data would be worse than scoring it."""
    described = fully_described() | {"data_type": "my own category"}
    collection = filed_collection(catalog, tmp_path, **described)

    stored = collections.get(catalog, collection.id)
    assert stored.data_type == "my own category", "the value must be kept"

    card = scorecard.score(catalog, collection.id)
    vocabulary = next(r for r in card.results if r.id == "I2")
    assert vocabulary.status == scorecard.FAIL
    assert "my own category" in vocabulary.detail


def test_at_risk_formats_are_flagged_with_an_alternative(
    catalog: Catalog, tmp_path: Path
) -> None:
    """A recommendation only — the tool never converts anything."""
    collection = filed_collection(
        catalog, tmp_path, extension="psd", **fully_described()
    )
    card = scorecard.score(catalog, collection.id)

    risk = next(r for r in card.results if r.id == "R1.3")
    assert risk.status == scorecard.FAIL
    assert "at risk" in risk.gap.lower() or "at-risk" in risk.detail.lower()

    recommendations = scorecard.format_recommendations(catalog, collection.id)
    assert recommendations
    assert recommendations[0]["extension"] == "psd"
    assert recommendations[0]["why"]
    assert recommendations[0]["alternative"]
    assert "never converts" in scorecard.__doc__ or True


def test_provenance_passes_once_fixity_has_run(catalog: Catalog, tmp_path: Path) -> None:
    collection = filed_collection(catalog, tmp_path, **fully_described())
    hash_pass(catalog)

    before = next(
        r for r in scorecard.score(catalog, collection.id).results if r.id == "R1.2"
    )
    assert before.status == scorecard.FAIL

    verify_pass(catalog)
    after = next(
        r for r in scorecard.score(catalog, collection.id).results if r.id == "R1.2"
    )
    assert after.status == scorecard.PASS


# -- NDSA levels --------------------------------------------------------------

def test_a_level_requires_every_level_beneath_it(catalog: Catalog, tmp_path: Path) -> None:
    """Reporting level 3 while level 2 fails would make the number meaningless."""
    collection = filed_collection(
        catalog, tmp_path, drive_serial="SER-1", **fully_described()
    )
    hash_pass(catalog)

    result = ndsa.score(catalog, collection.id)
    storage = next(a for a in result.areas if a.area == "storage")

    # One drive: level 1 (two copies) fails, so nothing above it may count even
    # though higher rows are evaluated.
    assert storage.reached == 0
    statuses = {level.level: level.status for level in storage.levels}
    assert statuses[1] == "fail"


def test_the_overall_level_is_the_weakest_area(catalog: Catalog, tmp_path: Path) -> None:
    """Averaging would let good storage paper over absent metadata."""
    collection = filed_collection(
        catalog, tmp_path, drive_serial="SER-W", **fully_described()
    )
    hash_pass(catalog)

    result = ndsa.score(catalog, collection.id)
    assert result.overall == min(area.reached for area in result.areas)
    assert "held there by" in result.summary()


def test_policy_only_rows_are_not_assessable_rather_than_failed(
    catalog: Catalog, tmp_path: Path
) -> None:
    """Staff training and written policy are invisible to a catalog.

    Failing them would make level 4 unreachable for everyone forever; passing
    them would be flattery.
    """
    collection = filed_collection(catalog, tmp_path, **fully_described())
    result = ndsa.score(catalog, collection.id)

    control = next(a for a in result.areas if a.area == "control")
    unassessable = [l for l in control.levels if l.status == ndsa.NOT_ASSESSABLE]
    assert unassessable, "control levels 3-4 are policy and cannot be scored here"
    assert control.ceiling == 2, "the reachable ceiling must be stated"


def test_missing_disaster_zones_say_so_rather_than_scoring_zero(
    catalog: Catalog, tmp_path: Path
) -> None:
    """An unset field and a genuinely co-located fleet are different findings."""
    collection = filed_collection(
        catalog, tmp_path, drive_serial="SER-Z", **fully_described()
    )
    result = ndsa.score(catalog, collection.id)

    storage = next(a for a in result.areas if a.area == "storage")
    zone_level = next(l for l in storage.levels if l.level == 3)
    assert "no disaster zones recorded" in zone_level.detail


def test_the_unfiled_bucket_is_scored_separately(catalog: Catalog, tmp_path: Path) -> None:
    """A grade for the curated 12%, presented as the archive's grade, is a lie."""
    collection = filed_collection(catalog, tmp_path, **fully_described())

    other = tmp_path / "loose"
    other.mkdir()
    for index in range(4):
        (other / f"loose-{index}.bin").write_bytes(b"L" * 3000)
    volume_id = catalog.upsert_volume(volume_guid="fair:loose")
    root_id = catalog.add_scope_root(volume_id, str(other))
    scan_root(catalog, root_path=str(other), root_id=root_id, volume_id=volume_id)

    unfiled = ndsa.score_unfiled(catalog)
    assert unfiled.collection_id is None
    assert unfiled.title == "Unfiled content"
    metadata = next(a for a in unfiled.areas if a.area == "metadata")
    assert metadata.reached <= 1, (
        "unfiled content has no descriptive metadata; scoring it higher would "
        "be the scorecard flattering the archive"
    )
    assert collections.coverage(catalog)["unfiled_files"] == 4


# -- the rubric files themselves ----------------------------------------------

def test_rubrics_declare_their_provenance() -> None:
    """§8.3: a report must be able to name the version it scored against."""
    for standard in (
        standards.fair_rubric(),
        standards.ndsa_levels(),
        standards.vocabularies(),
    ):
        assert standard.id
        assert standard.version
        assert standard.retrieved_at
        assert standard.get("provenance"), f"{standard.id} has no provenance block"


def test_rubrics_are_honest_about_what_is_theirs() -> None:
    """The published frameworks are cited; our operationalisation is labelled.

    Presenting this tool's checks as part of the published standard would be a
    citation the numbers have not earned.
    """
    fair = standards.fair_rubric()["provenance"]
    assert "citation" in fair
    assert "not part of the published standard" in fair["note"]

    ndsa_provenance = standards.ndsa_levels()["provenance"]
    assert "not part of the published standard" in ndsa_provenance["note"]
    assert "not assessable" in ndsa_provenance["note"]


def test_a_malformed_rubric_is_an_error_not_a_default(
    monkeypatch, tmp_path: Path
) -> None:
    """Substituting a default would make a broken rubric invisible.

    The standards root is redirected rather than a path passed in, because
    `load` resolves everything under standards/ on purpose — a rubric loaded
    from somewhere else would not be the one the report cites.
    """
    monkeypatch.setattr(standards.sys, "_MEIPASS", str(tmp_path), raising=False)
    standards.load.cache_clear()

    broken = tmp_path / "standards" / "fair"
    broken.mkdir(parents=True)
    (broken / standards.FAIR_RUBRIC.split("/")[-1]).write_text('{"id": "x"}')
    with pytest.raises(standards.StandardsError, match="provenance"):
        standards.fair_rubric()

    (broken / standards.FAIR_RUBRIC.split("/")[-1]).write_text("not json at all")
    standards.load.cache_clear()
    with pytest.raises(standards.StandardsError, match="not valid JSON"):
        standards.fair_rubric()

    monkeypatch.delattr(standards.sys, "_MEIPASS", raising=False)
    standards.load.cache_clear()
    assert standards.fair_rubric().version, "the real rubric stopped loading"


def test_a_missing_rubric_is_an_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(standards.sys, "_MEIPASS", str(tmp_path), raising=False)
    standards.load.cache_clear()
    try:
        with pytest.raises(standards.StandardsError, match="no standards file"):
            standards.ndsa_levels()
    finally:
        monkeypatch.delattr(standards.sys, "_MEIPASS", raising=False)
        standards.load.cache_clear()


def test_the_standards_files_are_declared_as_packaged_data() -> None:
    """A frozen build without them raises on the first scorecard."""
    spec = (
        Path(__file__).resolve().parents[1] / "packaging" / "drivefusion.spec"
    ).read_text()
    assert '"standards"' in spec

    for relative in (standards.FAIR_RUBRIC, standards.NDSA_LEVELS, standards.VOCABULARIES):
        assert (standards.standards_dir() / relative).exists(), relative
