"""Scan scope: what gets catalogued, and what a rescan will cost.

§11 screen 1. The column that earns its place is **rescan cost**: half the target
fleet is exFAT, which has no change journal, so every rescan there is a full
re-walk. Averaging that into one number would make the fast drives look slow and
the slow ones look fine, so it is shown per volume.

Removing a root removes it *from the scan scope*. It deletes nothing, on disk or
in the catalog, and the button says so — this is one of the places §11 means by
"the read-only guarantee is stated wherever a user would expect a destructive
action".
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core import discovery
from drivefusion.core.scan import preview_root, scan_root
from drivefusion.core.scope import ExclusionSet, ScopeError, validate_new_root
from drivefusion.gui.format import describe_age, human_bytes
from drivefusion.gui.workers import Progress

COLUMNS = ("Root", "Volume", "Filesystem", "Rescan", "Last scanned", "Excludes")


class ScopeScreen(QWidget):
    """Add, inspect, and stop scanning roots."""

    #: Carries a task body up to the window, which owns the progress bar.
    scanRequested = Signal(object)
    catalogChanged = Signal()

    def __init__(self, session, runner, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner

        layout = QVBoxLayout(self)
        heading = QLabel(
            "<b>Scan scope</b> — only what is listed here is ever read. "
            "Drive Fusion never writes to these locations."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.tree, 1)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add folder…")
        self.preview_button = QPushButton("Preview (writes nothing)")
        self.scan_button = QPushButton("Scan")
        # "Stop scanning", never "Delete": the catalogued history is kept.
        self.forget_button = QPushButton("Stop scanning this root")
        for button in (
            self.add_button,
            self.preview_button,
            self.scan_button,
            self.forget_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.add_button.clicked.connect(self.add_root)
        self.preview_button.clicked.connect(self.preview_selected)
        self.scan_button.clicked.connect(self.scan_selected)
        self.forget_button.clicked.connect(self.forget_selected)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        catalog = self.session.catalog()
        volumes = {row["id"]: row for row in catalog.volumes()}
        self.tree.clear()

        roots = catalog.scope_roots()
        for root in roots:
            volume = volumes.get(root.volume_id)
            item = QTreeWidgetItem(
                [
                    root.path,
                    (volume["label"] or volume["volume_guid"]) if volume else "?",
                    (volume["fs_type"] or "?") if volume else "?",
                    (volume["rescan_cost"] or "?") if volume else "?",
                    describe_age(volume["last_seen_at"] if volume else None),
                    ", ".join(root.excludes) or "—",
                ]
            )
            item.setData(0, Qt.UserRole, root.id)
            if volume and volume["rescan_cost"] == "full-walk":
                item.setToolTip(
                    3,
                    "This filesystem has no change journal, so every rescan "
                    "re-walks the whole root.",
                )
            self.tree.addTopLevelItem(item)

        if roots:
            self.note.setText(f"{len(roots)} root(s) in scope.")
        else:
            self.note.setText(
                "No roots yet. Add a folder to catalog it — nothing is scanned "
                "until you do."
            )

    def selected_root_id(self) -> int | None:
        item = self.tree.currentItem()
        return None if item is None else int(item.data(0, Qt.UserRole))

    # -- actions ------------------------------------------------------------

    def add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to catalog")
        if path:
            self.add_path(path)

    def add_path(self, path: str) -> bool:
        """Register a root. Separated from the file dialog so it can be tested."""
        catalog = self.session.catalog()
        try:
            root_path = validate_new_root(path, [r.path for r in catalog.scope_roots()])
            volume = discovery.identify_volume(root_path)
        except (ScopeError, discovery.DiscoveryError) as exc:
            QMessageBox.warning(self, "Cannot add this folder", str(exc))
            return False

        drive_id = None
        if volume.drive and volume.drive.serial:
            drive_id = catalog.upsert_drive(
                serial=volume.drive.serial,
                model=volume.drive.model,
                manufacturer=volume.drive.manufacturer,
                bus=volume.drive.bus,
                media=volume.drive.media,
            )
        fields = volume.as_volume_fields()
        if drive_id is not None:
            fields["drive_id"] = drive_id
        volume_id = catalog.upsert_volume(volume_guid=volume.volume_guid, **fields)
        catalog.add_scope_root(volume_id, root_path)

        self.refresh()
        self.catalogChanged.emit()
        return True

    def preview_selected(self) -> None:
        root_id = self.selected_root_id()
        if root_id is None:
            QMessageBox.information(self, "Select a root", "Choose a root to preview.")
            return
        root = self.session.catalog().scope_root(root_id)
        if root is None:
            return

        path, excludes = root.path, ExclusionSet.for_root(root.excludes)

        def body(_catalog, progress, cancel):
            progress(Progress(f"Previewing {path}…"))
            cancel.raised()
            return preview_root(path, excludes)

        task = self.runner.start(body, name="preview")
        task.signals.finished.connect(self._show_preview)

    def _show_preview(self, counters) -> None:
        QMessageBox.information(
            self,
            "Preview",
            f"{counters.dirs:,} directories\n"
            f"{counters.files:,} files\n"
            f"{human_bytes(counters.bytes)}\n\n"
            "Nothing was written, and nothing was changed.",
        )

    def scan_body(self, root_id: int | None = None):
        """Build the task body for scanning one root, or all of them.

        Returned rather than started so the window can own the progress bar and
        the Cancel button. ``None`` when there is nothing in scope.
        """
        catalog = self.session.catalog()
        roots = catalog.scope_roots(enabled_only=True)
        if root_id is not None:
            roots = [r for r in roots if r.id == root_id]
        if not roots:
            return None

        plan = [(r.id, r.path, r.volume_id, r.excludes) for r in roots]

        def body(catalog, progress, cancel):
            files = 0
            for index, (rid, path, volume_id, excludes) in enumerate(plan, start=1):
                cancel.raised()
                progress(Progress(f"Scanning {path}", index - 1, len(plan)))

                def report(counters, _path=path, _index=index):
                    progress(
                        Progress(
                            f"{_path}: {counters.files:,} files", _index - 1, len(plan)
                        )
                    )
                    # Checked per directory rather than per root, so Cancel takes
                    # effect during a scan and not only between roots.
                    cancel.raised()

                result = scan_root(
                    catalog,
                    root_path=path,
                    root_id=rid,
                    volume_id=volume_id,
                    excludes=ExclusionSet.for_root(excludes),
                    progress=report,
                )
                files += result["counters"].files
            progress(Progress("Scan complete", len(plan), len(plan)))
            return files

        return body

    def scan_selected(self) -> None:
        body = self.scan_body(self.selected_root_id())
        if body is None:
            QMessageBox.information(self, "Nothing to scan", "Add a root first.")
            return
        self.scanRequested.emit(body)

    def forget_selected(self) -> None:
        root_id = self.selected_root_id()
        if root_id is None:
            return
        catalog = self.session.catalog()
        root = catalog.scope_root(root_id)
        if root is None:
            return

        answer = QMessageBox.question(
            self,
            "Stop scanning this root?",
            f"Drive Fusion will stop scanning:\n\n{root.path}\n\n"
            "Nothing on the drive is touched, and what has already been "
            "catalogued is kept so existing reports stay accurate.",
        )
        if answer == QMessageBox.Yes:
            catalog.remove_scope_root(root_id)
            self.refresh()
            self.catalogChanged.emit()
