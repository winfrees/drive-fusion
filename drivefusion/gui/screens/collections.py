"""Collections: the review queue and the confirmed collections (PLAN §11 screen 5, ROADMAP M6.9).

Two panes. On the left, the **review queue**: subtrees inference proposed,
each with its evidence (why, files, size, dates, types, sample names) and
the acts a person can take: accept (as a new collection or into an existing
one), reject, split, or accept every proposal above a score. On the right,
the **confirmed collections**, with their metadata, how much of it is filled
in, and their FAIR and NDSA scorecards.

Coverage, the share of distinct bytes in confirmed collections, is shown at
the top, because a scorecard over 12 % of the archive must not look like one
over all of it. Nothing becomes a collection without an explicit act here or
on the command line, and every act is audited (``core/store/curation.py``).
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.curate import confirm, coverage, infer
from drivefusion.core.fair import rubrics, scorecard
from drivefusion.core.store import curation
from drivefusion.core.store.curation import CurationError
from drivefusion.gui.format import human_bytes
from drivefusion.gui.workers import Progress

QUEUE_COLUMNS = ("Folder", "Score", "Files", "Size", "Dates", "Why")
COLLECTION_COLUMNS = ("Collection", "Files", "Size", "Described", "FAIR", "NDSA levels")

#: Collection fields the edit form offers, with their labels.
EDITABLE = (("title", "Title"), ("description", "Description"), ("creators_json", "Creators"),
            ("contact", "Contact"), ("license", "License"), ("data_type", "Data type"),
            ("project", "Project"), ("external_pid", "External PID"))


def _tree(columns) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setColumnCount(len(columns))
    tree.setHeaderLabels(list(columns))
    tree.setRootIsDecorated(False)
    tree.setSelectionBehavior(QAbstractItemView.SelectRows)
    tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
    return tree


class CollectionsScreen(QWidget):
    #: A task body for the window to run (it owns progress and Cancel).
    proposeRequested = Signal(object)
    #: Something was accepted, rejected or edited.
    catalogChanged = Signal()

    def __init__(self, session, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Collections</b> — proposed by Drive Fusion, confirmed "
                                "by you. Nothing joins a collection until you accept it."))
        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)

        splitter = QSplitter(Qt.Horizontal)
        queue_box = QGroupBox("Review queue")
        queue_layout = QVBoxLayout(queue_box)
        self.queue = _tree(QUEUE_COLUMNS)
        queue_layout.addWidget(self.queue, 1)
        self.evidence = QLabel()
        self.evidence.setWordWrap(True)
        self.evidence.setTextInteractionFlags(Qt.TextSelectableByMouse)
        queue_layout.addWidget(self.evidence)
        row = QHBoxLayout()
        self.propose_button = QPushButton("Find proposals")
        self.propose_button.setToolTip(
            "Score the catalogued folders and queue the ones that look like datasets. "
            "Reads the catalog only.")
        self.accept_button = QPushButton("Accept…")
        self.reject_button = QPushButton("Reject")
        self.reject_button.setToolTip("Decline this proposal; it won't be proposed again.")
        self.split_button = QPushButton("Split")
        self.split_button.setToolTip("Set this proposal aside and propose each of its "
                                     "subfolders instead.")
        self.accept_all_button = QPushButton("Accept all above…")
        for button in (self.propose_button, self.accept_button, self.reject_button,
                       self.split_button, self.accept_all_button):
            row.addWidget(button)
        queue_layout.addLayout(row)
        splitter.addWidget(queue_box)

        coll_box = QGroupBox("Confirmed collections")
        coll_layout = QVBoxLayout(coll_box)
        self.collections = _tree(COLLECTION_COLUMNS)
        coll_layout.addWidget(self.collections, 1)
        row = QHBoxLayout()
        self.edit_button = QPushButton("Describe…")
        self.edit_button.setToolTip("Title, creators, license, data type and the rest: what "
                                    "the FAIR and NDSA scorecards look for.")
        self.score_button = QPushButton("Scorecard…")
        row.addWidget(self.edit_button)
        row.addWidget(self.score_button)
        row.addStretch(1)
        coll_layout.addLayout(row)
        splitter.addWidget(coll_box)
        layout.addWidget(splitter, 1)

        self.propose_button.clicked.connect(self.request_proposals)
        self.accept_button.clicked.connect(self.accept_selected)
        self.reject_button.clicked.connect(self.reject_selected)
        self.split_button.clicked.connect(self.split_selected)
        self.accept_all_button.clicked.connect(self.accept_all)
        self.edit_button.clicked.connect(self.edit_selected)
        self.score_button.clicked.connect(self.show_scorecard)
        self.queue.itemSelectionChanged.connect(self._queue_selected)
        self.collections.itemSelectionChanged.connect(self._update_buttons)
        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        catalog = self.session.catalog()
        cov = coverage.coverage(catalog)
        self.coverage.setText(
            f"<b>Coverage: {cov.share:.1%}</b> of distinct bytes are in confirmed collections. "
            f"Unfiled: {human_bytes(cov.unfiled.bytes)}, expected loss "
            f"{human_bytes(int(cov.unfiled.expected_loss))} a year. Unfiled is a legitimate "
            "state for content you never mean to curate.")

        self.queue.clear()
        for row in curation.candidates(catalog):
            e = json.loads(row["evidence_json"])
            span = e.get("date_span")
            item = QTreeWidgetItem([
                e.get("path", ""), f"{row['score']:.2f}", f"{e.get('files', 0):,}",
                human_bytes(e.get("bytes", 0)), f"{span[0]} – {span[1]}" if span else "—",
                "; ".join(e.get("why", [])),
            ])
            item.setData(0, Qt.UserRole, row["id"])
            self.queue.addTopLevelItem(item)

        self.collections.clear()
        for row in curation.collections(catalog):
            stats = curation.member_stats(catalog, row["id"])
            filled, total, _ = confirm.completeness(row)
            fair = scorecard.fair(catalog, row["id"])
            ndsa = scorecard.ndsa(catalog, row["id"])
            levels = ", ".join(f"{g[:4]} {lv}" for g, lv in ndsa.levels.items()
                               if lv is not None)
            item = QTreeWidgetItem([
                row["title"], f"{stats['files']:,}", human_bytes(stats["bytes"]),
                f"{filled}/{total}", "—" if fair.score is None else f"{fair.score:.0%}", levels,
            ])
            item.setData(0, Qt.UserRole, row["id"])
            self.collections.addTopLevelItem(item)
        self.evidence.setText("")
        self._update_buttons()

    def _selected(self, tree) -> int | None:
        item = tree.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _queue_selected(self) -> None:
        candidate_id = self._selected(self.queue)
        if candidate_id is not None:
            row = curation.candidate(self.session.catalog(), candidate_id)
            e = json.loads(row["evidence_json"])
            types = ", ".join(f"{t or '(none)'} {n}" for t, n in e.get("types", []))
            samples = ", ".join(e.get("samples", []))
            self.evidence.setText(f"<b>{e.get('path', '')}</b><br>Types: {types}<br>"
                                  f"Samples: {samples}")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_candidate = self._selected(self.queue) is not None
        for button in (self.accept_button, self.reject_button, self.split_button):
            button.setEnabled(has_candidate)
        has_collection = self._selected(self.collections) is not None
        self.edit_button.setEnabled(has_collection)
        self.score_button.setEnabled(has_collection)

    def _act(self, work) -> bool:
        try:
            work()
        except CurationError as exc:
            QMessageBox.warning(self, "Not done", str(exc))
            return False
        self.refresh()
        self.catalogChanged.emit()
        return True

    # -- the queue ------------------------------------------------------------

    def request_proposals(self) -> None:
        def body(catalog, progress, cancel):
            progress(Progress("Scoring the catalogued folders"))
            return len(infer.infer(catalog))
        self.proposeRequested.emit(body)

    def accept_selected(self) -> None:
        candidate_id = self._selected(self.queue)
        if candidate_id is None:
            return
        catalog = self.session.catalog()
        dialog = AcceptDialog(confirm.default_title(curation.candidate(catalog, candidate_id)),
                              curation.collections(catalog), self)
        if dialog.exec() != QDialog.Accepted:
            return
        title, into = dialog.values()
        self._act(lambda: confirm.accept(catalog, candidate_id,
                                         title=None if into else title, into=into))

    def reject_selected(self) -> None:
        candidate_id = self._selected(self.queue)
        if candidate_id is not None:
            self._act(lambda: confirm.reject(self.session.catalog(), candidate_id))

    def split_selected(self) -> None:
        candidate_id = self._selected(self.queue)
        if candidate_id is not None:
            self._act(lambda: confirm.split(self.session.catalog(), candidate_id))

    def accept_all(self) -> None:
        score, ok = QInputDialog.getDouble(
            self, "Accept all above a score",
            "Accept every waiting proposal scoring at least (each becomes its own "
            "collection, titled after its folder):", 0.8, 0.0, 1.0, 2)
        if ok:
            self._act(lambda: confirm.accept_all(self.session.catalog(), min_score=score))

    # -- collections ----------------------------------------------------------

    def edit_selected(self) -> None:
        collection_id = self._selected(self.collections)
        if collection_id is None:
            return
        row = curation.collection(self.session.catalog(), collection_id)
        dialog = DescribeDialog(row, self)
        if dialog.exec() == QDialog.Accepted:
            changes = dialog.changes()
            if changes:
                self._act(lambda: curation.edit_collection(
                    self.session.catalog(), collection_id, changes))

    def show_scorecard(self) -> None:
        collection_id = self._selected(self.collections)
        if collection_id is not None:
            ScorecardDialog(self.session.catalog(), collection_id, self).exec()


class AcceptDialog(QDialog):
    """A new collection with a title, or into an existing collection."""

    def __init__(self, title: str, existing, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Accept proposal")
        form = QFormLayout(self)
        self.title = QLineEdit(title)
        self.into = QComboBox()
        self.into.addItem("a new collection", None)
        for row in existing:
            self.into.addItem(f"add to: {row['title']}", row["id"])
        form.addRow("Make it", self.into)
        form.addRow("Title", self.title)
        self.into.currentIndexChanged.connect(
            lambda _i: self.title.setEnabled(self.into.currentData() is None))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, int | None]:
        return self.title.text(), self.into.currentData()


class DescribeDialog(QDialog):
    """The collection's descriptive fields; creators comma-separated."""

    def __init__(self, row, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Describe {row['title']}")
        self.row = row
        form = QFormLayout(self)
        self.fields: dict[str, QLineEdit] = {}
        for column, label in EDITABLE:
            value = row[column] or ""
            if column == "creators_json" and value:
                value = ", ".join(json.loads(value))
            self.fields[column] = QLineEdit(value)
            form.addRow(label, self.fields[column])
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def changes(self) -> dict:
        out = {}
        for column, edit in self.fields.items():
            text = edit.text().strip()
            if column == "creators_json":
                names = [n.strip() for n in text.split(",") if n.strip()]
                value = json.dumps(names) if names else None
            else:
                value = text or None
            if value != self.row[column] and not (column == "title" and value is None):
                out[column] = value
        return out


class ScorecardDialog(QDialog):
    """Both scorecards for one collection, every criterion with its remedy."""

    MARKS = {"pass": "✓", "partial": "~", "fail": "✗", "planned": "·", "judgement": "?"}

    def __init__(self, catalog, collection_id: int, parent=None) -> None:
        super().__init__(parent)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        self.trees = {}
        for rubric_id in rubrics.RUBRICS:
            card = scorecard.score(catalog, collection_id, rubric_id)
            self.setWindowTitle(f"Scorecard: {card.title}")
            rubric = rubrics.current(rubric_id)
            if card.levels:
                summary = ", ".join(f"{rubric.groups[g]} {lv if lv is not None else 'not assessed'}"
                                    for g, lv in card.levels.items())
            else:
                summary = "—" if card.score is None else f"{card.score:.0%} of assessed criteria met"
            note = "" if card.rubric_confirmed else " · rubric not yet confirmed against its source"
            layout.addWidget(QLabel(f"<b>{rubric.source} {rubric.version}</b>: {summary}{note}"))
            tree = _tree(("Criterion", "Result", "Detail", "To close the gap"))
            for r in card.results:
                tree.addTopLevelItem(QTreeWidgetItem(
                    [r.criterion, f"{self.MARKS[r.status]} {r.status}", r.detail, r.remedy]))
            layout.addWidget(tree, 1)
            self.trees[rubric_id] = tree
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
