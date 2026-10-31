"""The window, its workers, and the guarantee as the UI states it.

The CLI has a test asserting no destructive verb exists in its command surface.
The GUI needs the same assertion for the same reason: the read-only promise is
only as good as the narrowest surface that could break it, and a button is a
surface. So the whole widget tree is walked and every control's text checked.

The threading tests matter because the failure they prevent is not a crash but a
frozen window, and the cancellation tests because a Cancel button that does
nothing until the work finishes is worse than no Cancel button.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog

pytestmark = pytest.mark.usefixtures("qt_app")

#: Verbs that must not appear on any control. Same list the CLI test uses.
FORBIDDEN = (
    "delete",
    "remove",
    "erase",
    "move",
    "dedupe",
    "deduplicate",
    "purge",
    "clean",
    "reclaim",
    "apply",
    "execute",
    "trash",
    "wipe",
    "overwrite",
)


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    """A catalog with a scanned root, and its path."""
    root = tmp_path / "media"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a" * 100)
    # Same size as a.bin, so the pair is genuinely unknown until hashed.
    (root / "b.bin").write_bytes(b"b" * 100)
    (root / "sub" / "c.bin").write_bytes(b"c" * 300)

    catalog_path = tmp_path / "cat" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        volume_id = catalog.upsert_volume(volume_guid="shell:1", fs_type="ntfs")
        root_id = catalog.add_scope_root(volume_id, str(root))
        scan_root(
            catalog, root_path=str(root), root_id=root_id, volume_id=volume_id
        )
    return catalog_path


@pytest.fixture
def window(populated: Path):
    from drivefusion.gui.main_window import MainWindow

    win = MainWindow(populated)
    yield win
    win.close()


# -- the guarantee, on the UI surface -----------------------------------------

def _controls(widget):
    """Every control in a widget tree whose label a user can act on."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QAbstractButton

    found = []
    for child in widget.findChildren(QAbstractButton):
        found.append(("button", child.text()))
    for action in widget.findChildren(QAction):
        found.append(("action", action.text()))
    return found


def test_no_control_offers_a_destructive_action(window) -> None:
    """There is no delete button because there is no delete capability.

    Labels and tooltips may well say the word — "Drive Fusion never deletes" is
    the point — but nothing a user can press may.
    """
    offenders = []
    for kind, text in _controls(window):
        lowered = text.lower()
        for verb in FORBIDDEN:
            if re.search(rf"\b{verb}", lowered):
                offenders.append(f"{kind}: {text!r} (matched {verb!r})")
    assert not offenders, "destructive control(s) exposed: " + "; ".join(offenders)


def test_the_window_states_the_read_only_guarantee(window) -> None:
    """§11: state it wherever a user would expect a destructive action."""
    from drivefusion.gui.main_window import READ_ONLY_NOTICE

    assert window.notice.text() == READ_ONLY_NOTICE
    lowered = READ_ONLY_NOTICE.lower()
    assert "read-only" in lowered
    assert "ever modified" in lowered


def test_forget_button_promises_to_keep_what_was_catalogued(window) -> None:
    """Dropping a root from scope is not a deletion, and must not read as one."""
    text = window.scope.forget_button.text().lower()
    assert "stop scanning" in text
    assert "delete" not in text


def test_unbuilt_screens_are_shown_as_pending_not_broken(window) -> None:
    """A control that silently does nothing is a bug report waiting to happen."""
    from PySide6.QtCore import Qt

    from drivefusion.gui.main_window import PLANNED

    assert window.stack.count() == 8
    assert window.nav.count() == 8 + len(PLANNED)

    for offset, (name, milestone) in enumerate(PLANNED):
        item = window.nav.item(8 + offset)
        assert name in item.text()
        assert milestone in item.text()
        assert item.flags() == Qt.NoItemFlags, "a pending screen must not be clickable"


# -- threading ----------------------------------------------------------------

def test_each_thread_gets_its_own_catalog_connection(populated: Path) -> None:
    """One sqlite connection per thread, or sqlite3 refuses — or worse, does not.

    ``check_same_thread=False`` would make this "work" while letting two
    transactions interleave on one connection, so the design is asserted rather
    than assumed.
    """
    from drivefusion.gui.session import Session

    with Session(populated) as session:
        main = session.catalog()
        assert session.catalog() is main, "the same thread must reuse its handle"

        other: list = []

        def in_thread():
            other.append(session.catalog())

        thread = threading.Thread(target=in_thread)
        thread.start()
        thread.join()

        assert other[0] is not main, "a worker borrowed the UI thread's connection"
        assert session.connections == 2

    assert session.connections == 0, "shutdown left connections open"


def test_a_worker_can_read_the_catalog_while_holding_its_own_handle(
    populated: Path,
) -> None:
    from drivefusion.gui.session import Session
    from drivefusion.gui.workers import TaskRunner

    with Session(populated) as session:
        runner = TaskRunner(session)
        results: list = []

        def body(catalog, _progress, _cancel):
            return catalog.counts()["files"]

        task = runner.start(body, name="count")
        task.signals.finished.connect(results.append)
        runner.wait(10_000)
        _drain()

        assert results == [3]


def test_a_failing_task_reports_instead_of_dying_silently(populated: Path) -> None:
    """A worker traceback that only reaches stderr is invisible in a frozen build."""
    from drivefusion.gui.session import Session
    from drivefusion.gui.workers import TaskRunner

    with Session(populated) as session:
        runner = TaskRunner(session)
        failures: list = []

        def body(_catalog, _progress, _cancel):
            raise ValueError("deliberate")

        task = runner.start(body, name="boom")
        task.signals.failed.connect(lambda msg, detail: failures.append((msg, detail)))
        runner.wait(10_000)
        _drain()

        assert failures, "a failing task reported nothing"
        message, detail = failures[0]
        assert "deliberate" in message
        assert "ValueError" in detail, "the traceback must reach the UI"


def test_a_real_scan_stops_within_a_directory_of_cancel(window, populated: Path) -> None:
    """REVIEW F-13, end to end: the scan body hands its Cancellation to the
    scanner, which checks it per directory. A Cancellation that fires on its
    second check must stop the scan inside the first root."""
    from drivefusion.core.errors import OperationCancelled
    from drivefusion.gui.workers import Cancellation

    class SecondCheckCancels(Cancellation):
        checks = 0

        def raised(self) -> None:
            SecondCheckCancels.checks += 1
            if SecondCheckCancels.checks >= 3:  # one before the root, then per directory
                raise OperationCancelled()

    from drivefusion.core import discovery

    with Catalog(populated) as catalog:
        # The fixture's volume id is made up; make it the real one, so the
        # root binds (REVIEW F-01) and the scan actually starts.
        root = catalog.scope_roots()[0]
        catalog.conn.execute(
            "UPDATE volume SET volume_guid = ? WHERE id = ?",
            (discovery.volume_guid_of(root.path), root.volume_id),
        )
    body = window.scope.scan_body()
    with Catalog(populated) as catalog:
        with pytest.raises(OperationCancelled):
            body(catalog, lambda _p: None, SecondCheckCancels())
        statuses = [r["status"] for r in catalog.conn.execute("SELECT status FROM scan")]
    assert statuses[-1] == "aborted"


def test_cancellation_stops_work_partway_through(populated: Path) -> None:
    """Cancel must bite between units of work, not at the end of the job."""
    from drivefusion.gui.session import Session
    from drivefusion.gui.workers import TaskRunner

    with Session(populated) as session:
        runner = TaskRunner(session)
        cancelled: list = []
        iterations = {"count": 0}
        started = threading.Event()

        def body(_catalog, _progress, cancel):
            for _ in range(10_000):
                iterations["count"] += 1
                started.set()
                cancel.raised()
                time.sleep(0.001)
            return "finished"

        task = runner.start(body, name="long")
        task.signals.cancelled.connect(lambda: cancelled.append(True))

        assert started.wait(5), "the task never started"
        task.cancel()
        runner.wait(10_000)
        _drain()

        assert cancelled == [True], "cancellation was not reported"
        assert iterations["count"] < 10_000, "the task ran to completion anyway"


def test_progress_reports_reach_a_listener(populated: Path) -> None:
    from drivefusion.gui.session import Session
    from drivefusion.gui.workers import Progress, TaskRunner

    with Session(populated) as session:
        runner = TaskRunner(session)
        seen: list = []

        def body(_catalog, progress, _cancel):
            progress(Progress("half way", 1, 2))
            return None

        task = runner.start(body, name="reporting")
        task.signals.progress.connect(seen.append)
        runner.wait(10_000)
        _drain()

        assert [p.message for p in seen] == ["half way"]
        assert seen[0].fraction == 0.5


# -- the browser --------------------------------------------------------------

def test_selecting_a_folder_shows_its_files(window) -> None:
    root_id = window.browser.tree_model.dir_id_at(window.browser.tree_model.index(0, 0))
    window.browser.select_directory(root_id)

    names = [
        window.browser.table_model.row_at(i).name
        for i in range(window.browser.table_model.rowCount())
    ]
    assert names == ["a.bin", "b.bin"]


def test_a_late_count_for_another_folder_is_dropped(window) -> None:
    """A stale count would caption one folder with another folder's total.

    Not a crash — a wrong number, arriving milliseconds after a click, which is
    exactly the kind of bug that never reproduces on demand.
    """
    browser = window.browser
    root_id = browser.tree_model.dir_id_at(browser.tree_model.index(0, 0))
    browser.select_directory(root_id)

    # A count for a directory the user has since navigated away from.
    browser._count_arrived((root_id + 999, 4242))
    assert browser.table_model.total != 4242

    browser._count_arrived((root_id, 2))
    assert browser.table_model.total == 2


def test_the_status_line_follows_the_count(window) -> None:
    browser = window.browser
    root_id = browser.tree_model.dir_id_at(browser.tree_model.index(0, 0))
    browser.select_directory(root_id)
    assert "counting" in browser.status.text()

    browser._count_arrived((root_id, 2))
    assert browser.status.text() == "2 files"


# -- dashboard ----------------------------------------------------------------

def test_dashboard_states_partial_coverage(window) -> None:
    """Numbers over an unhashed catalog must not read as numbers over all of it."""
    text = window.dashboard.coverage.text().lower()
    assert "coverage" in text
    assert "no content identity yet" in text


def test_dashboard_says_reclamation_is_an_observation(window) -> None:
    assert "never deletes" in window.dashboard.detail.text().lower()


def test_drives_screen_lists_the_scanned_volume(window) -> None:
    assert window.drives.tree.topLevelItemCount() == 1
    item = window.drives.tree.topLevelItem(0)
    assert item.text(0) == "(drive not identified)"
    assert item.text(1) == "unverified"
    assert "unverified drive" in window.drives.note.text()


def test_an_unverified_drive_can_be_confirmed_from_the_drives_screen(window) -> None:
    """REVIEW F-05: the user settles what the hardware couldn't."""
    screen = window.drives
    assert not screen.own_drive_button.isEnabled(), "nothing selected yet"
    screen.tree.setCurrentItem(screen.tree.topLevelItem(0))
    assert screen.own_drive_button.isEnabled() and screen.on_drive_button.isEnabled()

    changed = []
    screen.catalogChanged.connect(lambda: changed.append(True))
    screen.confirm_own_drive()

    item = screen.tree.topLevelItem(0)
    assert item.text(1) == "confirmed by you"
    assert window.session.catalog().unverified_volumes() == []
    assert changed, "the dashboard must hear that copy counts changed"
    screen.tree.setCurrentItem(item)
    assert not screen.own_drive_button.isEnabled(), "a confirmed drive is settled"


def test_a_volume_can_be_put_on_a_known_drive(window) -> None:
    catalog = window.session.catalog()
    drive = catalog.upsert_drive(serial="KNOWN-1", model="Archive 4TB")
    volume = catalog.unverified_volumes()[0]["id"]
    window.drives.assign_to_drive(volume, drive)
    assert window.drives.tree.topLevelItem(0).text(1) == "serial number"
    assert window.drives._known_drives()[f"drive {drive} Archive 4TB"] == drive


def test_a_drive_identified_by_hardware_cannot_be_reassigned(window, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    catalog = window.session.catalog()
    volume = catalog.unverified_volumes()[0]["id"]
    real = catalog.upsert_drive(serial="REAL-1")
    catalog.assign_volume_drive(volume, real)      # stands in for discovery
    catalog.conn.execute("UPDATE drive SET identity = 'serial' WHERE id = ?", (real,))
    other = catalog.upsert_drive(serial="OTHER-2")

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window.drives.assign_to_drive(volume, other)
    assert warnings and "can't be overruled" in warnings[0]


# -- elevation ----------------------------------------------------------------

def test_elevation_is_declined_gracefully_off_windows() -> None:
    from drivefusion.gui.elevation import Decision, assess

    state = assess(windows=False)
    assert state.decision is Decision.NOT_WINDOWS
    assert not state.can_prompt
    assert not state.journal_available


def test_elevation_offers_a_prompt_only_with_a_helper_present(tmp_path: Path) -> None:
    from drivefusion.gui.elevation import Decision, assess
    from drivefusion.core.enum.helper.client import HELPER_EXECUTABLE

    missing = assess(windows=True, elevated=False, helper=tmp_path / HELPER_EXECUTABLE)
    assert missing.decision is Decision.HELPER_MISSING
    assert not missing.can_prompt
    assert "walk directories instead" in missing.detail

    helper = tmp_path / HELPER_EXECUTABLE
    helper.write_bytes(b"MZ")
    present = assess(windows=True, elevated=False, helper=helper)
    assert present.decision is Decision.CAN_PROMPT
    assert present.can_prompt


def test_already_elevated_reports_the_journal_as_available() -> None:
    from drivefusion.gui.elevation import Decision, assess

    state = assess(windows=True, elevated=True)
    assert state.decision is Decision.ALREADY_ELEVATED
    assert state.journal_available


def test_elevation_refuses_to_run_anything_but_the_helper(tmp_path: Path) -> None:
    """A `runas` launch of an arbitrary path would hand out Administrator."""
    from drivefusion.gui.elevation import (
        Decision,
        ElevationError,
        ElevationState,
        request,
    )

    impostor = tmp_path / "notepad.exe"
    impostor.write_bytes(b"MZ")
    state = ElevationState(Decision.CAN_PROMPT, impostor, "")

    with pytest.raises(ElevationError, match="refusing to elevate"):
        request(state)


def test_elevation_rationale_does_not_overstate_the_need() -> None:
    """Declining costs a faster rescan, nothing else, and the text must say so."""
    from drivefusion.gui.elevation import RATIONALE

    assert "still catalogs everything" in RATIONALE
    assert "nothing on your drives is written to either way" in RATIONALE.lower()


def _drain() -> None:
    """Let queued cross-thread signals be delivered."""
    from PySide6.QtCore import QCoreApplication

    for _ in range(10):
        QCoreApplication.processEvents()
        time.sleep(0.01)


# -- refusals (docs/REVIEW.md F-01, F-03) --------------------------------------

def test_scan_refuses_a_root_on_a_different_volume(window, populated: Path) -> None:
    """The fixture records volume "shell:1"; the path is really on another one."""
    from drivefusion.core.errors import RootRefusedError
    from drivefusion.gui.workers import Cancellation

    body = window.scope.scan_body()
    with Catalog(populated) as catalog:
        before = catalog.scans(limit=100)
        with pytest.raises(RootRefusedError, match="not the catalogued volume"):
            body(catalog, lambda _p: None, Cancellation())
        assert catalog.scans(limit=100) == before, "nothing was scanned"


def test_hash_refuses_a_root_on_a_different_volume(
    window, populated: Path, monkeypatch
) -> None:
    from drivefusion.core.errors import RootRefusedError
    from drivefusion.gui.workers import Cancellation

    captured = []
    # Capture the task body instead of starting it on the pool.
    monkeypatch.setattr(window, "run_task", lambda body, _label: captured.append(body))
    window.dashboard.request_hash()
    with Catalog(populated) as catalog:
        with pytest.raises(RootRefusedError, match="Nothing was hashed"):
            captured[0](catalog, lambda _p: None, Cancellation())
        assert catalog.counts()["identified"] == 0


def test_adding_a_folder_that_holds_the_catalog_is_refused(
    window, populated: Path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: warnings.append(args[2])
    )
    assert window.scope.add_path(str(populated.parent)) is False
    assert "contains the catalog" in warnings[0]


def test_adding_a_folder_on_the_catalogs_data_drive_is_refused(
    window, populated: Path, tmp_path: Path, monkeypatch
) -> None:
    """Only the system drive may hold both the catalog and scanned folders."""
    from PySide6.QtWidgets import QMessageBox

    from drivefusion.core import discovery

    monkeypatch.setattr(discovery, "system_volume_guid", lambda: "vol:some-other-drive")
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: warnings.append(args[2])
    )
    elsewhere = tmp_path / "on-the-same-drive"
    elsewhere.mkdir()
    roots_before = len(window.session.catalog().scope_roots())
    assert window.scope.add_path(str(elsewhere)) is False
    assert "same drive as the catalog" in warnings[0]
    assert len(window.session.catalog().scope_roots()) == roots_before


# -- the first-run disclaimer (docs/PLAN.md §1, REVIEW SR-25) ------------------

def test_no_drive_is_read_from_the_gui_before_the_disclaimer_is_accepted(
    window, tmp_path: Path, monkeypatch
) -> None:
    from drivefusion.core import disclaimer
    from drivefusion.gui import consent

    monkeypatch.delenv("DRIVEFUSION_ACCEPT_DISCLAIMER", raising=False)
    asked = []
    monkeypatch.setattr(consent, "_ask", lambda parent: asked.append(parent) or False)
    emitted = []
    window.dashboard.hashRequested.connect(emitted.append)

    folder = tmp_path / "another"
    folder.mkdir()
    roots_before = len(window.session.catalog().scope_roots())
    assert window.scope.add_path(str(folder)) is False
    assert window.scope.scan_body() is None
    window.dashboard.request_hash()
    assert emitted == [], "a hash task was started"
    assert len(window.session.catalog().scope_roots()) == roots_before
    assert len(asked) == 3, "each action asked, and each was declined"
    assert not disclaimer.accepted(window.session.catalog())

    monkeypatch.setattr(consent, "_ask", lambda parent: True)
    assert window.scope.scan_body() is not None
    assert disclaimer.accepted(window.session.catalog())


def test_the_dialog_acceptance_is_recorded(window, monkeypatch) -> None:
    import json

    from drivefusion.core.store import audit
    from drivefusion.gui import consent

    monkeypatch.delenv("DRIVEFUSION_ACCEPT_DISCLAIMER", raising=False)
    monkeypatch.setattr(consent, "_ask", lambda parent: True)
    assert consent.ensure(None, window.session.catalog())
    last = audit.entries(window.session.catalog().conn, limit=1)[0]
    assert last["action"] == "disclaimer.accepted"
    assert json.loads(last["detail_json"])["how"] == "dialog"


def test_declining_at_startup_closes_without_reading(qt_app, populated: Path, monkeypatch) -> None:
    from drivefusion.gui import app, consent

    monkeypatch.delenv("DRIVEFUSION_ACCEPT_DISCLAIMER", raising=False)
    monkeypatch.setattr(consent, "_ask", lambda parent: False)
    monkeypatch.setattr(qt_app, "exec", lambda: pytest.fail("the window was opened"))
    assert app.main([], catalog=populated) == 0


# -- drive metadata (ROADMAP M5.2) ------------------------------------------------

def _with_a_drive(window) -> int:
    catalog = window.session.catalog()
    drive = catalog.upsert_drive(serial="EDIT-1", manufacturer="Seagate", model="ST4000DM004")
    catalog.assign_volume_drive(catalog.unverified_volumes()[0]["id"], drive)
    window.drives.refresh()
    window.drives.tree.setCurrentItem(window.drives.tree.topLevelItem(0))
    return drive


def test_the_dashboard_shows_expected_loss_with_its_confidence(window) -> None:
    window.dashboard.refresh()
    value = window.dashboard.tiles["expected_loss"].value.text()
    assert value.endswith("(unknown)") or value.endswith("(estimated)")
    assert "No SMART readings" in window.dashboard.detail.text()


def test_the_dashboard_shows_coverage(window) -> None:
    window.dashboard.refresh()
    assert window.dashboard.tiles["coverage"].value.text().startswith("0% · unfiled")


def test_each_drive_shows_its_estimated_failure_rate(window) -> None:
    from drivefusion.gui.screens.drives import COLUMNS

    _with_a_drive(window)
    item = window.drives.tree.topLevelItem(0)
    column = COLUMNS.index("Failure rate")
    assert item.text(column) == "1.5%/yr (unknown)"     # no media, model or purchase date
    assert "no SMART reading" in item.toolTip(column)


def test_the_edit_button_needs_a_drive(window) -> None:
    window.drives.tree.setCurrentItem(window.drives.tree.topLevelItem(0))
    assert not window.drives.edit_button.isEnabled(), "an unverified volume has no drive"
    _with_a_drive(window)
    assert window.drives.edit_button.isEnabled()


def test_the_edit_dialog_round_trips_and_is_audited(window) -> None:
    from drivefusion.core.store import audit
    from drivefusion.gui.screens.drives import COLUMNS, DriveEditDialog

    drive = _with_a_drive(window)
    catalog = window.session.catalog()
    dialog = DriveEditDialog(catalog.drive(drive))
    assert dialog.changes() == {}, "an untouched form changes nothing"
    dialog.set_value("purchase-date", "2021-03-14")
    dialog.set_value("role", "archive")
    changed = []
    window.drives.catalogChanged.connect(lambda: changed.append(True))
    assert window.drives.save_drive(drive, dialog.changes())

    item = window.drives.tree.topLevelItem(0)
    assert item.text(COLUMNS.index("Purchased")) == "2021-03-14"
    assert item.text(COLUMNS.index("Role")) == "archive"
    assert changed and audit.entries(catalog.conn, limit=1)[0]["action"] == "drive.edit"

    again = DriveEditDialog(catalog.drive(drive))
    again.set_value("role", "")
    assert again.changes() == {"role": None}, "blanking a value clears it"


def test_a_bad_value_in_the_dialog_is_refused_and_nothing_is_saved(window, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    drive = _with_a_drive(window)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    assert not window.drives.save_drive(drive, {"purchase-date": "14/03/2021", "role": "archive"})
    assert "not a date" in warnings[0]
    assert window.session.catalog().drive(drive)["role"] is None


# -- settings: failure-rate data (ROADMAP M5.4) ---------------------------------------

def test_settings_imports_a_drive_stats_file_as_a_task(window, tmp_path: Path) -> None:
    from drivefusion.gui.workers import Cancellation

    stats = tmp_path / "data_Q3_2025.csv"
    rows = ["date,serial_number,model,capacity_bytes,failure"]
    rows += [f"2025-07-01,S{i},ST4000DM000,4000787030016,{int(i < 2)}" for i in range(40_000)]
    stats.write_text("\n".join(rows) + "\n")
    screen = window.settings
    names = [screen.tree.topLevelItem(i).text(0) for i in range(screen.tree.topLevelItemCount())]
    assert "provisional-1" in names and not screen.forget_button.isEnabled()

    emitted = []
    screen.importRequested.disconnect()
    screen.importRequested.connect(emitted.append)
    screen.start_import([stats])
    [body] = emitted
    reports = []
    with Catalog(window.session.path) as catalog:
        assert body(catalog, reports.append, Cancellation()) == "backblaze-2025Q3"
    assert reports[0].message == "Reading data_Q3_2025.csv"

    screen.refresh()
    top = screen.tree.topLevelItem(0)
    assert top.text(0) == "backblaze-2025Q3" and top.text(2).startswith("imported")
    screen.tree.setCurrentItem(top)
    assert screen.forget_button.isEnabled()


def test_the_tools_menu_opens_settings(window) -> None:
    [menu] = [a for a in window.menuBar().actions() if "Tools" in a.text()]
    [action] = menu.menu().actions()
    action.trigger()
    assert window.stack.currentWidget() is window.settings


# -- drive health (ROADMAP M5.3) ---------------------------------------------------

def test_health_is_shelved_in_the_gui(window) -> None:
    from drivefusion.gui.screens.drives import COLUMNS

    assert window.drives.health_button.isHidden()
    assert window.drives.tree.isColumnHidden(COLUMNS.index("Health"))


@pytest.fixture
def health_gui(window, monkeypatch):
    """One catalogued drive; smartctl 'bundled'; dialogs answered by the test."""
    from drivefusion.core.enum.helper import client
    from drivefusion.core.health import smart
    from drivefusion.gui.screens import drives as screen

    drive = _with_a_drive(window)
    fixture = (Path(__file__).parent / "fixtures" / "smart" / "ata_hdd_healthy.json").read_text()
    state = {"admin": False, "restart": False, "confirm": False, "asked": [], "restarted": [],
             "runs": [], "emitted": []}
    monkeypatch.setattr(client, "is_elevated", lambda: state["admin"])
    monkeypatch.setattr(screen, "_ask_restart",
                        lambda parent: state["asked"].append("restart") or state["restart"])
    monkeypatch.setattr(screen, "_confirm_read",
                        lambda parent, n: state["asked"].append(("confirm", n)) or state["confirm"])
    monkeypatch.setattr(smart, "bundled_smartctl", lambda: Path("smartctl.exe"))
    monkeypatch.setattr(smart, "device_for_drive", lambda c, d: ("/dev/pd1", ""))
    monkeypatch.setattr(smart, "run", lambda *a, **k: state["runs"].append(a) or fixture)
    # The test runs the task body itself, so the window must not start it too.
    window.drives.healthRequested.disconnect()
    window.drives.healthRequested.connect(state["emitted"].append)
    state["drive"] = drive
    return state


def test_without_admin_rights_it_warns_and_asks_to_restart(window, health_gui, monkeypatch) -> None:
    from drivefusion.gui import elevation

    monkeypatch.setattr(elevation, "restart_elevated",
                        lambda *a: health_gui["restarted"].append(True))
    monkeypatch.setattr("PySide6.QtWidgets.QApplication.quit", lambda: None)

    window.drives.read_health()                     # "Not now"
    assert health_gui["asked"] == ["restart"] and health_gui["restarted"] == []
    assert health_gui["emitted"] == [] and health_gui["runs"] == []

    health_gui["restart"] = True                    # "Restart as administrator"
    window.drives.read_health()
    assert health_gui["restarted"] == [True] and health_gui["runs"] == []


def test_with_admin_rights_it_confirms_then_reads(window, health_gui) -> None:
    from drivefusion.core.health import smart
    from drivefusion.gui.screens.drives import COLUMNS
    from drivefusion.gui.workers import Cancellation

    health_gui["admin"] = True
    window.drives.read_health()                     # declined
    assert health_gui["asked"] == [("confirm", 1)] and health_gui["emitted"] == []

    health_gui["confirm"] = True
    window.drives.read_health()
    [body] = health_gui["emitted"]
    with Catalog(window.session.path) as catalog:
        body(catalog, lambda _p: None, Cancellation())
    assert len(health_gui["runs"]) == 1
    assert smart.verdict(smart.latest(window.session.catalog(), health_gui["drive"])) == "OK"
    window.drives.refresh()
    assert window.drives.tree.topLevelItem(0).text(COLUMNS.index("Health")) == "OK"


def test_restart_from_source_explains_instead(monkeypatch) -> None:
    from drivefusion.gui import elevation

    monkeypatch.delattr("sys.frozen", raising=False)
    with pytest.raises(elevation.ElevationError, match="Run as administrator"):
        elevation.restart_elevated()


# -- Collections, and Duplicates & Versions (ROADMAP M6.9) ------------------------------

def _queue_one(window) -> tuple[int, int]:
    from drivefusion.core.store import curation

    catalog = window.session.catalog()
    sub = catalog.conn.execute("SELECT id FROM dir WHERE name = 'sub'").fetchone()["id"]
    cid = curation.propose(catalog, sub, 0.9, {
        "path": "/media/sub", "why": ["one file type"], "files": 1, "bytes": 300,
        "date_span": ["2021-03", "2021-03"], "types": [["bin", 1]], "samples": ["c.bin"]})
    window.collections.refresh()
    window.collections.queue.setCurrentItem(window.collections.queue.topLevelItem(0))
    return cid, sub


def test_the_collections_screen_shows_coverage_and_the_queue_with_evidence(window) -> None:
    screen = window.collections
    assert screen.coverage.text().startswith("<b>Coverage: 0.0%</b>")
    assert screen.queue.topLevelItemCount() == 0 and not screen.accept_button.isEnabled()
    _queue_one(window)
    item = screen.queue.topLevelItem(0)
    assert (item.text(0), item.text(1), item.text(5)) == ("/media/sub", "0.90", "one file type")
    assert "c.bin" in screen.evidence.text() and screen.accept_button.isEnabled()


def test_accepting_from_the_queue_makes_a_collection_and_describing_it_is_audited(
        window, monkeypatch) -> None:
    from drivefusion.core.store import audit, curation
    from drivefusion.gui.screens import collections as screen_mod

    _queue_one(window)

    class Accept:
        def __init__(self, title, existing, parent=None):
            self.title = title

        def exec(self):
            return screen_mod.QDialog.Accepted

        def values(self):
            return "Sub data", None

    monkeypatch.setattr(screen_mod, "AcceptDialog", Accept)
    window.collections.accept_selected()
    catalog = window.session.catalog()
    [row] = curation.collections(catalog)
    assert row["title"] == "Sub data" and window.collections.queue.topLevelItemCount() == 0
    coll = window.collections.collections.topLevelItem(0)
    assert coll.text(0) == "Sub data" and coll.text(3) == "1/7"

    class Describe:
        def __init__(self, row, parent=None):
            pass

        def exec(self):
            return screen_mod.QDialog.Accepted

        def changes(self):
            return {"license": "CC-BY-4.0", "creators_json": '["A. Smith"]'}

    monkeypatch.setattr(screen_mod, "DescribeDialog", Describe)
    window.collections.collections.setCurrentItem(coll)
    window.collections.edit_selected()
    assert curation.collection(catalog, row["id"])["license"] == "CC-BY-4.0"
    assert audit.entries(catalog.conn, limit=1)[0]["action"] == "collection.edit"
    assert window.collections.collections.topLevelItem(0).text(3) == "3/7"


def test_reject_and_find_proposals_from_the_screen(window) -> None:
    from drivefusion.core.store import curation
    from drivefusion.gui.workers import Cancellation

    cid, _ = _queue_one(window)
    window.collections.reject_selected()
    assert curation.candidate(window.session.catalog(), cid)["status"] == "rejected"
    emitted = []
    window.collections.proposeRequested.disconnect()
    window.collections.proposeRequested.connect(emitted.append)
    window.collections.request_proposals()
    [body] = emitted
    with Catalog(window.session.path) as catalog:
        assert isinstance(body(catalog, lambda _p: None, Cancellation()), int)


def test_the_scorecard_dialog_lists_every_criterion(window) -> None:
    from drivefusion.core.fair import rubrics
    from drivefusion.core.store import curation
    from drivefusion.gui.screens.collections import ScorecardDialog

    catalog = window.session.catalog()
    sub = catalog.conn.execute("SELECT id FROM dir WHERE name = 'sub'").fetchone()["id"]
    coll = curation.create_collection(catalog, "Sub", dir_ids=[sub])
    dialog = ScorecardDialog(catalog, coll)
    for rubric_id, tree in dialog.trees.items():
        assert tree.topLevelItemCount() == len(rubrics.current(rubric_id).criteria)
    assert tree.topLevelItem(0).text(1).startswith(("✓", "~", "✗", "·", "?"))


def test_the_duplicates_screen_reports_duplicates_and_version_sets(window) -> None:
    from drivefusion.core.analysis import duplicate_groups
    from drivefusion.core.identity import hash_pass
    from drivefusion.core.store import curation
    from drivefusion.core.store.curation import VersionMember

    catalog = window.session.catalog()
    hash_pass(catalog)
    files = [r["id"] for r in catalog.conn.execute("SELECT id FROM file ORDER BY id")]
    curation.replace_version_sets(catalog, [{
        "label": "a.bin", "confidence": 0.9, "rationale": "most recently modified (b.bin)",
        "members": [VersionMember(files[0]), VersionMember(files[1], preferred=True)]}],
        method="name-stem")
    screen = window.duplicates
    screen.refresh()
    assert screen.duplicates.topLevelItemCount() == len(duplicate_groups(catalog))
    [found] = [screen.versions.topLevelItem(i) for i in range(screen.versions.topLevelItemCount())]
    assert found.text(0) == "a.bin" and found.childCount() == 2
    starred = [found.child(i).text(0) for i in range(2) if found.child(i).text(0).startswith("★")]
    assert len(starred) == 1 and "most recently modified" in found.toolTip(0)
    assert "never deletes" in screen.findChildren(type(screen.versions_note))[0].text()


# -- Plan (ROADMAP M7.7) -------------------------------------------------------------

def test_the_plan_screen_shows_the_plan_by_drive(window) -> None:
    from drivefusion.core.planner import document
    from tests.test_planner_units import Fleet

    catalog = window.session.catalog()
    f = Fleet(catalog)
    f.drive("A", purchase_date="2019-01-01", nickname="ARCHIVE-1")
    f.drive("B", purchase_date="2024-01-01", nickname="ARCHIVE-2")
    f.file("A", f.mkdir("A", "Photos"), 5 << 20)
    screen = window.plan
    screen.refresh()
    assert "No plan yet" in screen.requires.text()

    document.save(catalog, document.make(catalog, use_cpsat=False))
    screen.refresh()
    assert screen.requires.text().startswith("Requires: connect ARCHIVE-2")
    groups = {screen.steps.topLevelItem(i).text(0): screen.steps.topLevelItem(i)
              for i in range(screen.steps.topLevelItemCount())}
    archive = groups["ARCHIVE-2"]
    kinds = [archive.child(i).data(0, 0x0100) for i in range(archive.childCount())]
    assert "copy" in kinds and "verify" in kinds
    copy = next(archive.child(i) for i in range(archive.childCount())
                if archive.child(i).data(0, 0x0100) == "copy")
    assert copy.text(2).startswith("−") and copy.text(3)
    assert "deletes nothing" in screen.findChildren(type(screen.requires))[0].text()
