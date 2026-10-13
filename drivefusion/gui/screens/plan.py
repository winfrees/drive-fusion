"""Plan: the policy on the left, the checklist on the right.

§11 screen 6. The screen has one job beyond showing steps, and it is a
presentational one that carries the whole weight of the tool's promise: this is
a **document**, and it must not look like a control panel. So there is no
"Apply", no "Run", no per-step checkbox that writes anything back, and the only
buttons are *Build*, *Save* and *Copy to clipboard*. A screen that looks
executable teaches the user that it is, and then one of them goes looking for
the button that does it.

What the screen does owe the user, it owes plainly:

* **Every step shows its reason.** The rationale is not a tooltip; it is the
  second line of the step, because a checklist nobody can interrogate is one
  nobody should follow.
* **Cost and benefit sit together.** Bytes to copy, hours, and the change in
  expected loss are in one panel, so "is this worth an afternoon" is answerable
  without arithmetic.
* **Shortfalls are first-class.** "You need another 2 TB" is an outcome, not an
  error, and it appears as a step rather than as a failure message.
* **Reclamation is in its own tab**, labelled as evidence, never in the
  checklist. Mixing "you could free 400 GB" into a numbered list is how a
  planning tool becomes a deletion tool one button at a time.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.plan import document
from drivefusion.core.policy import Policy
from drivefusion.gui.format import human_bytes

#: How a step kind is introduced in the list. The wording is part of the
#: promise: every one of these is something the *user* does.
KIND_LABELS = {
    "copy": "Copy",
    "verify": "Verify",
    "organize": "Rename or reorganise",
    "describe": "Describe",
    "review-candidate": "Review",
    "acquire": "Buy storage",
}

STEP_COLUMNS = ("#", "Step", "Size on destination", "Why")


class PlanScreen(QWidget):
    """Build a plan, read it, save it. Nothing here carries it out."""

    planRequested = Signal(object)
    catalogChanged = Signal()

    def __init__(self, session, runner=None, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner
        self.plan = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "<b>Plan</b> — a checklist you carry out with your own tools. "
                "Drive Fusion has no executor and will never move, rename or "
                "delete anything itself."
            )
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._policy_panel())
        splitter.addWidget(self._plan_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 860])
        layout.addWidget(splitter, 1)

    # -- the policy panel ---------------------------------------------------

    def _policy_panel(self) -> QWidget:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)

        box = QGroupBox("Policy")
        form = QFormLayout(box)

        self.level = QComboBox()
        for level in (1, 2, 3, 4):
            policy = Policy.for_level(level)
            self.level.addItem(
                f"NDSA level {level} — {policy.min_copies} copies, "
                f"{policy.min_zones} location(s)",
                level,
            )
        form.addRow("Target", self.level)

        self.reserve = QDoubleSpinBox()
        self.reserve.setRange(0.0, 0.5)
        self.reserve.setSingleStep(0.05)
        self.reserve.setDecimals(2)
        self.reserve.setValue(Policy().reserve_fraction)
        self.reserve.setToolTip(
            "Fraction of each drive left free. A drive filled to the last byte "
            "cannot take a manifest and slows down badly."
        )
        form.addRow("Keep free", self.reserve)

        self.batch_diversity = QCheckBox("Spread copies across purchase batches")
        self.batch_diversity.setChecked(True)
        self.batch_diversity.setToolTip(
            "Drives bought together tend to fail together, so two copies from "
            "one order are not two independent copies."
        )
        form.addRow(self.batch_diversity)

        self.use_optimiser = QCheckBox("Use the optimiser if available")
        self.use_optimiser.setChecked(True)
        self.use_optimiser.setToolTip(
            "Runs CP-SAT as an improvement on the plain plan. Without it you "
            "get the straightforward plan, which is never worse than nothing "
            "and is easier to follow."
        )
        form.addRow(self.use_optimiser)

        self.plan_name = QLineEdit()
        self.plan_name.setPlaceholderText("optional name")
        form.addRow("Name", self.plan_name)

        panel_layout.addWidget(box)

        self.exclusions = QGroupBox("Drives to leave alone")
        self.exclusions_layout = QVBoxLayout(self.exclusions)
        self.exclusion_boxes: list[tuple[int, QCheckBox]] = []
        panel_layout.addWidget(self.exclusions)

        self.build_button = QPushButton("Build plan")
        self.build_button.clicked.connect(self._build)
        panel_layout.addWidget(self.build_button)

        self.save_button = QPushButton("Save plan to catalog")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        panel_layout.addWidget(self.save_button)

        self.copy_button = QPushButton("Copy checklist to clipboard")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy)
        panel_layout.addWidget(self.copy_button)

        self.saved = QLabel()
        self.saved.setWordWrap(True)
        panel_layout.addWidget(self.saved)

        panel_layout.addStretch(1)
        return panel

    # -- the plan panel -----------------------------------------------------

    def _plan_panel(self) -> QWidget:
        self.tabs = QTabWidget()

        self.steps = QTreeWidget()
        self.steps.setColumnCount(len(STEP_COLUMNS))
        self.steps.setHeaderLabels(list(STEP_COLUMNS))
        self.steps.setRootIsDecorated(False)
        self.steps.setAlternatingRowColors(True)
        self.steps.setWordWrap(True)
        header = self.steps.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tabs.addTab(self.steps, "Checklist")

        self.simulation = QTextBrowser()
        self.tabs.addTab(self.simulation, "If you follow this plan")

        self.reclamation = QListWidget()
        self.tabs.addTab(self.reclamation, "Space used by duplication")

        self.scorecards = QTextBrowser()
        self.tabs.addTab(self.scorecards, "Scorecards")

        return self.tabs

    # -- building -----------------------------------------------------------

    def policy(self) -> Policy:
        overrides = {
            "reserve_fraction": float(self.reserve.value()),
            "require_batch_diversity": self.batch_diversity.isChecked(),
        }
        excluded = tuple(
            drive_id for drive_id, box in self.exclusion_boxes if box.isChecked()
        )
        if excluded:
            overrides["excluded_drives"] = excluded
        return Policy.for_level(self.level.currentData() or 1, **overrides)

    def _build(self) -> None:
        policy = self.policy()
        name = self.plan_name.text().strip() or None
        use_cp_sat = self.use_optimiser.isChecked()

        def body(catalog, progress, cancel):
            from drivefusion.gui.workers import Progress

            progress(Progress("Working out what is where…"))
            cancel.raised()
            return document.build(
                catalog, name=name, policy=policy, use_cp_sat=use_cp_sat
            )

        if self.runner is None:
            # Synchronous path, used by tests and by any caller without a
            # runner. The window always supplies one.
            catalog = self.session.catalog()
            self.show_plan(
                document.build(
                    catalog, name=name, policy=policy, use_cp_sat=use_cp_sat
                )
            )
            return

        self.planRequested.emit(body)

    def show_plan(self, plan) -> None:
        """Render a plan. Separate from building so a stored plan can be shown."""
        self.plan = plan
        self.saved.setText("")
        self.save_button.setEnabled(True)
        self.copy_button.setEnabled(True)

        self.steps.clear()
        for step in plan.steps:
            label = KIND_LABELS.get(step.kind, step.kind)
            if step.depends_on:
                label += " (after step " + ", ".join(
                    str(d) for d in step.depends_on
                ) + ")"
            item = QTreeWidgetItem(
                [
                    str(step.seq),
                    label,
                    human_bytes(step.footprint_bytes) if step.footprint_bytes else "",
                    step.rationale,
                ]
            )
            item.setToolTip(3, step.rationale)
            self.steps.addTopLevelItem(item)

        if not plan.steps:
            self.steps.addTopLevelItem(
                QTreeWidgetItem(
                    ["", "Nothing to do", "", "Every unit already meets the policy."]
                )
            )

        self.simulation.setHtml(_simulation_html(plan))
        self.scorecards.setHtml(_scorecards_html(plan))

        self.reclamation.clear()
        self.reclamation.addItem(
            QListWidgetItem(
                "Evidence only. Drive Fusion does not delete anything and no "
                "step in the checklist acts on this list; it is here so you can "
                "see where the space went."
            )
        )
        for entry in plan.reclamation_appendix:
            self.reclamation.addItem(
                QListWidgetItem(
                    f"{human_bytes(entry['reclaimable_bytes'])} — "
                    f"{entry['physical_copies']} copies of "
                    f"{human_bytes(entry['size_bytes'])} on one drive: "
                    + ", ".join(entry["locations"][:2])
                )
            )

    def _save(self) -> None:
        if self.plan is None:
            return
        plan_id = document.save(self.session.catalog(), self.plan)
        self.saved.setText(
            f"Saved as plan {plan_id}, status <b>draft</b>. Drive Fusion will "
            "not carry it out."
        )
        self.catalogChanged.emit()

    def _copy(self) -> None:
        if self.plan is None:
            return
        QGuiApplication.clipboard().setText(_checklist_text(self.plan))

    # -- refresh ------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the exclusion list from the drives currently catalogued."""
        from drivefusion.core.analysis.browse import drive_inventory

        rows = drive_inventory(self.session.catalog())

        previously = {
            drive_id for drive_id, box in self.exclusion_boxes if box.isChecked()
        }
        while self.exclusions_layout.count():
            item = self.exclusions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.exclusion_boxes = []

        # One box per physical drive, not per volume: exclusion is about the
        # device that can fail, and two volumes on one disk are one risk.
        seen: set[int] = set()
        for row in rows:
            drive_id = row.get("drive_id")
            if drive_id is None or drive_id in seen:
                continue
            seen.add(int(drive_id))
            name = (
                row.get("nickname")
                or row.get("model")
                or row.get("label")
                or row.get("volume_guid")
                or f"drive {drive_id}"
            )
            box = QCheckBox(str(name))
            box.setChecked(int(drive_id) in previously)
            self.exclusions_layout.addWidget(box)
            self.exclusion_boxes.append((int(drive_id), box))

        if not self.exclusion_boxes:
            self.exclusions_layout.addWidget(
                QLabel(
                    "No identified drives yet. Scan a scope root, and run "
                    "Drives to read serial numbers."
                )
            )


def _simulation_html(plan) -> str:
    simulation = plan.simulation
    if not simulation:
        return "<p>No simulation for this plan.</p>"

    rows = [
        "<h3>If you follow this plan</h3>",
        "<table cellpadding='4'>",
        f"<tr><td>Data to copy</td><td><b>"
        f"{human_bytes(simulation['bytes_to_copy'])}</b></td></tr>",
        f"<tr><td>Estimated time</td><td><b>"
        f"{simulation['estimated_hours']:.1f} hours</b></td></tr>",
        f"<tr><td>Expected loss now</td><td>"
        f"{human_bytes(simulation['expected_loss_before'])} per year</td></tr>",
        f"<tr><td>Expected loss after</td><td><b>"
        f"{human_bytes(simulation['expected_loss_after'])} per year</b></td></tr>",
        "</table>",
        f"<p><i>{simulation.get('throughput_assumption', '')}</i></p>",
    ]

    if simulation.get("per_drive"):
        rows.append("<h4>Free space, before and after</h4><table cellpadding='4'>")
        for entry in simulation["per_drive"]:
            rows.append(
                f"<tr><td>{entry['name']}</td>"
                f"<td>{human_bytes(entry['free_before'])}</td>"
                f"<td>&rarr; {human_bytes(entry['free_after'])}</td></tr>"
            )
        rows.append("</table>")

    objective = plan.objective
    if objective:
        rows.append(
            f"<p>Objective score {objective['weighted']:.4f} (lower is better), "
            f"with durability valued over {objective['horizon_years']} years. "
            f"Solver: {plan.solver}.</p>"
        )
        if objective.get("correlated_copies"):
            rows.append(
                f"<p><b>{objective['correlated_copies']} unit(s)</b> would still "
                "have every copy in one purchase batch or one location.</p>"
            )
    return "".join(rows)


def _scorecards_html(plan) -> str:
    if not plan.scorecards_before and not plan.scorecards_after:
        return (
            "<p>No collections to score. Confirm a collection on the "
            "Collections screen and the plan will carry its scorecard.</p>"
        )

    rows = ["<h3>Before</h3><ul>"]
    for entry in plan.scorecards_before:
        rows.append(f"<li><b>{entry['title']}</b>: {entry['summary']}</li>")
    rows.append("</ul><h3>After, if you follow the plan</h3>")
    rows.append(
        "<p><i>These are a forecast, not a measurement: they assume every "
        "step is carried out and verified.</i></p><ul>"
    )
    for entry in plan.scorecards_after:
        rows.append(f"<li><b>{entry['title']}</b>: {entry['summary']}</li>")
    rows.append("</ul>")
    return "".join(rows)


def _checklist_text(plan) -> str:
    """The checklist as plain text, for pasting into a notebook or a ticket."""
    lines = [
        plan.name,
        f"Policy: {plan.policy.describe()}",
        "",
        "This is a checklist you carry out yourself. Drive Fusion does not",
        "move, rename or delete anything.",
        "",
    ]
    for step in plan.steps:
        lines.append(f"{step.seq:3}. [{KIND_LABELS.get(step.kind, step.kind)}]")
        lines.append(f"     {step.rationale}")
    return "\n".join(lines)
