"""Drives: the inventory, and which of it is currently connected.

§11 screen 3. Two things here are worth more than the rest:

* **"Last scanned" is in words**, because the question a user actually has is "is
  this the drive I scanned in March?" — and a raw timestamp makes them do
  arithmetic to find out.
* **A volume with no identified physical drive gets its own row**, marked
  unverified. Not dropped, not folded into another: an unmapped disk that
  silently disappeared from the inventory would make the totals stop adding up
  to what was scanned. Copies on it count as at most one proven copy
  (docs/REVIEW.md F-05) until the user says which drive it is on, which the two
  buttons here record. A drive identified by its serial or partition table
  can't be overruled from here.

What the hardware can't say (purchase date, price, warranty, role, location,
disaster zone, a batch override) is edited here through "Edit drive…", and each
edit is audited (ROADMAP M5.2). SMART sparklines arrive with M5.3.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QInputDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.analysis import drive_inventory
from drivefusion.gui.format import describe_age, human_bytes, percent

COLUMNS = (
    "Drive",
    "Identity",
    "Volume",
    "Filesystem",
    "Capacity",
    "Free",
    "Catalogued",
    "Files",
    "Rescan",
    "Last scanned",
    "Purchased",
    "Role",
)

#: How each drive identity is shown.
IDENTITY_WORDS = {
    "serial": "serial number",
    "disk-layout": "partition table",
    "user": "confirmed by you",
}
UNVERIFIED = "unverified"


class DrivesScreen(QWidget):
    """What is in the fleet, and what has been read from each of it."""

    #: A drive was confirmed, which changes every copy count that used it.
    catalogChanged = Signal()

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "<b>Drives</b> — the fleet as the catalog knows it. "
                "Nothing here is ever written to."
            )
        )

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
        self.refresh_button = QPushButton("Refresh")
        self.own_drive_button = QPushButton("Its own drive")
        self.own_drive_button.setToolTip(
            "Record that the selected volume, whose drive is unverified, is on a "
            "physical drive of its own."
        )
        self.on_drive_button = QPushButton("On drive…")
        self.on_drive_button.setToolTip(
            "Record which known drive the selected volume, whose drive is "
            "unverified, is on (a second partition of it, say)."
        )
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.own_drive_button)
        buttons.addWidget(self.on_drive_button)
        self.edit_button = QPushButton("Edit drive…")
        self.edit_button.setToolTip(
            "Record what the hardware can't say: purchase date, price, warranty, "
            "role, location, disaster zone. Each edit is kept in the audit log."
        )
        buttons.addWidget(self.edit_button)
        layout.addLayout(buttons)
        self.edit_button.clicked.connect(self.edit_selected)
        self.refresh_button.clicked.connect(self.refresh)
        self.own_drive_button.clicked.connect(self.confirm_own_drive)
        self.on_drive_button.clicked.connect(self.choose_drive)
        self.tree.itemSelectionChanged.connect(self._update_buttons)

        self.refresh()

    def refresh(self) -> None:
        rows = drive_inventory(self.session.catalog())
        self.tree.clear()

        unmapped = 0
        for row in rows:
            if row["drive_id"] is None:
                unmapped += 1
                drive_name = "(drive not identified)"
            else:
                drive_name = (
                    row["nickname"]
                    or row["model"]
                    or row["serial"]
                    or f"drive {row['drive_id']}"
                )

            capacity = row["capacity_bytes"]
            item = QTreeWidgetItem(
                [
                    drive_name,
                    UNVERIFIED
                    if row["drive_id"] is None
                    else IDENTITY_WORDS.get(row["identity"], row["identity"] or "?"),
                    row["label"] or row["volume_guid"],
                    row["fs_type"] or "?",
                    human_bytes(capacity),
                    f"{human_bytes(row['free_bytes'])}"
                    + (
                        f" ({percent(row['free_bytes'], capacity)})"
                        if capacity and row["free_bytes"] is not None
                        else ""
                    ),
                    human_bytes(row["bytes"]),
                    f"{row['files']:,}",
                    row["rescan_cost"] or "?",
                    describe_age(row["last_scanned_at"]),
                    row["purchase_date"] or "—",
                    row["role"] or "—",
                ]
            )
            item.setData(0, Qt.UserRole,
                         (row["volume_id"], row["drive_id"] is None, row["drive_id"]))
            if row["drive_id"] is None:
                for column in (0, 1):
                    item.setToolTip(
                        column,
                        "Neither a serial number nor a partition table identifies "
                        "this volume's physical drive, so a copy on it adds at most "
                        "one proven copy: it could share a disk with another volume. "
                        "Say which drive it is on with the buttons below.",
                    )
            if row["rescan_cost"] == "full-walk":
                item.setToolTip(
                    8,
                    "No change journal on this filesystem: every rescan re-walks "
                    "the whole volume.",
                )
            self.tree.addTopLevelItem(item)

        self._update_buttons()
        if not rows:
            self.note.setText("No volumes yet. Add a root on the Scope screen.")
            return

        parts = [f"{len(rows)} volume(s)"]
        if unmapped:
            parts.append(
                f"{unmapped} with an unverified drive: copies on them count as "
                "proven only once each drive is confirmed"
            )
        self.note.setText(" · ".join(parts))

    # -- confirming a drive -------------------------------------------------

    def selected_unverified_volume(self) -> int | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        volume_id, unverified, _drive = item.data(0, Qt.UserRole)
        return volume_id if unverified else None

    def selected_drive(self) -> int | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.UserRole)[2]

    def _update_buttons(self) -> None:
        enabled = self.selected_unverified_volume() is not None
        self.own_drive_button.setEnabled(enabled)
        self.on_drive_button.setEnabled(enabled)
        self.edit_button.setEnabled(self.selected_drive() is not None)

    # -- editing what the hardware can't say (ROADMAP M5.2) -------------------

    def edit_selected(self) -> None:
        drive_id = self.selected_drive()
        if drive_id is None:
            return
        row = self.session.catalog().drive(drive_id)
        dialog = DriveEditDialog(row, self)
        while dialog.exec() == QDialog.Accepted:
            if self.save_drive(drive_id, dialog.changes()):
                return

    def save_drive(self, drive_id: int, changes: dict[str, str | None]) -> bool:
        """Separated from the dialog so it can be tested. True when saved."""
        from drivefusion.core.health.metadata import MetadataError

        try:
            applied = self.session.catalog().update_drive(drive_id, changes)
        except (MetadataError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot record this", str(exc))
            return False
        if applied:
            self.refresh()
            self.catalogChanged.emit()
        return True

    def confirm_own_drive(self) -> None:
        volume_id = self.selected_unverified_volume()
        if volume_id is None:
            return
        self._record(lambda catalog: catalog.confirm_volume_drive(volume_id))

    def choose_drive(self) -> None:
        volume_id = self.selected_unverified_volume()
        if volume_id is None:
            return
        drives = self._known_drives()
        if not drives:
            QMessageBox.information(
                self, "No known drives",
                "No identified drive is catalogued yet; use \"Its own drive\" instead.",
            )
            return
        names = list(drives)
        name, accepted = QInputDialog.getItem(
            self, "Which drive is this volume on?", "Drive:", names, 0, False
        )
        if accepted and name in drives:
            self.assign_to_drive(volume_id, drives[name])

    def assign_to_drive(self, volume_id: int, drive_id: int) -> None:
        """Separated from the dialog so it can be tested."""
        self._record(lambda catalog: catalog.assign_volume_drive(volume_id, drive_id))

    def _known_drives(self) -> dict[str, int]:
        drives: dict[str, int] = {}
        for row in self.session.catalog().conn.execute(
            "SELECT id, nickname, model, serial, layout_id FROM drive ORDER BY id"
        ):
            name = row["nickname"] or row["model"] or row["serial"] or row["layout_id"] or ""
            drives[f"drive {row['id']} {name}".strip()] = int(row["id"])
        return drives

    def _record(self, change) -> None:
        try:
            change(self.session.catalog())
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot record this", str(exc))
            return
        self.refresh()
        self.catalogChanged.emit()


class DriveEditDialog(QDialog):
    """A form over ``core.health.metadata.FIELDS``: blank clears a value."""

    def __init__(self, row, parent=None) -> None:
        super().__init__(parent)
        from drivefusion.core.health import metadata

        self.setWindowTitle(f"Drive {row['id']}: {row['nickname'] or row['model'] or ''}".strip())
        self._before: dict[str, str] = {}
        self._inputs: dict[str, QLineEdit | QComboBox] = {}
        form = QFormLayout(self)
        for field in metadata.FIELDS:
            current = row[field.column]
            shown = "" if current is None else metadata.format_value(field.column, current)
            if field.name == "role":
                widget = QComboBox()
                widget.addItems(["", *metadata.ROLES])
                widget.setCurrentText(shown)
            else:
                widget = QLineEdit(shown)
                if field.column in ("purchase_date", "warranty_end", "retired_at"):
                    widget.setPlaceholderText("YYYY-MM-DD")
                if field.column == "batch_key":
                    key, source = metadata.batch_key(row)
                    widget.setPlaceholderText(f"{key} (derived)" if source == "derived"
                                              else "maker + model + purchase month")
            form.addRow(field.label, widget)
            self._before[field.name] = shown
            self._inputs[field.name] = widget
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def set_value(self, name: str, text: str) -> None:
        widget = self._inputs[name]
        if isinstance(widget, QComboBox):
            widget.setCurrentText(text)
        else:
            widget.setText(text)

    def changes(self) -> dict[str, str | None]:
        """What the user altered: a new value, or None where a value was blanked."""
        out: dict[str, str | None] = {}
        for name, widget in self._inputs.items():
            text = (widget.currentText() if isinstance(widget, QComboBox)
                    else widget.text()).strip()
            if text == self._before[name]:
                continue
            out[name] = text or None
        return out
