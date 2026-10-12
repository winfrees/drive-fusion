"""Drives: the inventory, and which of it is currently connected.

§11 screen 3. Two things here are worth more than the rest:

* **"Last seen" is in words**, because the question a user actually has is "is
  this the drive I scanned in March?" — and a raw timestamp makes them do
  arithmetic to find out.
* **A volume with no identified physical drive gets its own row.** Not dropped,
  not folded into another: an unmapped disk that silently disappeared from the
  inventory would make the totals stop adding up to what was scanned.

Since M5 the screen also carries what the durability model needs and cannot
discover: purchase date, location, disaster zone, batch key. Those fields are
*editable here* because only the owner knows them, and they change the numbers —
two drives from one purchase order are not two independent copies.

Risk is shown with its confidence, never bare. "3.1%/yr (estimated from age —
no SMART counters)" and "3.1%/yr (measured)" are different claims, and a column
of bare percentages would make them look identical.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.analysis import drive_inventory
from drivefusion.core.analysis.exposure import RiskIndex
from drivefusion.gui.format import describe_age, human_bytes, percent
from drivefusion.gui.workers import Progress

COLUMNS = (
    "Drive",
    "Volume",
    "Filesystem",
    "Capacity",
    "Catalogued",
    "Files",
    "Risk/yr",
    "Evidence",
    "Rescan",
    "Last seen",
)

#: Fields the owner knows and the tool cannot discover. Order is the form order.
EDITABLE = (
    ("nickname", "Nickname"),
    ("purchase_date", "Purchased (YYYY-MM-DD)"),
    ("vendor", "Vendor"),
    ("warranty_end", "Warranty ends"),
    ("batch_key", "Batch key (maker|model|YYYY-MM)"),
    ("role", "Role"),
    ("location", "Location"),
    ("disaster_zone", "Disaster zone"),
    ("smart_device", "SMART device"),
)


class DrivesScreen(QWidget):
    """What is in the fleet, and what has been read from each of it."""

    #: Emitted with a task body when the user asks to read SMART.
    healthRequested = Signal(object)

    def __init__(self, session, runner=None, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner
        self._index = None

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
        self.health_button = QPushButton("Read drive health")
        self.edit_button = QPushButton("Edit drive details…")
        for button in (self.refresh_button, self.health_button, self.edit_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.refresh_button.clicked.connect(self.refresh)
        self.health_button.clicked.connect(self.request_health)
        self.edit_button.clicked.connect(self.edit_selected)

        self.refresh()

    def selected_drive_id(self) -> int | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        value = item.data(0, Qt.UserRole)
        return None if value is None else int(value)

    def refresh(self) -> None:
        catalog = self.session.catalog()
        rows = drive_inventory(catalog)
        # One risk index for the whole screen: there are twenty drives and the
        # model is not cheap enough to run per painted row.
        self._index = RiskIndex(catalog)
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
            # A volume with no physical drive counts as its own device, keyed
            # negatively, exactly as the copy-counting rule does.
            key = row["drive_id"] if row["drive_id"] is not None else -row["volume_id"]
            assessment = self._index.risk_for(key)

            item = QTreeWidgetItem(
                [
                    drive_name,
                    row["label"] or row["volume_guid"],
                    row["fs_type"] or "?",
                    human_bytes(capacity)
                    + (
                        f" ({percent(row['free_bytes'], capacity)} free)"
                        if capacity and row["free_bytes"] is not None
                        else ""
                    ),
                    human_bytes(row["bytes"]),
                    f"{row['files']:,}",
                    f"{assessment.annual_probability * 100:.2f}%",
                    assessment.confidence,
                    row["rescan_cost"] or "?",
                    describe_age(row["last_seen_at"]),
                ]
            )
            item.setData(0, Qt.UserRole, row["drive_id"])
            # Risk is never shown bare: "3.1% (measured)" and "3.1% (estimated
            # from age)" are different claims that would otherwise look alike.
            item.setToolTip(6, "\n".join(assessment.reasons))
            item.setToolTip(
                7,
                {
                    "measured": "From the drive's own SMART counters.",
                    "estimated": (
                        "No SMART counters available — estimated from age and "
                        "media class. Not a measurement."
                    ),
                    "unknown": (
                        "Nothing is known about this drive but its presence. "
                        "Treat the figure as a placeholder."
                    ),
                }.get(assessment.confidence, assessment.confidence),
            )
            if row["drive_id"] is None:
                item.setToolTip(
                    0,
                    "This volume's physical drive could not be identified, so it "
                    "counts as its own device for redundancy — never merged with "
                    "another.",
                )
            if row["rescan_cost"] == "full-walk":
                item.setToolTip(
                    8,
                    "No change journal on this filesystem: every rescan re-walks "
                    "the whole volume.",
                )
            self.tree.addTopLevelItem(item)

        if not rows:
            self.note.setText("No volumes yet. Add a root on the Scope screen.")
            return

        parts = [f"{len(rows)} volume(s)"]
        if unmapped:
            parts.append(
                f"{unmapped} with no identified physical drive (each counted "
                "separately for redundancy)"
            )
        measured = sum(
            1
            for row in rows
            if self._index
            and self._index.risk_for(
                row["drive_id"] if row["drive_id"] is not None else -row["volume_id"]
            ).is_measured
        )
        if measured < len(rows):
            parts.append(
                f"{len(rows) - measured} without SMART counters, so their risk "
                "is an estimate rather than a measurement"
            )
        self.note.setText(" · ".join(parts))

    # -- actions ------------------------------------------------------------

    def request_health(self) -> None:
        """Read SMART for every drive. Emits a task body; the window runs it."""
        if self.runner is None:
            return

        def body(catalog, progress, cancel):
            from drivefusion.core.health.collect import collect_health

            def report(drive):
                progress(Progress(f"Reading {_label(drive)}…"))
                cancel.raised()

            return collect_health(catalog, progress=report)

        self.healthRequested.emit(body)

    def edit_selected(self) -> None:
        drive_id = self.selected_drive_id()
        if drive_id is None:
            QMessageBox.information(
                self,
                "Select a drive",
                "Choose a row with an identified physical drive. A volume whose "
                "drive could not be identified has no record to edit.",
            )
            return

        catalog = self.session.catalog()
        row = catalog.drive(drive_id)
        if row is None:
            return

        dialog = DriveDetailsDialog(dict(row), self)
        if dialog.exec() == QDialog.Accepted:
            changes = dialog.changes()
            if changes:
                catalog.update_drive(drive_id, **changes)
                self.refresh()


def _label(drive: dict) -> str:
    return (
        drive.get("nickname")
        or drive.get("model")
        or drive.get("serial")
        or f"drive {drive.get('id')}"
    )


class DriveDetailsDialog(QDialog):
    """The fields only the owner knows, and why they are worth typing in.

    Serial is shown but not editable: it is the drive's identity, and letting it
    be rewritten would re-point a drive's whole health history at another disk.
    """

    def __init__(self, drive: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Drive details — {_label(drive)}")
        self._drive = drive
        self._fields: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "These change the durability figures. Purchase date drives the "
            "bathtub curve; batch key and disaster zone decide whether two "
            "copies are genuinely independent — two drives from one order, in "
            "one room, are not two copies."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        identity = QLabel(drive.get("serial") or "—")
        identity.setToolTip(
            "Read-only: the serial identifies the drive, and its health history "
            "hangs off it."
        )
        form.addRow("Serial (fixed)", identity)

        for name, label in EDITABLE:
            edit = QLineEdit(str(drive.get(name) or ""))
            self._fields[name] = edit
            form.addRow(label, edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def changes(self) -> dict:
        """Only the fields the user actually altered.

        Writing every field back would overwrite values set from the CLI with
        whatever happened to be on screen.
        """
        out: dict[str, str | None] = {}
        for name, edit in self._fields.items():
            typed = edit.text().strip()
            existing = self._drive.get(name)
            current = "" if existing is None else str(existing)
            if typed != current:
                out[name] = typed or None
        return out
