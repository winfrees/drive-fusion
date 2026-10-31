"""Exports (PLAN §11 screen 8, ROADMAP M8.6).

Choose an export folder once (D22: always a person's choice, then
remembered), then make any export: per-drive manifests, collection packages,
mappings with copy-only command previews, reports, or all of them. Each
lands in a new folder of its own; the exports made are listed with their
files and the standards versions they used, and "Open folder" shows one in
the system's file browser.

Exports are the only files Drive Fusion writes besides its catalog, and
never on a scanned drive (REVIEW F-03).
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.export import run
from drivefusion.core.store import exports
from drivefusion.gui.format import human_bytes
from drivefusion.gui.workers import Progress

COLUMNS = ("Made", "Kind", "Files", "Size", "Folder")
LABELS = {"manifests": "Drive manifests", "collections": "Collection packages",
          "mappings": "Mappings and copy commands", "reports": "Reports", "all": "Make all"}


class ExportsScreen(QWidget):
    #: A task body for the window to run (it owns progress and Cancel).
    exportRequested = Signal(object)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.root: str | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Exports</b> — written into a new folder each time, under a folder you choose, "
            "never on a scanned drive. Nothing on any drive is changed."))
        row = QHBoxLayout()
        self.root_label = QLabel()
        self.choose_button = QPushButton("Choose export folder…")
        row.addWidget(self.root_label, 1)
        row.addWidget(self.choose_button)
        layout.addLayout(row)

        kinds = QHBoxLayout()
        self.make_buttons = {}
        for kind in (*run.KINDS, "all"):
            button = QPushButton(LABELS[kind])
            button.clicked.connect(lambda _=False, k=kind: self.request_export(k))
            kinds.addWidget(button)
            self.make_buttons[kind] = button
        kinds.addStretch(1)
        layout.addLayout(kinds)

        self.list = QTreeWidget()
        self.list.setColumnCount(len(COLUMNS))
        self.list.setHeaderLabels(list(COLUMNS))
        self.list.header().setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.list, 1)
        bottom = QHBoxLayout()
        self.open_button = QPushButton("Open folder")
        self.open_button.setToolTip("Show the selected export in the file browser")
        bottom.addStretch(1)
        bottom.addWidget(self.open_button)
        layout.addLayout(bottom)

        self.choose_button.clicked.connect(self.choose_root)
        self.open_button.clicked.connect(self.open_selected)
        self.refresh()

    def refresh(self) -> None:
        catalog = self.session.catalog()
        self.root = self.root or run.last_root(catalog)
        self.root_label.setText(f"Export folder: {self.root}" if self.root else
                                "No export folder yet: choose one to make an export.")
        for button in self.make_buttons.values():
            button.setEnabled(self.root is not None)
        self.list.clear()
        for row in exports.exports(catalog):
            item = QTreeWidgetItem([row["created_at"][:16].replace("T", " "), row["kind"],
                                    f"{row['files']:,}", human_bytes(row["bytes"]),
                                    row["path"]])
            standards = json.loads(row["standards_json"] or "{}")
            item.setToolTip(1, json.dumps(standards, indent=1))
            item.setData(0, Qt.UserRole, row["path"])
            self.list.addTopLevelItem(item)

    def choose_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose an export folder",
                                                  self.root or "")
        if chosen:
            self.set_root(chosen)

    def set_root(self, path: str) -> None:
        self.root = path
        self.refresh()

    def request_export(self, kind: str) -> None:
        root = self.root

        def body(catalog, progress, cancel):
            progress(Progress(f"Writing {LABELS[kind].lower()} into {root}"))
            return run.make(catalog, [kind], root)
        self.exportRequested.emit(body)

    def open_selected(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(0, Qt.UserRole)))
