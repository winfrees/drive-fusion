"""Duplicates & Versions (PLAN §11 screen 6, ROADMAP M6.9).

Two tabs, both reports. **Duplicates**: content found at more than one path,
with how many drives hold it and the bytes duplicated within one drive.
**Versions**: near-duplicates grouped into version sets (``core/curate/
versions.py``), the members side by side with size and date, the proposed
preferred member marked and the reason given.

Nothing here removes, merges or moves anything: Drive Fusion never deletes
and never emits a command that would. Evidence for a person to act on.
"""

from __future__ import annotations

import datetime as dt
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.analysis import duplicate_groups, group_paths
from drivefusion.core.curate import versions
from drivefusion.core.store import curation
from drivefusion.gui.format import human_bytes
from drivefusion.gui.workers import Progress

DUPLICATE_COLUMNS = ("Size", "Paths", "Drives", "Duplicated on one drive", "Example")
VERSION_COLUMNS = ("Version set / member", "Size", "Modified", "Confidence")
#: Duplicate groups listed; the rest are counted.
SHOWN = 500


def _when(mtime_ns) -> str:
    if mtime_ns is None:
        return "—"
    return dt.datetime.fromtimestamp(mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")


class DuplicatesScreen(QWidget):
    #: A task body for the window to run (it owns progress and Cancel).
    versionsRequested = Signal(object)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Duplicates &amp; Versions</b> — a report. Drive Fusion never deletes and "
            "never emits a command that would; copies on separate drives are protection, "
            "not waste."))
        self.tabs = QTabWidget()

        self.duplicates = QTreeWidget()
        self.duplicates.setColumnCount(len(DUPLICATE_COLUMNS))
        self.duplicates.setHeaderLabels(list(DUPLICATE_COLUMNS))
        self.duplicates.header().setSectionResizeMode(4, QHeaderView.Stretch)
        self.duplicates_note = QLabel()
        dup_page = QWidget()
        dup_layout = QVBoxLayout(dup_page)
        dup_layout.addWidget(self.duplicates, 1)
        dup_layout.addWidget(self.duplicates_note)
        self.tabs.addTab(dup_page, "Duplicates")

        self.versions = QTreeWidget()
        self.versions.setColumnCount(len(VERSION_COLUMNS))
        self.versions.setHeaderLabels(list(VERSION_COLUMNS))
        self.versions.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.versions_note = QLabel()
        self.versions_note.setWordWrap(True)
        ver_page = QWidget()
        ver_layout = QVBoxLayout(ver_page)
        ver_layout.addWidget(self.versions, 1)
        ver_layout.addWidget(self.versions_note)
        row = QHBoxLayout()
        self.find_versions_button = QPushButton("Find version sets")
        self.find_versions_button.setToolTip(
            "Group near-duplicates in each folder and propose which to keep in view. "
            "Reads the catalog only.")
        row.addWidget(self.find_versions_button)
        row.addStretch(1)
        ver_layout.addLayout(row)
        self.tabs.addTab(ver_page, "Versions")
        layout.addWidget(self.tabs, 1)

        self.find_versions_button.clicked.connect(self.request_versions)
        self.refresh()

    def refresh(self) -> None:
        catalog = self.session.catalog()
        self.duplicates.clear()
        groups = duplicate_groups(catalog)
        for group in groups[:SHOWN]:
            paths = group_paths(catalog, group, limit=10)
            item = QTreeWidgetItem([
                human_bytes(group.size_bytes), f"{group.paths:,}", f"{group.drives}",
                human_bytes(group.reclaimable_bytes) if group.reclaimable_bytes else "—",
                paths[0] if paths else "",
            ])
            for path in paths:
                item.addChild(QTreeWidgetItem(["", "", "", "", path]))
            self.duplicates.addTopLevelItem(item)
        more = len(groups) - SHOWN
        self.duplicates_note.setText(
            f"{len(groups):,} content item(s) at more than one path"
            + (f"; the largest {SHOWN} are listed" if more > 0 else ""))

        self.versions.clear()
        sets = curation.version_sets(catalog)
        for found in sets:
            parent = QTreeWidgetItem([found["label"], "", "", f"{found['confidence']:.2f}"])
            parent.setToolTip(0, found["rationale"] or "")
            for member in curation.version_members(catalog, found["id"]):
                row = catalog.conn.execute(
                    "SELECT dir_id, name, size_bytes, mtime_ns FROM file WHERE id = ?",
                    (member["file_id"],)).fetchone()
                if row is None:
                    continue
                mark = "★ " if member["is_preferred"] else "   "
                child = QTreeWidgetItem([
                    f"{mark}{catalog.dir_path(row['dir_id'], os.sep)}{os.sep}{row['name']}",
                    human_bytes(row["size_bytes"]), _when(row["mtime_ns"]), ""])
                child.setData(0, Qt.UserRole, bool(member["is_preferred"]))
                parent.addChild(child)
            self.versions.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.versions_note.setText(
            f"{len(sets)} version set(s). ★ marks the proposed preferred member; hover a set "
            "for why. Versions in different folders, or renamed beyond their version "
            "markers, are not found." if sets else
            "No version sets yet: identify content (Dashboard), then Find version sets.")

    def request_versions(self) -> None:
        def body(catalog, progress, cancel):
            progress(Progress("Grouping near-duplicates into version sets"))
            return versions.refresh(catalog)
        self.versionsRequested.emit(body)
