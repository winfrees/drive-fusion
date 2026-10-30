"""Settings: the data Drive Fusion scores against (ROADMAP M5.4, D13).

For now that is the failure-rate tables behind every drive's AFR. The build
ships provisional class rates and, when the build pinned one, a Backblaze
Drive Stats snapshot. Here the user can import a newer snapshot from a ZIP
they downloaded themselves: Drive Fusion makes no network connections
(PLAN §4), so it never fetches one. The ZIP is only read; the table is kept
in the catalog, and the import is audited.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.health import afr, backblaze
from drivefusion.gui.workers import Progress

COLUMNS = ("Table", "Basis", "Where from", "Models", "Class rates")

INTRO = (
    "Every drive's estimated failure rate starts from a base rate in one of these "
    "tables, best first: fleet statistics, then manufacturer ratings, then "
    "provisional placeholders. To use newer fleet statistics, download a quarter of "
    "<a href=\"{url}\">Backblaze Drive Stats</a> and import the ZIP here. Drive Fusion "
    "only reads it; it never connects to the internet itself."
)


class SettingsScreen(QWidget):
    #: An import task body for the window to run (it owns progress and Cancel).
    importRequested = Signal(object)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Settings</b>"))

        group = QGroupBox("Failure-rate data")
        inner = QVBoxLayout(group)
        intro = QLabel(INTRO.format(url=backblaze.SOURCE_URL))
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        inner.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        inner.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.import_button = QPushButton("Import Drive Stats…")
        self.import_button.setToolTip(
            "Read a Backblaze Drive Stats ZIP (or its CSVs) you downloaded, and use "
            "its per-model failure rates. Large quarters take a few minutes."
        )
        self.forget_button = QPushButton("Forget")
        self.forget_button.setToolTip("Stop using the selected imported table.")
        buttons.addWidget(self.import_button)
        buttons.addStretch(1)
        buttons.addWidget(self.forget_button)
        inner.addLayout(buttons)
        layout.addWidget(group, 1)

        self.import_button.clicked.connect(self.choose_import)
        self.forget_button.clicked.connect(self.forget_selected)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.refresh()

    def refresh(self) -> None:
        catalog = self.session.catalog()
        imported = {row["name"]: row["imported_at"] for row in catalog.afr_tables()}
        self.tree.clear()
        for table in afr.tables(catalog):
            where = (f"imported {imported[table.name][:10]}" if table.name in imported
                     else "shipped with Drive Fusion")
            item = QTreeWidgetItem([
                table.name, table.basis, where, f"{len(table.models):,}",
                ", ".join(f"{k} {v:.2%}" for k, v in table.classes.items()),
            ])
            item.setData(0, Qt.UserRole, table.name if table.name in imported else None)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    def _selected_import(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _update_buttons(self) -> None:
        self.forget_button.setEnabled(self._selected_import() is not None)

    # -- importing ------------------------------------------------------------

    def choose_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Backblaze Drive Stats", "",
            "Drive Stats (*.zip *.csv);;All files (*)")
        if files:
            self.start_import([Path(f) for f in files])

    def start_import(self, paths: list[Path], version: str | None = None) -> None:
        version = version or backblaze.version_from_name(paths[0].name)
        if not version:
            version, ok = QInputDialog.getText(
                self, "Which quarter?",
                "The file name doesn't say which quarter this is. Enter it, e.g. 2025Q2:")
            if not ok or not version.strip():
                return
            version = version.strip()

        def body(catalog, progress, cancel):
            seen = []

            def on_file(name):
                cancel.raised()
                seen.append(name)
                progress(Progress(f"Reading {name}", len(seen)))

            table = backblaze.import_files(paths, version=version,
                                           retrieved=dt.date.today().isoformat(),
                                           on_file=on_file)
            cancel.raised()
            return catalog.import_afr_table(table)

        self.importRequested.emit(body)

    def forget_selected(self) -> None:
        name = self._selected_import()
        if name is None:
            return
        answer = QMessageBox.question(
            self, "Forget table",
            f"Stop using {name}? Failure rates go back to the shipped tables. "
            "You can import it again later.")
        if answer == QMessageBox.Yes:
            self.session.catalog().forget_afr_table(name)
            self.refresh()
