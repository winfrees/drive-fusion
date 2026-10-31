"""Plan (PLAN §11 screen 7, ROADMAP M7.7).

The current plan as a checklist for a person: copies grouped by the drive
they need connected, each with its verify step, then the steps that need no
drive (describe, review, acquire). Each says why, and a copy shows the
change it makes to the bytes expected lost a year. Above, the simulation:
drives to connect in order, bytes to copy, expected loss before and after,
free space per drive.

Make a plan runs the planner over the catalog; nothing on any drive is read
or written, and Drive Fusion never carries a plan out.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.planner import document
from drivefusion.core.store import plans
from drivefusion.gui.format import human_bytes
from drivefusion.gui.workers import Progress

COLUMNS = ("Step", "Size", "Loss a year", "Why")


def _delta(value) -> str:
    if not value:
        return ""
    return ("−" if value < 0 else "+") + human_bytes(int(abs(value)))


class PlanScreen(QWidget):
    #: A task body for the window to run (it owns progress and Cancel).
    planRequested = Signal(object)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Plan</b> — copies to make, for you to carry out. Drive Fusion copies, moves "
            "and deletes nothing; every existing copy stays where it is."))
        row = QHBoxLayout()
        self.make_button = QPushButton("Make a plan")
        self.make_button.setToolTip("Plan from the catalog. Reads the catalog only; the last "
                                    "draft is kept as superseded.")
        self.chooser = QComboBox()
        row.addWidget(self.make_button)
        row.addWidget(QLabel("Plan:"))
        row.addWidget(self.chooser, 1)
        layout.addLayout(row)

        self.requires = QLabel()
        self.requires.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.requires)
        layout.addWidget(self.summary)

        self.steps = QTreeWidget()
        self.steps.setColumnCount(len(COLUMNS))
        self.steps.setHeaderLabels(list(COLUMNS))
        self.steps.header().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.steps, 1)

        self.make_button.clicked.connect(self.request_plan)
        self.chooser.currentIndexChanged.connect(self._show_selected)
        self.refresh()

    def refresh(self) -> None:
        catalog = self.session.catalog()
        self.chooser.blockSignals(True)
        self.chooser.clear()
        for row in plans.plans(catalog):
            self.chooser.addItem(f"{row['id']}: {row['name']} ({row['status']})", row["id"])
        self.chooser.blockSignals(False)
        self._show_selected()

    def _show_selected(self) -> None:
        plan_id = self.chooser.currentData()
        self.steps.clear()
        if plan_id is None:
            self.requires.setText("No plan yet. Make a plan to see what to copy where.")
            self.summary.setText("")
            return
        self.show_plan(document.to_json(self.session.catalog(), plan_id))

    def show_plan(self, doc: dict) -> None:
        s = doc["summary"]
        connect = s.get("drives_to_connect") or []
        self.requires.setText(
            ("Requires: connect " + ", then ".join(connect)) if connect else
            "No copies to make: every unit meets its target, or nothing has room.")
        lines = [f"{s.get('copies', 0)} copies, {human_bytes(s.get('bytes_to_copy'))} "
                 f"({human_bytes(s.get('footprint_to_copy'))} on disk) · expected loss a year "
                 f"{human_bytes(int(s.get('expected_loss_before') or 0))} → "
                 f"{human_bytes(int(s.get('expected_loss_after') or 0))} · units at target "
                 f"{s.get('units_at_target_before')} → {s.get('units_at_target_after')} of "
                 f"{s.get('units')} · solver {doc['solver']}"]
        if s.get("short_copies"):
            lines.append(f"{s['short_copies']} copies short: see the acquire step.")
        lines.append("Free space: " + "; ".join(
            f"{d['label']} {human_bytes(d['free_before'])} → {human_bytes(d['free_after'])}"
            for d in s.get("drives", []) if not d["excluded"]))
        self.summary.setText("\n".join(lines))

        groups: dict[str, QTreeWidgetItem] = {}
        for st in doc["steps"]:
            key = st["dst_drive"] or "No drive needed"
            parent = groups.get(key)
            if parent is None:
                parent = QTreeWidgetItem([key if st["dst_drive"] else key, "", "", ""])
                groups[key] = parent
                self.steps.addTopLevelItem(parent)
                parent.setExpanded(True)
            if st["kind"] == "copy":
                label = f"{st['seq']}. copy {st['src_drive'] or 'unconfirmed drive'}: " \
                        f"{st['src_path']} → {st['dst_path']}"
            elif st["dst_path"]:
                label = f"{st['seq']}. {st['kind']} {st['dst_path']}"
            else:
                label = f"{st['seq']}. {st['kind']}"
            child = QTreeWidgetItem([label, human_bytes(st["bytes"]) if st["bytes"] else "",
                                     _delta(st["risk_delta"]), st["rationale"]])
            child.setToolTip(3, st["rationale"])
            child.setData(0, Qt.UserRole, st["kind"])
            parent.addChild(child)

    def request_plan(self) -> None:
        def body(catalog, progress, cancel):
            progress(Progress("Planning copies across the drives"))
            return document.save(catalog, document.make(catalog))
        self.planRequested.emit(body)
