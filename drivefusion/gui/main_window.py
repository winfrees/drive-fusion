"""The window: navigation, a progress bar, and a permanent honesty notice.

Four screens are wired up at M4 — Scope, Dashboard, Drives, Catalog. The four
that are not (Collections, Duplicates, Plan, Exports) appear as disabled entries
naming the milestone that lands each, rather than as buttons that do nothing: a
dead control is a bug report waiting to be filed.

The status bar always carries "Read-only — nothing on your drives is ever
modified", because §11 requires the guarantee be stated wherever a user would
expect a destructive action, and the answer to "where is the delete button" is
best given before it is asked.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from drivefusion import __version__
from drivefusion.gui import elevation
from drivefusion.gui.screens.browser import BrowserScreen
from drivefusion.gui.screens.dashboard import DashboardScreen
from drivefusion.gui.screens.drives import DrivesScreen
from drivefusion.gui.screens.scope import ScopeScreen
from drivefusion.gui.session import Session
from drivefusion.gui.workers import TaskRunner

READ_ONLY_NOTICE = "Read-only — nothing on your drives is ever modified"

#: Screens the plan defines but this milestone does not ship, and their milestone.
PLANNED = (
    ("Collections", "M6"),
    ("Duplicates & Versions", "M7"),
    ("Plan", "M7"),
    ("Exports", "M8"),
)


class MainWindow(QMainWindow):
    def __init__(self, catalog_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Drive Fusion {__version__}")
        self.resize(1180, 720)

        self.session = Session(catalog_path)
        self.runner = TaskRunner(self.session)
        self.elevation_state = elevation.assess()

        central = QWidget()
        root_layout = QHBoxLayout(central)

        self.nav = QListWidget()
        self.nav.setMaximumWidth(220)
        root_layout.addWidget(self.nav)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        progress_row.addWidget(self.progress_label, 1)
        progress_row.addWidget(self.progress)
        progress_row.addWidget(self.cancel_button)
        right_layout.addLayout(progress_row)
        root_layout.addWidget(right, 1)
        self.setCentralWidget(central)

        self.scope = ScopeScreen(self.session, self.runner)
        self.dashboard = DashboardScreen(self.session, self.runner)
        self.drives = DrivesScreen(self.session)
        self.browser = BrowserScreen(self.session, self.runner)

        for name, screen in (
            ("Scope", self.scope),
            ("Dashboard", self.dashboard),
            ("Drives", self.drives),
            ("Catalog", self.browser),
        ):
            self.nav.addItem(QListWidgetItem(name))
            self.stack.addWidget(screen)

        for name, milestone in PLANNED:
            item = QListWidgetItem(f"{name}  ({milestone})")
            item.setFlags(Qt.NoItemFlags)     # visible, plainly not yet available
            item.setToolTip(f"Arrives in {milestone}. See docs/PLAN.md §15.")
            self.nav.addItem(item)

        self.nav.currentRowChanged.connect(self._navigate)
        self.nav.setCurrentRow(0)

        self.setStatusBar(QStatusBar())
        self.notice = QLabel(READ_ONLY_NOTICE)
        self.statusBar().addPermanentWidget(self.notice)
        self.statusBar().showMessage(elevation.status_line(self.elevation_state))

        self.scope.scanRequested.connect(lambda body: self.run_task(body, "Scanning"))
        self.scope.catalogChanged.connect(self.refresh_all)
        self.dashboard.hashRequested.connect(lambda body: self.run_task(body, "Hashing"))
        self.cancel_button.clicked.connect(self._cancel_current)
        self._current_task = None

    # -- navigation ---------------------------------------------------------

    def _navigate(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)

    def refresh_all(self) -> None:
        self.scope.refresh()
        self.dashboard.refresh()
        self.drives.refresh()
        self.browser.refresh()

    # -- background work ----------------------------------------------------

    def run_task(self, body, label: str):
        """Run one long operation, with progress and a working Cancel."""
        if self._current_task is not None:
            QMessageBox.information(
                self,
                "Already busy",
                "One long operation runs at a time so the drives are read "
                "sequentially rather than thrashed.",
            )
            return None

        task = self.runner.start(body, name=label.lower())
        self._current_task = task
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self.progress.setRange(0, 0)          # indeterminate until a total exists
        self.progress_label.setText(f"{label}…")

        task.signals.progress.connect(self._on_progress)
        task.signals.finished.connect(lambda _r: self._task_done(f"{label} complete"))
        task.signals.cancelled.connect(
            lambda: self._task_done(
                f"{label} cancelled — the catalog is unchanged from before it started"
            )
        )
        task.signals.failed.connect(self._task_failed)
        return task

    def _on_progress(self, progress) -> None:
        self.progress_label.setText(progress.message)
        if progress.total:
            self.progress.setRange(0, progress.total)
            self.progress.setValue(progress.done)
        else:
            self.progress.setRange(0, 0)

    def _task_done(self, message: str) -> None:
        self._current_task = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.progress_label.setText("")
        self.statusBar().showMessage(message, 8000)
        self.refresh_all()

    def _task_failed(self, message: str, detail: str) -> None:
        self._current_task = None
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.progress_label.setText("")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Operation failed")
        box.setText(message)
        box.setDetailedText(detail)
        box.setInformativeText(
            "Nothing on your drives was modified; the catalog keeps whatever had "
            "already been recorded."
        )
        box.exec()

    def _cancel_current(self) -> None:
        if self._current_task is not None:
            self._current_task.cancel()
            self.progress_label.setText("Cancelling…")

    # -- lifecycle ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self.runner.cancel_all()
        self.runner.wait(5_000)
        self.session.close()
        super().closeEvent(event)
