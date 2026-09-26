"""Drives: the inventory, and which of it is currently connected.

§11 screen 3. Two things here are worth more than the rest:

* **"Last seen" is in words**, because the question a user actually has is "is
  this the drive I scanned in March?" — and a raw timestamp makes them do
  arithmetic to find out.
* **A volume with no identified physical drive gets its own row.** Not dropped,
  not folded into another: an unmapped disk that silently disappeared from the
  inventory would make the totals stop adding up to what was scanned.

Editable purchase/warranty/location metadata and SMART sparklines are M5, where
the durability model gives those fields something to feed.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
    "Volume",
    "Filesystem",
    "Capacity",
    "Free",
    "Catalogued",
    "Files",
    "Rescan",
    "Last seen",
)


class DrivesScreen(QWidget):
    """What is in the fleet, and what has been read from each of it."""

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
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.refresh_button.clicked.connect(self.refresh)

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
                    describe_age(row["last_seen_at"]),
                ]
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
                    7,
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
        self.note.setText(" · ".join(parts))
