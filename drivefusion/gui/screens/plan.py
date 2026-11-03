"""Plan (PLAN §11 screen 7, ROADMAP M7.7).

The current plan, for a person to carry out, two ways:

* **Steps**: the copies in rounds (the copies of one round use no drive twice,
  so they can all run at the same time), then the steps that need no drive
  (describe, review, acquire). Each copy is copied and then verified. Picking a
  step shows it in full below; the columns and the split are adjustable.
* **By drive**: a column per drive: what it receives, what is copied from it,
  and what will be extra on it once the copies are verified (information only).

Above both, the simulation: drives to connect in order, bytes to copy,
expected loss before and after, free space per drive.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
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
NO_DRIVE = "Other steps"


def _delta(value) -> str:
    if not value:
        return ""
    return ("−" if value < 0 else "+") + human_bytes(int(abs(value)))


def _what(st: dict) -> str:
    if st["kind"] == "copy":
        return (f"{st['seq']}. copy and verify {st['src_drive'] or 'unconfirmed drive'}: "
                f"{st['src_path']} → {st['dst_drive']}: {st['dst_path']}")
    if st["dst_path"]:
        return f"{st['seq']}. {st['kind']} {st['dst_drive'] or ''}: {st['dst_path']}"
    return f"{st['seq']}. {st['kind']}"


def _leaf(path: str | None) -> str:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    return parts[-1] if parts else (path or "")


def _row(text: str, size: str, full: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem([text, size])
    item.setToolTip(0, full)
    return item


def _list(title: str) -> tuple[QGroupBox, QTreeWidget]:
    box = QGroupBox(title)
    tree = QTreeWidget()
    tree.setColumnCount(2)
    tree.setHeaderLabels(["Folder", "Size"])
    tree.setRootIsDecorated(False)
    tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
    tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
    QVBoxLayout(box).addWidget(tree)
    return box, tree


class DriveColumn(QWidget):
    """One drive on the By drive tab: received above, sent, then extra below."""

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        layout = QVBoxLayout(self)
        self.title = QLabel(f"<b>{name}</b>")
        self.space = QLabel()
        layout.addWidget(self.title)
        layout.addWidget(self.space)
        split = QSplitter(Qt.Vertical)
        box, self.receives = _list("Copied to it")
        split.addWidget(box)
        box, self.sends = _list("Copied from it")
        split.addWidget(box)
        box, self.extra = _list("Extra once verified (information; you decide)")
        split.addWidget(box)
        layout.addWidget(split, 1)


class PlanScreen(QWidget):
    #: A task body for the window to run (it owns progress and Cancel).
    planRequested = Signal(object)

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.doc: dict | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Plan</b> — the copies to make, for you to carry out."))
        row = QHBoxLayout()
        self.make_button = QPushButton("Make a plan")
        self.make_button.setToolTip("Plan from the catalog. The last draft is kept as "
                                    "superseded.")
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
        self.how = QLabel("Each copy: " + document.COPY_AND_VERIFY)
        self.how.setWordWrap(True)
        layout.addWidget(self.requires)
        layout.addWidget(self.summary)
        layout.addWidget(self.how)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        steps_page = QSplitter(Qt.Vertical)
        self.steps = QTreeWidget()
        self.steps.setColumnCount(len(COLUMNS))
        self.steps.setHeaderLabels(list(COLUMNS))
        header = self.steps.header()
        header.setSectionResizeMode(QHeaderView.Interactive)   # every column can be dragged
        header.setStretchLastSection(True)                      # and Why takes what is left
        self.steps.setColumnWidth(0, 460)
        self.steps.setColumnWidth(1, 80)
        self.steps.setColumnWidth(2, 90)
        self.detail = QLabel("Pick a step to see it in full.")
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.detail)
        steps_page.addWidget(self.steps)
        steps_page.addWidget(detail_scroll)
        steps_page.setStretchFactor(0, 3)
        steps_page.setStretchFactor(1, 1)
        steps_page.setSizes([420, 200])
        self.tabs.addTab(steps_page, "Steps")

        self.board = QWidget()
        self.board_layout = QHBoxLayout(self.board)
        self.columns: dict[str, DriveColumn] = {}
        board_scroll = QScrollArea()
        board_scroll.setWidgetResizable(True)
        board_scroll.setWidget(self.board)
        self.tabs.addTab(board_scroll, "By drive")

        self.make_button.clicked.connect(self.request_plan)
        self.chooser.currentIndexChanged.connect(self._show_selected)
        self.steps.currentItemChanged.connect(self._show_detail)
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
        self._clear_board()
        if plan_id is None:
            self.doc = None
            self.requires.setText("No plan yet. Make a plan to see what to copy where.")
            self.summary.setText("")
            return
        self.show_plan(document.to_json(self.session.catalog(), plan_id))

    def show_plan(self, doc: dict) -> None:
        self.doc = doc
        s = doc["summary"]
        connect = s.get("drives_to_connect") or []
        self.requires.setText(
            ("Requires: connect " + ", then ".join(connect)) if connect else
            "No copies to make: every unit meets its target, or nothing has room.")
        rounds = s.get("rounds")
        lines = [f"{s.get('copies', 0)} copies, {human_bytes(s.get('bytes_to_copy'))} "
                 f"({human_bytes(s.get('footprint_to_copy'))} on disk)"
                 + (f" in {rounds} round{'s' if rounds != 1 else ''}" if rounds else "")
                 + f" · expected loss a year "
                 f"{human_bytes(int(s.get('expected_loss_before') or 0))} → "
                 f"{human_bytes(int(s.get('expected_loss_after') or 0))} · units at target "
                 f"{s.get('units_at_target_before')} → {s.get('units_at_target_after')} of "
                 f"{s.get('units')}"]
        if s.get("short_copies"):
            lines.append(f"{s['short_copies']} copies short: see the acquire step.")
        lines.append("Free space: " + "; ".join(
            f"{d['label']} {human_bytes(d['free_before'])} → {human_bytes(d['free_after'])}"
            for d in s.get("drives", []) if not d["excluded"]))
        self.summary.setText("\n".join(lines))
        self._fill_steps(doc)
        self._fill_board(doc)

    # -- Steps ------------------------------------------------------------------------

    def _fill_steps(self, doc: dict) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        for st in doc["steps"]:
            if st.get("round"):
                together = sum(1 for o in doc["steps"] if o.get("round") == st["round"])
                key = (f"Round {st['round']} — {together} copies can run at the same time"
                       if together > 1 else f"Round {st['round']}")
            elif st["kind"] == "verify":          # plans made before copy-and-verify
                key = f"Verify on {st['dst_drive'] or 'its drive'}"
            else:
                key = NO_DRIVE
            parent = groups.get(key)
            if parent is None:
                parent = QTreeWidgetItem([key, "", "", ""])
                groups[key] = parent
                self.steps.addTopLevelItem(parent)
                parent.setExpanded(True)
            child = QTreeWidgetItem([_what(st), human_bytes(st["bytes"]) if st["bytes"] else "",
                                     _delta(st["risk_delta"]), st["rationale"]])
            child.setToolTip(0, _what(st))
            child.setToolTip(3, st["rationale"])
            child.setData(0, Qt.UserRole, st["kind"])
            child.setData(1, Qt.UserRole, st["seq"])
            parent.addChild(child)

    def _show_detail(self, item, _previous=None) -> None:
        if item is None or self.doc is None or item.data(1, Qt.UserRole) is None:
            self.detail.setText("Pick a step to see it in full.")
            return
        seq = item.data(1, Qt.UserRole)
        st = next(s for s in self.doc["steps"] if s["seq"] == seq)
        parts = [f"<b>{_what(st)}</b>", f"<b>Why:</b> {st['rationale']}"]
        if st["kind"] == "copy":
            parts.append(f"<b>How:</b> {document.COPY_AND_VERIFY}")
            parts.append(f"<b>From:</b> {st['src_drive'] or 'unconfirmed drive'}: "
                         f"{st['src_path']}<br><b>To:</b> {st['dst_drive']}: {st['dst_path']}")
            parts.append(f"<b>Size:</b> {human_bytes(st['bytes'])}, "
                         f"{human_bytes(st['footprint_bytes'])} on the destination's clusters"
                         + (f" · round {st['round']}" if st.get("round") else ""))
        if st["risk_delta"]:
            parts.append(f"<b>Expected loss a year:</b> {_delta(st['risk_delta'])}")
        self.detail.setText("<br><br>".join(parts))

    # -- By drive ---------------------------------------------------------------------

    def _clear_board(self) -> None:
        while self.board_layout.count():
            item = self.board_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.columns = {}

    def _column(self, name: str) -> DriveColumn:
        if name not in self.columns:
            self.columns[name] = DriveColumn(name)
            self.board_layout.addWidget(self.columns[name])
        return self.columns[name]

    def _fill_board(self, doc: dict) -> None:
        self._clear_board()
        s = doc["summary"]
        for d in s.get("drives", []):
            column = self._column(d["label"])
            column.space.setText(
                d["excluded"] or f"free {human_bytes(d['free_before'])} → "
                f"{human_bytes(d['free_after'])}")
        for st in doc["steps"]:
            if st["kind"] != "copy":
                continue
            size = human_bytes(st["bytes"]) if st["bytes"] else ""
            note = f" (round {st['round']})" if st.get("round") else ""
            src = st["src_drive"] or "unconfirmed drive"
            full = f"{src}: {st['src_path']} → {st['dst_drive']}: {st['dst_path']}"
            if st["dst_drive"]:
                self._column(st["dst_drive"]).receives.addTopLevelItem(_row(
                    f"{_leaf(st['src_path'])} ← {src}{note}", size, full))
            if st["src_drive"]:
                self._column(st["src_drive"]).sends.addTopLevelItem(_row(
                    f"{_leaf(st['src_path'])} → {st['dst_drive']}{note}", size, full))
        for name, extra in (s.get("extra_after") or {}).items():
            column = self._column(name)
            for x in extra.get("beyond_target", []):
                column.extra.addTopLevelItem(QTreeWidgetItem(
                    [f"{x['label']}: {x['copies']} copies, target {x['target']}",
                     human_bytes(x["bytes"])]))
            if extra.get("same_drive_duplicate_bytes"):
                column.extra.addTopLevelItem(QTreeWidgetItem(
                    ["duplicated within this drive",
                     human_bytes(extra["same_drive_duplicate_bytes"])]))

    def request_plan(self) -> None:
        def body(catalog, progress, cancel):
            progress(Progress("Planning copies across the drives"))
            return document.save(catalog, document.make(catalog))
        self.planRequested.emit(body)
