"""Bundles: what an export claims, and whether the claim holds.

The bundle is where the tool's promises become artifacts someone else reads, so
these tests are mostly about honesty rather than mechanics: that the bag says
its payload is a description and not the archive, that a rename plan never
ships without the table that undoes it, that a drive with nothing on it is not
told it is about to lose everything, and that nothing in any generated file
tells the reader to delete anything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from drivefusion.core import wording
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.export import bagit, bundle, naming, writer
from drivefusion.core.identity import hash_pass, verify_pass
from drivefusion.core.plan import document
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

GB = 1024 ** 3


@pytest.fixture
def archive(tmp_path: Path) -> dict:
    """A small archive with a confirmed collection, a duplicate, and two drives."""
    media = tmp_path / "media" / "project"
    (media / "2021").mkdir(parents=True)
    (media / "2023").mkdir(parents=True)
    for index in range(8):
        (media / "2021" / f"m{index}.csv").write_text(f"reading {index}\n" * 12)
    for index in range(6):
        (media / "2023" / f"scan{index}.tif").write_bytes(
            bytes([index % 251]) * 20_000
        )
    # A duplicate on the same drive, so the reclamation appendix has content.
    (media / "2023" / "scan0-copy.tif").write_bytes(bytes([0]) * 20_000)

    catalog_path = tmp_path / "cat" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        source = catalog.upsert_drive(serial="BUN-SRC", nickname="WORKING")
        catalog.update_drive(
            source, disaster_zone="office", batch_key="WD|RED|2021-03"
        )
        volume_id = catalog.upsert_volume(
            volume_guid="bun:src",
            drive_id=source,
            fs_type="ntfs",
            cluster_bytes=4096,
            capacity_bytes=500 * GB,
            free_bytes=400 * GB,
        )
        root_id = catalog.add_scope_root(volume_id, str(tmp_path / "media"))
        scan_root(
            catalog,
            root_path=str(tmp_path / "media"),
            root_id=root_id,
            volume_id=volume_id,
        )
        hash_pass(catalog)
        verify_pass(catalog)

        spare = catalog.upsert_drive(serial="BUN-ARCH", nickname="ARCHIVE")
        catalog.update_drive(
            spare, disaster_zone="parents", batch_key="HGST|DC|2024-01"
        )
        catalog.upsert_volume(
            volume_guid="bun:arch",
            drive_id=spare,
            fs_type="exfat",
            cluster_bytes=131072,
            capacity_bytes=500 * GB,
            free_bytes=500 * GB,
        )

        dir_id = int(
            catalog.conn.execute(
                "SELECT id FROM dir WHERE name = ?", ("project",)
            ).fetchone()["id"]
        )
        collection = collections_module.create(
            catalog,
            title="Project Alpha",
            license="CC-BY-4.0",
            data_type="dataset",
            project="Alpha study",
            description="Field readings and lab scans.",
        )
        collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))

    return {
        "catalog_path": catalog_path,
        "media": tmp_path / "media",
        "destination": tmp_path / "exports",
        "collection_pid": collection.local_pid,
    }


@pytest.fixture
def built(archive: dict):
    """A bundle, and the catalog it came from."""
    with Catalog(archive["catalog_path"]) as catalog:
        plan = document.build(catalog, use_cp_sat=False)
        result = bundle.build(
            catalog,
            archive["destination"],
            plan=plan,
            rename_template="{collection}/{year}/{stem}{ext}",
        )
        yield result, catalog, archive


def _tree_digest(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


# -- the bag ------------------------------------------------------------------

def test_the_bag_validates(built) -> None:
    result, _catalog, _archive = built
    assert bagit.validate(result.root) == []


def test_validation_notices_a_tampered_payload(built) -> None:
    """A manifest nobody can fail is a manifest nobody has checked."""
    result, _catalog, _archive = built
    target = result.root / "data" / "ro-crate-metadata.json"
    target.write_text(target.read_text() + "\n")

    problems = bagit.validate(result.root)
    assert problems
    assert any("does not match its manifest checksum" in p for p in problems)


def test_validation_notices_a_payload_file_nobody_listed(built) -> None:
    result, _catalog, _archive = built
    (result.root / "data" / "smuggled.txt").write_text("x")

    problems = bagit.validate(result.root)
    assert any("not in the manifest" in p for p in problems)


def test_validation_notices_a_missing_payload_file(built) -> None:
    result, _catalog, _archive = built
    (result.root / "data" / "report.html").unlink()

    problems = bagit.validate(result.root)
    assert any("not in the bag" in p for p in problems)


def test_the_bag_says_its_payload_is_the_description_not_the_archive(
    built,
) -> None:
    """The single most misleading thing this bundle could imply is that it
    contains the data. BagIt's own fields are where a reader looks."""
    result, _catalog, _archive = built
    info = (result.root / "bag-info.txt").read_text()

    assert "NOT in this bag" in info
    assert "payload is the description" in info
    assert "were not modified, moved or copied" in info
    # Payload-Oxum must describe the payload, which is the metadata — not the
    # archive's byte count, which would make the bag look like a copy of it.
    oxum = next(
        line for line in info.splitlines() if line.startswith("Payload-Oxum:")
    )
    assert oxum.split(":")[1].strip() == bagit.payload_oxum(result.root)


def test_finalising_a_bag_with_no_payload_is_refused(tmp_path: Path) -> None:
    """A manifest over nothing would validate, and mean nothing."""
    root = writer.prepare_root(tmp_path / "exports")
    with pytest.raises(writer.ExportError, match="no payload"):
        bagit.finalise(root, version="test", described={})


# -- the archive is untouched -------------------------------------------------

def test_exporting_changes_nothing_in_the_archive(archive: dict) -> None:
    """The whole point, asserted over bytes rather than argued."""
    before = _tree_digest(archive["media"])

    with Catalog(archive["catalog_path"]) as catalog:
        plan = document.build(catalog, use_cp_sat=False)
        bundle.build(
            catalog,
            archive["destination"],
            plan=plan,
            rename_template="{collection}/{year}/{stem}{ext}",
        )

    assert _tree_digest(archive["media"]) == before


def test_no_user_file_is_copied_into_the_bundle(built) -> None:
    """Drive Fusion describes; it does not copy. A bundle holding the archive's
    bytes would be a copy nobody asked for, on a disk nobody budgeted."""
    result, _catalog, archive = built
    source_digests = set(_tree_digest(archive["media"]).values())

    for path in result.root.rglob("*"):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest not in source_digests, f"{path.name} is a copy of user data"


# -- nothing tells the reader to delete --------------------------------------

def test_no_generated_file_tells_the_reader_to_remove_anything(built) -> None:
    """Every text file in the bundle, not a sample of them."""
    result, _catalog, _archive = built
    checked = 0
    for path in sorted(result.root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        wording.assert_safe(text, what=path.name)
        checked += 1
    assert checked > 10, "the walk found almost nothing to check"


def test_the_disclaimer_is_in_the_bundle(built) -> None:
    result, _catalog, _archive = built
    readme = (result.root / "README.txt").read_text()
    assert "not a compliance artifact" in readme
    assert "no executor" in readme
    assert "identifiable human-subjects data" in readme
    assert "It is not a copy of your data" in readme


# -- the rename mapping, and its inverse -------------------------------------

def test_a_rename_plan_never_ships_without_its_inverse(built) -> None:
    """Once the files are renamed the old names exist nowhere else, so the undo
    table has to be written at the same time or not at all."""
    result, _catalog, _archive = built
    plans = sorted(result.root.rglob("rename-plan.csv"))
    undos = sorted(result.root.rglob("rename-undo.csv"))

    assert plans, "no rename plan was written"
    assert len(plans) == len(undos)
    for forward in plans:
        assert (forward.parent / "rename-undo.csv").is_file()


def test_the_inverse_actually_inverts(built) -> None:
    """Writing a file called rename-undo.csv is not the same as being able to
    undo the rename."""
    import csv

    result, _catalog, _archive = built
    forward_path = next(result.root.rglob("rename-plan.csv"))
    undo_path = forward_path.parent / "rename-undo.csv"

    forward = list(csv.DictReader(forward_path.read_text().splitlines()))
    undo = list(csv.DictReader(undo_path.read_text().splitlines()))
    assert forward and len(forward) == len(undo)

    by_id = {row["file_id"]: row for row in undo}
    for row in forward:
        back = by_id[row["file_id"]]
        assert back["renamed_relative_path"] == row["proposed_relative_path"]
        assert back["restore_to"] == row["current_path"], (
            "the undo row does not restore the original path"
        )


def test_a_mapping_that_cannot_be_undone_is_refused_and_said_so(
    tmp_path: Path,
) -> None:
    """A template that collapses two files onto one name would lose one of them
    when applied, and the undo table could not say which.

    The collision is built into the fixture rather than hoped for: the first
    version of this test accepted "no collision arose", and the refusal path it
    was meant to cover never ran.
    """
    media = tmp_path / "media" / "project"
    (media / "2021").mkdir(parents=True)
    (media / "2023").mkdir(parents=True)
    # The same filename in two subfolders, which a template without {year} or
    # {parent} flattens onto one name.
    (media / "2021" / "notes.txt").write_text("first")
    (media / "2023" / "notes.txt").write_text("second")

    catalog_path = tmp_path / "cat" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        volume_id = catalog.upsert_volume(volume_guid="clash:1", fs_type="ntfs")
        root_id = catalog.add_scope_root(volume_id, str(tmp_path / "media"))
        scan_root(
            catalog,
            root_path=str(tmp_path / "media"),
            root_id=root_id,
            volume_id=volume_id,
        )
        dir_id = int(
            catalog.conn.execute(
                "SELECT id FROM dir WHERE name = ?", ("project",)
            ).fetchone()["id"]
        )
        collection = collections_module.create(catalog, title="Alpha")
        collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))

        result = bundle.build(
            catalog,
            tmp_path / "exports",
            rename_template="{collection}/{stem}{ext}",
        )

    refusals = sorted(result.root.rglob("rename-REFUSED.txt"))
    assert refusals, "a mapping with a collision was exported anyway"

    text = refusals[0].read_text()
    assert "irreversible" in text
    assert "only when it can also" in text
    assert result.warnings and "collision" in result.warnings[0]
    assert not sorted(result.root.rglob("rename-plan.csv")), (
        "a refused mapping must not also ship a plan"
    )
    assert not sorted(result.root.rglob("rename-undo.csv"))
    # And the bundle is still a valid bag: a refusal is an outcome, not a crash.
    assert bagit.validate(result.root) == []


def test_a_colliding_mapping_raises_rather_than_warning() -> None:
    """Asserted on the mapping itself, so the refusal is covered whatever a
    fixture's filenames happen to be."""
    mapping = naming.Mapping(template="{stem}{ext}")
    mapping.renames = [
        naming.Rename(1, "/a/notes.txt", "notes.txt", "t"),
        naming.Rename(2, "/b/notes.txt", "notes.txt", "t"),
    ]
    with pytest.raises(naming.NamingError, match="irreversible"):
        mapping.assert_reversible()


def test_a_template_without_the_original_name_is_refused() -> None:
    """A name that drops the stem is not decodable back to what it was, and
    throws away the one piece of information the owner chose."""
    with pytest.raises(naming.NamingError, match="must include"):
        naming.validate_template("{collection}/{year}/{pid}")


def test_an_unknown_placeholder_is_refused_before_it_hits_thousands_of_files() -> None:
    with pytest.raises(naming.NamingError, match="unknown placeholder"):
        naming.validate_template("{collection}/{invented}/{stem}{ext}")


# -- per-drive manifests ------------------------------------------------------

def test_the_drive_manifests_are_in_the_bundle_and_not_on_the_drive(
    built,
) -> None:
    """§3.3. Writing to the drive is the one thing this tool does not do, and it
    does not make an exception for its own files."""
    result, _catalog, archive = built

    assert sorted(result.root.rglob("_DRIVE-FUSION.json"))
    assert sorted(result.root.rglob("_CATALOG.md"))

    for name in ("_DRIVE-FUSION.json", "_CATALOG.md"):
        assert not list(archive["media"].rglob(name)), (
            f"{name} was written into the archive"
        )
        assert not (archive["media"].parent / name).exists()

    instructions = (result.root / "data" / "drives" / "HOW-TO-USE.txt").read_text()
    assert "Copy both onto the matching drive" in instructions
    assert "does not put them there for you" in instructions


def test_a_drive_holding_a_sole_copy_says_so_plainly(built) -> None:
    result, _catalog, _archive = built
    text = (result.root / "data" / "drives" / "WORKING" / "_CATALOG.md").read_text()
    assert "on no other drive in the catalog" in text
    assert "most important line in this file" in text


def test_a_drive_with_nothing_on_it_is_not_told_it_is_about_to_lose_everything(
    built,
) -> None:
    """The alarming sentence belongs to drives that hold a sole copy. Printing
    it for an empty drive makes the most important line in the file wrong."""
    result, _catalog, _archive = built
    text = (result.root / "data" / "drives" / "ARCHIVE" / "_CATALOG.md").read_text()

    assert "Nothing on this drive is in the catalog" in text
    assert "Losing this drive would lose" not in text


def test_the_machine_manifest_counts_what_is_held_nowhere_else(built) -> None:
    result, _catalog, _archive = built
    data = json.loads(
        (result.root / "data" / "drives" / "WORKING" / "_DRIVE-FUSION.json").read_text()
    )
    sole = data["other_copies"]["held_nowhere_else"]
    assert sole["files"] > 0
    assert sole["bytes"] > 0
    assert data["read_only_note"].startswith("Drive Fusion read this drive")


# -- metadata -----------------------------------------------------------------

def test_the_crate_describes_collections_not_fifty_million_files(built) -> None:
    result, _catalog, _archive = built
    crate = json.loads((result.root / "data" / "ro-crate-metadata.json").read_text())
    kinds = [entity["@type"] for entity in crate["@graph"]]

    assert "Dataset" in kinds
    assert "File" not in kinds, (
        "a crate with a File entity per catalogued file is unusable, and the "
        "files are not in the bundle anyway"
    )
    root = next(e for e in crate["@graph"] if e["@id"] == "./")
    assert "not included in this bundle" in root["description"]


def test_datacite_admits_what_it_does_not_know(archive: dict) -> None:
    """A record naming whoever ran the tool as the creator would validate and
    be wrong."""
    with Catalog(archive["catalog_path"]) as catalog:
        result = bundle.build(catalog, archive["destination"])

    record = json.loads(
        next(result.root.rglob("datacite.json")).read_text()
    )
    assert record["creators"][0]["name"] == "(:unav)"
    assert "does not guess" in record["_drive_fusion_note"]
    # And what it does know is there.
    assert record["titles"][0]["title"] == "Project Alpha"
    assert record["rightsList"][0]["rights"] == "CC-BY-4.0"


def test_an_unlicensed_collection_states_the_absence(archive: dict) -> None:
    """R1.1 is blocking, and an omitted key reads as an oversight where "none
    asserted" reads as the finding it is."""
    with Catalog(archive["catalog_path"]) as catalog:
        bare = collections_module.create(catalog, title="Unlicensed")
        collections_module.add_rule(catalog, bare.id, pattern="1")
        result = bundle.build(catalog, archive["destination"])

    crate = json.loads((result.root / "data" / "ro-crate-metadata.json").read_text())
    ids = {entity["@id"] for entity in crate["@graph"]}
    assert "#licence-none-asserted" in ids

    record = json.loads(
        (
            result.root
            / "data"
            / "collections"
            / naming.token(bare.local_pid)
            / "datacite.json"
        ).read_text()
    )
    assert record["rightsList"] == [{"rights": "(:unav)"}]


def test_fixity_history_ships_as_provenance(built) -> None:
    """The only evidence anyone has that the bytes are still the bytes."""
    result, _catalog, _archive = built
    premis = json.loads((result.root / "data" / "premis-events.json").read_text())

    assert premis["events"], "no fixity events in the bundle"
    assert premis["summary"].get("ok")
    event = premis["events"][0]
    assert event["eventType"] == "fixity check"
    assert event["eventOutcomeInformation"]["eventOutcome"] in {
        "ok", "mismatch", "unreadable", "missing"
    }
    assert "reads" in premis["agent"]["agentNote"].lower()
    assert "not schema-valid PREMIS XML" in premis["note"]


# -- reports ------------------------------------------------------------------

def test_the_html_report_needs_no_network_to_read(built) -> None:
    """An export that stops rendering when a CDN moves is not an archival
    format."""
    import re

    result, _catalog, _archive = built
    html = (result.root / "data" / "report.html").read_text()

    assert not re.findall(r'(?:src|href)\s*=\s*"(?:https?:)?//', html)
    assert "<style>" in html, "styles must be inline, not fetched"
    assert "Read-only" in html


def test_the_inventory_has_a_row_per_file(built) -> None:
    """It had one row for thirty-six files: an aggregate over outer-query
    columns inside a subquery made SQLite collapse the whole result."""
    result, catalog, _archive = built
    lines = (
        (result.root / "data" / "inventory" / "files.csv")
        .read_text()
        .strip()
        .splitlines()
    )
    expected = catalog.conn.execute(
        "SELECT COUNT(*) AS n FROM file WHERE vanished_at IS NULL"
    ).fetchone()["n"]

    assert len(lines) - 1 == expected, f"{len(lines) - 1} rows for {expected} files"


def test_the_inventory_distinguishes_a_sampled_hash_from_a_full_one(built) -> None:
    """A quick hash is head, middle and tail; presenting it in the same column
    as a full hash without saying so implies whole-file verification."""
    result, _catalog, _archive = built
    text = (result.root / "data" / "inventory" / "files.csv").read_text()
    assert "blake2b-quick" in text or "blake2b-full" in text
    assert "checksum_algorithm" in text.splitlines()[0]


# -- a new folder every time --------------------------------------------------

def test_a_second_export_leaves_the_first_alone(archive: dict) -> None:
    with Catalog(archive["catalog_path"]) as catalog:
        first = bundle.build(catalog, archive["destination"])
        before = _tree_digest(first.root)
        second = bundle.build(catalog, archive["destination"])

    assert first.root != second.root
    assert _tree_digest(first.root) == before
    assert bagit.validate(first.root) == []
    assert bagit.validate(second.root) == []


# -- the record ---------------------------------------------------------------

def test_the_export_is_recorded_so_fair_can_be_scored(built) -> None:
    """F3 and I1 ask whether a machine-readable description exists. Before M8
    the honest answer was "not assessable"; now it is a fact about the catalog,
    so it is stored as one."""
    result, catalog, _archive = built
    assert result.export_id

    rows = catalog.conn.execute("SELECT * FROM export").fetchall()
    assert len(rows) == 1
    record = dict(rows[0])
    assert record["payload_oxum"] == result.bag["payload_oxum"]
    assert record["files_written"] == len(result.files)

    covered = bundle.exports_covering(
        catalog, collections_module.all_collections(catalog)[0].id
    )
    assert covered
    assert covered[0]["has_ro_crate"]
    assert covered[0]["has_datacite"]
    assert covered[0]["has_manifest"]


def test_exporting_flips_the_two_criteria_that_needed_a_bundle(
    archive: dict,
) -> None:
    from drivefusion.core.fair import scorecard

    with Catalog(archive["catalog_path"]) as catalog:
        collection = collections_module.all_collections(catalog)[0]

        before = scorecard.score(catalog, collection.id)
        by_id = {result.id: result for result in before.results}
        assert by_id["F3"].status == scorecard.FAIL
        assert by_id["I1"].status == scorecard.FAIL

        bundle.build(catalog, archive["destination"])

        after = scorecard.score(catalog, collection.id)
        by_id = {result.id: result for result in after.results}
        assert by_id["F3"].status == scorecard.PASS
        assert by_id["I1"].status == scorecard.PASS
        assert after.score > before.score
        assert after.assessed == before.assessed, (
            "the criteria were already being assessed; only the answer changed"
        )


def test_a_plan_in_a_bundle_becomes_exported_not_applied(archive: dict) -> None:
    """§3.3: a plan's terminal state is "exported", and there is no other."""
    with Catalog(archive["catalog_path"]) as catalog:
        plan = document.build(catalog, use_cp_sat=False)
        plan_id = document.save(catalog, plan)
        assert document.load(catalog, plan_id)["plan"]["status"] == "draft"

        bundle.build(catalog, archive["destination"], plan=plan)

        assert document.load(catalog, plan_id)["plan"]["status"] == "exported"
        statuses = {
            dict(row)["status"]
            for row in catalog.conn.execute("SELECT DISTINCT status FROM plan")
        }
        assert statuses <= {"draft", "exported", "superseded"}
        assert "applied" not in statuses


def test_the_bundle_does_not_carry_the_catalogs_local_path(built) -> None:
    """A bundle is a thing people share, and a catalog path is usually
    C:\\Users\\<someone>\\..."""
    result, catalog, _archive = built
    catalog_path = str(catalog.path)

    for path in result.root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert catalog_path not in text, f"{path.name} contains the catalog path"

    info = (result.root / "bag-info.txt").read_text()
    assert "External-Identifier: catalog-" in info


# -- the proposed names themselves -------------------------------------------

@pytest.fixture
def awkward_names(tmp_path: Path) -> dict:
    """A collection of filenames that break naive stem/extension handling."""
    media = tmp_path / "media" / "proj"
    media.mkdir(parents=True)
    for name in (
        "a.csv",
        "archive.tar.gz",
        ".hidden",
        "noext",
        "UPPER.TIF",
        "two.words.txt",
    ):
        (media / name).write_text("x")

    catalog_path = tmp_path / "cat" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        volume_id = catalog.upsert_volume(volume_guid="awk:1", fs_type="ntfs")
        root_id = catalog.add_scope_root(volume_id, str(tmp_path / "media"))
        scan_root(
            catalog,
            root_path=str(tmp_path / "media"),
            root_id=root_id,
            volume_id=volume_id,
        )
        dir_id = int(
            catalog.conn.execute(
                "SELECT id FROM dir WHERE name = ?", ("proj",)
            ).fetchone()["id"]
        )
        collection = collections_module.create(catalog, title="Alpha")
        collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))
    return {"catalog_path": catalog_path, "collection_id": collection.id}


def test_a_proposed_name_keeps_its_extension(awkward_names: dict) -> None:
    """The catalog stores the extension lowercased and without its dot, so
    rendering ``{stem}{ext}`` from it produced "m0csv" from "m0.csv" — every
    proposed name in every rename plan had its extension welded on, and anyone
    following the table would have created extensionless files."""
    with Catalog(awkward_names["catalog_path"]) as catalog:
        mapping = naming.build(
            catalog,
            collection_id=awkward_names["collection_id"],
            template="{collection}/{stem}{ext}",
        )

    proposed = {
        entry.old_path.rsplit("/", 1)[-1]: entry.new_relative
        for entry in mapping.renames
    }
    assert proposed["a.csv"] == "Alpha/a.csv"
    assert proposed["UPPER.TIF"] == "Alpha/UPPER.TIF", "case must survive"
    assert proposed["archive.tar.gz"] == "Alpha/archive.tar.gz"
    assert proposed["two.words.txt"] == "Alpha/two.words.txt"
    assert proposed["noext"] == "Alpha/noext"


def test_a_dotfile_does_not_quietly_become_visible(awkward_names: dict) -> None:
    """A leading dot changes how a file behaves. Dropping it is a change, made
    silently, by a tool whose whole claim is that it changes nothing."""
    with Catalog(awkward_names["catalog_path"]) as catalog:
        mapping = naming.build(
            catalog,
            collection_id=awkward_names["collection_id"],
            template="{collection}/{stem}{ext}",
        )

    proposed = {
        entry.old_path.rsplit("/", 1)[-1]: entry.new_relative
        for entry in mapping.renames
    }
    assert proposed[".hidden"] == "Alpha/.hidden"


@pytest.mark.parametrize(
    "stem,expected",
    [
        (".hidden", ".hidden"),
        ("normal", "normal"),
        ("..", "untitled"),      # a path component, not a filename
        (".", "untitled"),
        ("...x", ".x"),
        (" spaced ", "spaced"),
        ("has/slash", "has-slash"),
        ('quote"mark', "quote-mark"),
    ],
)
def test_stem_tokens_are_safe_without_being_destructive(
    stem: str, expected: str
) -> None:
    assert naming._stem_token(stem) == expected


def test_no_proposed_name_escapes_its_collection_folder(
    awkward_names: dict,
) -> None:
    """A rename table is something a person runs. A row proposing a path above
    the collection would move a file somewhere nobody chose."""
    with Catalog(awkward_names["catalog_path"]) as catalog:
        mapping = naming.build(
            catalog,
            collection_id=awkward_names["collection_id"],
            template="{collection}/{parent}/{stem}{ext}",
        )

    for entry in mapping.renames:
        assert not entry.new_relative.startswith(("/", "\\"))
        assert ".." not in Path(entry.new_relative).parts
        assert entry.new_relative.startswith("Alpha/")


def test_exporting_satisfies_the_ndsa_machine_readable_metadata_level(
    archive: dict,
) -> None:
    """NDSA's level-4 metadata row asks for a standard machine-readable form.

    It used to report that the capability had not been built. RO-Crate and
    DataCite are that form, so the question now has an answer — and it is
    still subject to the levels rule, so passing level 4 does not lift an area
    whose level 3 fails.
    """
    from drivefusion.core.fair import ndsa

    with Catalog(archive["catalog_path"]) as catalog:
        collection = collections_module.all_collections(catalog)[0]

        def metadata_area():
            result = ndsa.score(catalog, collection.id)
            return next(area for area in result.areas if area.area == "metadata")

        before = metadata_area()
        level_four = next(lv for lv in before.levels if lv.level == 4)
        assert level_four.status == "fail"
        assert "export bundle" in level_four.detail
        reached_before = before.reached

        bundle.build(catalog, archive["destination"])

        after = metadata_area()
        level_four = next(lv for lv in after.levels if lv.level == 4)
        assert level_four.status == "pass"
        assert "RO-Crate" in level_four.detail
        # Every level beneath still has to hold: a level 4 pass over a failing
        # level 3 must not lift the area.
        assert after.reached == reached_before or all(
            lv.status == "pass" for lv in after.levels if lv.level < 4
        )


def test_superseded_rubric_versions_are_kept_on_disk() -> None:
    """An export cites the rubric version it was scored against. A citation
    that resolves to nothing is worse than an old file beside a new one."""
    from drivefusion.core import standards

    directory = standards.standards_dir()
    assert (directory / "fair" / "fair-rubric-v1.json").is_file()
    assert (directory / "ndsa" / "ndsa-levels-v2-v1.json").is_file()
    # And the loader points at the current ones.
    assert standards.FAIR_RUBRIC.endswith("fair-rubric-v2.json")
    assert standards.fair_rubric().version == "2.0.0"


def test_no_rubric_remedy_points_at_an_unshipped_milestone() -> None:
    """A remedy telling the reader to wait for a milestone that has landed is
    worse than no remedy: they wait."""
    from drivefusion.core import standards

    stale = []
    for criterion in standards.fair_rubric()["criteria"]:
        text = f"{criterion.get('gap', '')} {criterion.get('remedy', '')}".lower()
        for marker in ("arrives with m", "arrive with m", "waits for m", "not yet built"):
            if marker in text:
                stale.append(f"{criterion['id']}: {criterion.get('remedy')}")
    assert not stale, f"rubric remedies name an unshipped milestone: {stale}"


def test_every_rubric_shipped_is_loadable_and_names_its_provenance() -> None:
    """Including the superseded ones, which an old export may still cite."""
    import json

    from drivefusion.core import standards

    directory = standards.standards_dir()
    found = sorted(directory.rglob("*.json"))
    assert found, "no standards files shipped"

    for path in found:
        document = json.loads(path.read_text(encoding="utf-8"))
        for field in ("id", "version", "retrieved_at", "kind"):
            assert field in document, f"{path.name} has no {field}"
        assert document.get("provenance"), f"{path.name} names no provenance"
