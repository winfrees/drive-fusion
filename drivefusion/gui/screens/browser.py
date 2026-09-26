"""Catalog browser: tree, virtualized table, and an asynchronous count.

§11 screen 4. Three behaviours are load-bearing:

* The table loads a page at a time as the view scrolls (``FileTableModel``).
* Subtree sizes come from ``dir_rollup``, so selecting a folder is instant even
  when the folder holds a million files.
* The exact file count is computed on a worker and shown as "counting…" until it
  lands. Counting on the UI thread would stall the window on exactly the folders
  a user most wants to look at.

The stale-count problem is handled explicitly: a count that arrives after the
user has clicked a different folder is *dropped*, not applied. Applying it would
label one folder with another's total, which looks like data loss and is
impossible to reproduce on demand.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from drivefusion.core.analysis.browse import count_files_in_dir, dir_summary
from drivefusion.gui.format import human_bytes
from drivefusion.gui.models import DirTreeModel, FileTableModel


class BrowserScreen(QWidget):
    """A tree of directories beside a paged table of their files."""

    def __init__(self, session, runner, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner
        #: The directory whose count we are waiting for; guards against a stale
        #: result being applied to whatever is selected by the time it arrives.
        self._counting_for: int | None = None

        catalog = session.catalog()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Catalog</b>"))

        splitter = QSplitter(Qt.Horizontal)

        self.tree_model = DirTreeModel.over(catalog, self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.summary = QLabel("Select a folder.")
        self.summary.setWordWrap(True)
        right_layout.addWidget(self.summary)

        self.table_model = FileTableModel.over(catalog, self)
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        # Uniform row heights let Qt fetch only what is on screen; without it the
        # view measures every row and the paging is pointless.
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        right_layout.addWidget(self.table, 1)

        self.status = QLabel()
        right_layout.addWidget(self.status)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.tree.selectionModel().currentChanged.connect(self._directory_chosen)
        self.table_model.pageLoaded.connect(lambda _n: self._update_status())

    # -- selection ----------------------------------------------------------

    def _directory_chosen(self, current, _previous) -> None:
        dir_id = self.tree_model.dir_id_at(current)
        self.select_directory(dir_id)

    def select_directory(self, dir_id: int | None) -> None:
        """Show a directory. Separated from the signal so tests can drive it."""
        self.table_model.set_directory(dir_id)
        self._counting_for = dir_id
        self._show_summary(dir_id)
        self._update_status()
        if dir_id is not None:
            self._start_count(dir_id)

    def _show_summary(self, dir_id: int | None) -> None:
        if dir_id is None:
            self.summary.setText("Select a folder.")
            return
        info = dir_summary(self.session.catalog(), dir_id)
        if not info:
            self.summary.setText("Folder not found.")
            return
        self.summary.setText(
            f"<b>{info['name']}</b> — {human_bytes(info['bytes'])} here, "
            f"{human_bytes(info['subtree_bytes'])} including subfolders "
            f"({info['subtree_files']:,} files)"
        )

    # -- counting -----------------------------------------------------------

    def _start_count(self, dir_id: int) -> None:
        def body(catalog, _progress, cancel):
            cancel.raised()
            return (dir_id, count_files_in_dir(catalog, dir_id))

        task = self.runner.start(body, name="count")
        task.signals.finished.connect(self._count_arrived)

    def _count_arrived(self, result) -> None:
        dir_id, total = result
        if dir_id != self._counting_for:
            # The user moved on. Applying this would caption one folder with
            # another folder's total.
            return
        self.table_model.set_total(total)
        self._update_status()

    def _update_status(self) -> None:
        self.status.setText(self.table_model.status_text())

    def refresh(self) -> None:
        """Re-read after a scan. Keeps the selected folder if it still exists."""
        selected = self.table_model.directory
        self.tree_model.refresh()
        self.select_directory(selected)
