"""The Exports screen: one decision, and no surprises about its consequence.

The screen writes files, which makes it the only screen in the application
whose buttons do something irreversible in the ordinary sense — a file exists
afterwards that did not before. So these tests are about the two things that
could go wrong: writing somewhere the user did not intend, and claiming to have
written something it did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.curate import collections as collections_module
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

pytestmark = pytest.mark.usefixtures("qt_app")


@pytest.fixture
def archive(tmp_path: Path) -> dict:
    media = tmp_path / "media" / "study"
    media.mkdir(parents=True)
    for index in range(6):
        (media / f"f{index}.csv").write_text("a,b\n1,2\n" * 8)

    catalog_path = tmp_path / "cat" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        drive_id = catalog.upsert_drive(serial="EXP-1", nickname="WORKING")
        volume_id = catalog.upsert_volume(
            volume_guid="exp:1",
            drive_id=drive_id,
            fs_type="ntfs",
            cluster_bytes=4096,
            capacity_bytes=500 * 1024 ** 3,
            free_bytes=400 * 1024 ** 3,
        )
        root_id = catalog.add_scope_root(volume_id, str(tmp_path / "media"))
        scan_root(
            catalog,
            root_path=str(tmp_path / "media"),
            root_id=root_id,
            volume_id=volume_id,
        )
        dir_id = int(
            catalog.conn.execute(
                "SELECT id FROM dir WHERE name = ?", ("study",)
            ).fetchone()["id"]
        )
        collection = collections_module.create(
            catalog, title="Study", license="CC0-1.0"
        )
        collections_module.add_rule(catalog, collection.id, pattern=str(dir_id))

    return {
        "catalog_path": catalog_path,
        "media": tmp_path / "media",
        "destination": tmp_path / "exports",
    }


@pytest.fixture
def screen(archive: dict):
    from drivefusion.gui.screens.exports import ExportsScreen
    from drivefusion.gui.session import Session

    session = Session(archive["catalog_path"])
    widget = ExportsScreen(session, runner=None)
    widget.refresh()
    yield widget, archive
    widget.close()
    session.close()


def _labels(widget) -> str:
    from PySide6.QtWidgets import QAbstractButton

    return " | ".join(
        child.text() for child in widget.findChildren(QAbstractButton)
    ).lower()


# -- the surface --------------------------------------------------------------

def test_the_screen_says_what_the_folder_is_and_is_not(screen) -> None:
    from PySide6.QtWidgets import QLabel

    widget, _archive = screen
    text = " ".join(label.text() for label in widget.findChildren(QLabel)).lower()
    assert "not a copy of your files" in text
    assert "nothing in your archive is changed" in text


def test_no_control_offers_a_destructive_action(screen) -> None:
    widget, _archive = screen
    labels = _labels(widget)
    for forbidden in (
        "delete", "remove", "erase", "move", "overwrite", "replace", "clean",
        "purge", "wipe",
    ):
        assert forbidden not in labels, f"a control offers to {forbidden!r}"


def test_the_destination_field_says_it_must_be_outside_the_archive(
    screen,
) -> None:
    widget, _archive = screen
    hint = widget.destination.placeholderText().lower()
    assert "outside your archive" in hint
    assert "timestamped" in hint


# -- writing ------------------------------------------------------------------

def test_exporting_with_no_destination_asks_for_one_rather_than_failing(
    screen,
) -> None:
    widget, _archive = screen
    widget._export()
    assert "choose a folder" in widget.status.text().lower()
    assert widget.result is None


def test_a_successful_export_lists_what_it_wrote(screen) -> None:
    """The only way to trust "it wrote 23 files" is to see them."""
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))
    widget._export()

    assert widget.result is not None
    assert widget.contents.topLevelItemCount() == len(widget.result.files)

    listed = {
        widget.contents.topLevelItem(row).text(0)
        for row in range(widget.contents.topLevelItemCount())
    }
    assert "README.txt" in listed
    assert "data/report.html" in listed
    assert "manifest-sha256.txt" in listed

    status = widget.status.text()
    assert "Verified against its own manifest" in status
    assert "report.html" in status


def test_a_destination_inside_the_archive_is_refused_in_place(screen) -> None:
    """The one realistic way this tool could write to user media. The user is
    told why, next to the field they need to change."""
    widget, archive = screen
    widget.destination.setText(str(archive["media"] / "exports"))
    widget._export()

    assert widget.result is None
    status = widget.status.text()
    assert "Nothing was written" in status
    assert "inside the scanned tree" in status
    assert widget.contents.topLevelItemCount() == 0
    assert not list(archive["media"].rglob("export-*"))


def test_a_bad_rename_template_is_caught_before_anything_is_written(
    screen,
) -> None:
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))
    widget.rename_template.setCurrentText("{collection}/{invented}/{stem}{ext}")
    widget._export()

    assert widget.result is None
    assert "will not work" in widget.status.text()
    assert "unknown placeholder" in widget.status.text()
    assert not archive["destination"].exists(), "a folder was created anyway"


def test_a_template_missing_the_original_name_is_refused(screen) -> None:
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))
    widget.rename_template.setCurrentText("{collection}/{year}/{pid}")
    widget._export()

    assert widget.result is None
    assert "must include" in widget.status.text()


def test_the_offered_templates_are_all_valid(screen) -> None:
    """A preset that fails validation is worse than no preset."""
    from drivefusion.core.export import naming

    widget, _archive = screen
    for index in range(widget.rename_template.count()):
        template = widget.rename_template.itemText(index)
        if not template:
            continue
        naming.validate_template(template)


def test_a_second_export_leaves_the_first_alone(screen) -> None:
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))

    widget._export()
    first = widget.result.root
    first_files = {
        path: path.read_bytes() for path in first.rglob("*") if path.is_file()
    }

    widget._export()
    second = widget.result.root

    assert first != second
    for path, content in first_files.items():
        assert path.read_bytes() == content


def test_the_export_history_is_shown_after_writing(screen) -> None:
    widget, archive = screen
    assert "No exports yet" in widget.previous.toHtml()

    widget.destination.setText(str(archive["destination"]))
    widget._export()

    history = widget.previous.toHtml()
    assert "Previous exports" in history
    assert str(widget.result.root) in history


def test_including_a_plan_marks_it_exported(screen) -> None:
    """§3.3: a plan's terminal state is "exported", never "applied"."""
    from drivefusion.core.plan import document

    widget, archive = screen
    catalog = widget.session.catalog()
    plan = document.build(catalog, use_cp_sat=False)
    plan_id = document.save(catalog, plan)
    assert document.load(catalog, plan_id)["plan"]["status"] == "draft"

    widget.destination.setText(str(archive["destination"]))
    widget.include_plan.setChecked(True)
    widget._export()

    assert widget.result is not None
    assert document.load(catalog, plan_id)["plan"]["status"] == "exported"
    listed = {
        widget.contents.topLevelItem(row).text(0)
        for row in range(widget.contents.topLevelItemCount())
    }
    assert "data/plan/checklist.md" in listed


def test_an_export_without_a_plan_writes_no_plan_folder(screen) -> None:
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))
    widget.include_plan.setChecked(False)
    widget._export()

    assert widget.result is not None
    assert not any(name.startswith("data/plan/") for name in widget.result.files)


def test_a_tampered_bundle_is_reported_rather_than_claimed_good(screen) -> None:
    """The screen validates what it wrote. A success message over a bundle that
    does not match its own manifest is the one claim it must never make."""
    widget, archive = screen
    widget.destination.setText(str(archive["destination"]))
    widget._export()
    root = widget.result.root

    target = root / "data" / "report.html"
    target.write_text(target.read_text() + " ")

    widget.show_result(widget.result)
    assert "does not match its own manifest" in widget.status.text()
    assert "Verified against its own manifest" not in widget.status.text()
