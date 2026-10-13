"""The Plan screen: a document that must not look like a control panel.

The screen's risk is entirely presentational, and it is the same risk the whole
tool carries: a numbered list beside a drive picker looks like something that
will be carried out, and a user who believes that will go looking for the
button. So these tests check the surface — what the controls say, what the tabs
separate, whether a rationale is visible without hunting for it — rather than
re-testing the planner, which has its own suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.core.plan import document
from drivefusion.core.policy import Policy
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

pytestmark = pytest.mark.usefixtures("qt_app")

GB = 1024 ** 3


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    """A catalog with content on one drive and two empty drives to copy to."""
    root = tmp_path / "media" / "project"
    root.mkdir(parents=True)
    for index in range(40):
        (root / f"f{index:03}.dat").write_bytes(b"x" * 5000)

    path = tmp_path / "cat" / "catalog.db"
    with Catalog(path) as catalog:
        drive_id = catalog.upsert_drive(serial="PLAN-SRC", nickname="SOURCE")
        catalog.update_drive(drive_id, disaster_zone="office")
        volume_id = catalog.upsert_volume(
            volume_guid="plan:src",
            drive_id=drive_id,
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

        for name, fs, cluster, zone in (
            ("ARCHIVE", "exfat", 131072, "office"),
            ("OFFSITE", "ntfs", 4096, "parents"),
        ):
            other = catalog.upsert_drive(serial=f"PLAN-{name}", nickname=name)
            catalog.update_drive(other, disaster_zone=zone)
            catalog.upsert_volume(
                volume_guid=f"plan:{name}",
                drive_id=other,
                fs_type=fs,
                cluster_bytes=cluster,
                capacity_bytes=500 * GB,
                free_bytes=500 * GB,
            )
    return path


@pytest.fixture
def screen(catalog_path: Path):
    from drivefusion.gui.screens.plan import PlanScreen
    from drivefusion.gui.session import Session

    session = Session(catalog_path)
    widget = PlanScreen(session, runner=None)
    widget.refresh()
    yield widget
    widget.close()
    session.close()


def _button_labels(widget) -> list[str]:
    from PySide6.QtWidgets import QAbstractButton

    return [child.text() for child in widget.findChildren(QAbstractButton)]


# -- the surface --------------------------------------------------------------

def test_there_is_no_control_that_carries_the_plan_out(screen) -> None:
    """No Apply, no Run, no Execute — because there is no executor.

    A button that looked like it would do the work would be the single most
    misleading thing in the product, and its absence is the screen's main
    design decision.
    """
    labels = " | ".join(_button_labels(screen)).lower()
    for forbidden in (
        "apply", "run", "execute", "start", "go", "perform", "do it",
        "delete", "move", "remove", "clean", "dedupe", "reclaim",
    ):
        assert forbidden not in labels, f"a control offers to {forbidden!r}"


def test_the_screen_says_who_carries_the_plan_out(screen) -> None:
    from PySide6.QtWidgets import QLabel

    text = " ".join(label.text() for label in screen.findChildren(QLabel)).lower()
    assert "no executor" in text or "your own tools" in text
    assert "checklist" in text


def test_building_a_plan_fills_the_checklist_with_reasons(screen) -> None:
    """Every row carries its rationale as a visible column, not a tooltip."""
    screen._build()

    assert screen.steps.topLevelItemCount() > 0
    reasons = [
        screen.steps.topLevelItem(row).text(3)
        for row in range(screen.steps.topLevelItemCount())
    ]
    assert all(reason.strip() for reason in reasons), "a step with no visible reason"
    assert any("reduces expected loss" in reason for reason in reasons)


DESTRUCTIVE_VERBS = ("delete", "remove", "move", "purge", "erase", "wipe")


def test_no_step_kind_is_labelled_as_a_destructive_action() -> None:
    """Checked over the whole label map, not only the kinds a fixture reaches.

    An ``acquire`` step appears when no drive can take another copy, which is
    the opposite of a fixture with somewhere to copy to — so the wording of the
    rarest step is exactly the wording a scenario-driven test misses.
    """
    from drivefusion.gui.screens.plan import KIND_LABELS

    from drivefusion.core.plan.document import STEP_KINDS

    assert set(KIND_LABELS) == set(STEP_KINDS), (
        "a step kind with no label renders as its raw identifier"
    )
    for kind, label in KIND_LABELS.items():
        for forbidden in DESTRUCTIVE_VERBS:
            assert forbidden not in label.lower(), (
                f"the label for {kind!r} reads as {forbidden!r}: {label!r}"
            )


def test_the_checklist_never_offers_a_destructive_step(screen) -> None:
    screen._build()
    assert screen.steps.topLevelItemCount() > 0

    for row in range(screen.steps.topLevelItemCount()):
        label = screen.steps.topLevelItem(row).text(1).lower()
        for forbidden in DESTRUCTIVE_VERBS:
            assert forbidden not in label, f"step kind reads as {forbidden!r}"


def test_reclamation_is_a_separate_tab_and_labelled_as_evidence(screen) -> None:
    """Mixing "you could free 400 GB" into the checklist is how a planning tool
    becomes a deletion tool one button at a time."""
    titles = [screen.tabs.tabText(i) for i in range(screen.tabs.count())]
    assert "Checklist" in titles
    assert any("duplication" in title.lower() for title in titles)

    screen._build()
    first = screen.reclamation.item(0).text().lower()
    assert "evidence only" in first
    assert "does not delete" in first


def test_cost_and_benefit_are_shown_together(screen) -> None:
    """"Is this worth an afternoon" should not require arithmetic."""
    screen._build()
    html = screen.simulation.toHtml().lower()
    assert "data to copy" in html
    assert "estimated time" in html
    assert "expected loss" in html


def test_the_save_and_copy_buttons_wait_for_a_plan(screen) -> None:
    """Enabled buttons that do nothing teach users to distrust the ones that do."""
    assert not screen.save_button.isEnabled()
    assert not screen.copy_button.isEnabled()

    screen._build()
    assert screen.save_button.isEnabled()
    assert screen.copy_button.isEnabled()


def test_saving_stores_a_draft_and_says_so(screen) -> None:
    screen._build()
    screen._save()

    assert "draft" in screen.saved.text().lower()
    assert "not carry it out" in screen.saved.text().lower()

    stored = document.plans(screen.session.catalog())
    assert stored and stored[0]["status"] == "draft"


def test_the_copied_checklist_is_plain_text_a_person_can_follow(screen) -> None:
    from drivefusion.gui.screens.plan import _checklist_text

    screen._build()
    text = _checklist_text(screen.plan)

    assert screen.plan.name in text
    assert "does not" in text and "delete" in text
    assert "Policy:" in text
    for step in screen.plan.steps:
        assert step.rationale in text


def test_the_copied_checklist_contains_no_destructive_instruction(screen) -> None:
    """It leaves the application as text someone may paste into a shell."""
    from drivefusion.core import wording
    from drivefusion.gui.screens.plan import _checklist_text

    screen._build()
    wording.assert_safe(_checklist_text(screen.plan), what="checklist")


# -- the policy controls ------------------------------------------------------

def test_the_policy_comes_from_the_controls(screen) -> None:
    screen.level.setCurrentIndex(1)              # NDSA level 2
    screen.reserve.setValue(0.20)
    screen.batch_diversity.setChecked(False)

    policy = screen.policy()
    assert policy.min_copies == Policy.for_level(2).min_copies
    assert policy.reserve_fraction == pytest.approx(0.20)
    assert policy.require_batch_diversity is False


def test_every_catalogued_drive_can_be_excluded(screen) -> None:
    """Excluding a drive is the one piece of real control on this screen, and
    it is per *drive*, not per volume: exclusion is about the device that fails.
    """
    names = {box.text() for _drive_id, box in screen.exclusion_boxes}
    assert {"SOURCE", "ARCHIVE", "OFFSITE"} <= names

    for drive_id, box in screen.exclusion_boxes:
        if box.text() in {"ARCHIVE", "OFFSITE"}:
            box.setChecked(True)

    screen._build()
    assert screen.plan.acquire_steps, (
        "with every destination excluded the plan should ask for more storage"
    )
    assert not screen.plan.copy_steps


def test_the_level_choices_state_what_they_mean(screen) -> None:
    """"NDSA level 3" means nothing to most people; "3 copies" does."""
    for index in range(screen.level.count()):
        text = screen.level.itemText(index)
        assert "copies" in text
        assert "location" in text
