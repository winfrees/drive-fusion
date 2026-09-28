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
    (root / "b.bin").write_bytes(b"b" * 200)
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

    assert window.stack.count() == 4
    assert window.nav.count() == 4 + len(PLANNED)

    for offset, (name, milestone) in enumerate(PLANNED):
        item = window.nav.item(4 + offset)
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
    assert "no identified physical drive" in window.drives.note.text()


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
