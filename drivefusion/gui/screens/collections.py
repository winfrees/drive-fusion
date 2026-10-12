"""Collections: a review queue on the left, the confirmed ones on the right.

§11 screen 5. The shape of this screen is an argument about consent. Inference
produces proposals; a person accepts, renames, merges or rejects each one. So:

* **Every candidate shows its evidence** — the signals that proposed it, sample
  paths, counts, size, date span. A proposal you cannot audit is one you cannot
  safely accept, and "Accept all" over an unauditable list is how a curation
  tool ends up with collections nobody chose.
* **There is no implicit accept.** No default selection, no accept-on-double-
  click, and the bulk action names how many it will confirm.
* **Coverage is at the top**, because a FAIR grade over 12% of an archive is not
  a FAIR grade for the archive, and the share is the first thing that makes
  that visible.

The unfiled bucket is shown as a permanent, legitimate state rather than a
backlog to be nagged about: content nobody intends to curate is allowed to stay
uncurated.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core import standards
from drivefusion.core.curate import candidates as candidates_module
from drivefusion.core.curate import collections as collections_module
from drivefusion.core.fair import ndsa, scorecard
from drivefusion.gui.format import human_bytes, percent

QUEUE_COLUMNS = ("Proposed", "Files", "Size", "Score")
CONFIRMED_COLUMNS = ("Collection", "Files", "Size", "Licence", "FAIR")


class CollectionsScreen(QWidget):
    """The review queue and the confirmed collections."""

    proposeRequested = Signal(object)
    catalogChanged = Signal()

    def __init__(self, session, runner=None, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "<b>Collections</b> — inference proposes; you decide. "
                "Nothing becomes a collection without your confirmation."
            )
        )

        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)

        splitter = QSplitter(Qt.Horizontal)

        # -- the review queue
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("<b>Review queue</b>"))

        self.queue = QTreeWidget()
        self.queue.setColumnCount(len(QUEUE_COLUMNS))
        self.queue.setHeaderLabels(list(QUEUE_COLUMNS))
        self.queue.setRootIsDecorated(False)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue.header().setSectionResizeMode(0, QHeaderView.Stretch)
        left_layout.addWidget(self.queue, 2)

        self.evidence = QTextBrowser()
        self.evidence.setPlaceholderText(
            "Select a proposal to see why it was proposed."
        )
        left_layout.addWidget(self.evidence, 1)

        queue_buttons = QHBoxLayout()
        self.propose_button = QPushButton("Look for collections")
        self.accept_button = QPushButton("Accept…")
        self.merge_button = QPushButton("Merge into…")
        self.reject_button = QPushButton("Not a collection")
        for button in (
            self.propose_button,
            self.accept_button,
            self.merge_button,
            self.reject_button,
        ):
            queue_buttons.addWidget(button)
        queue_buttons.addStretch(1)
        left_layout.addLayout(queue_buttons)
        splitter.addWidget(left)

        # -- the confirmed collections
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("<b>Confirmed</b>"))

        self.confirmed = QTreeWidget()
        self.confirmed.setColumnCount(len(CONFIRMED_COLUMNS))
        self.confirmed.setHeaderLabels(list(CONFIRMED_COLUMNS))
        self.confirmed.setRootIsDecorated(False)
        self.confirmed.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.confirmed.header().setSectionResizeMode(0, QHeaderView.Stretch)
        right_layout.addWidget(self.confirmed, 2)

        self.card = QTextBrowser()
        self.card.setPlaceholderText(
            "Select a collection to see its FAIR and NDSA scorecard."
        )
        right_layout.addWidget(self.card, 2)

        confirmed_buttons = QHBoxLayout()
        self.describe_button = QPushButton("Describe…")
        self.refresh_button = QPushButton("Refresh")
        confirmed_buttons.addWidget(self.describe_button)
        confirmed_buttons.addWidget(self.refresh_button)
        confirmed_buttons.addStretch(1)
        right_layout.addLayout(confirmed_buttons)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.queue.currentItemChanged.connect(self._show_evidence)
        self.confirmed.currentItemChanged.connect(self._show_card)
        self.propose_button.clicked.connect(self.request_proposals)
        self.accept_button.clicked.connect(self.accept_selected)
        self.merge_button.clicked.connect(self.merge_selected)
        self.reject_button.clicked.connect(self.reject_selected)
        self.describe_button.clicked.connect(self.describe_selected)
        self.refresh_button.clicked.connect(self.refresh)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        catalog = self.session.catalog()
        self._refresh_coverage(catalog)
        self._refresh_queue(catalog)
        self._refresh_confirmed(catalog)

    def _refresh_coverage(self, catalog) -> None:
        report = collections_module.coverage(catalog)
        if not report["total_bytes"]:
            self.coverage.setText(
                "Nothing catalogued yet. Add a root on the Scope screen."
            )
            return

        text = (
            f"<b>Coverage: {percent(report['filed_bytes'], report['total_bytes'])}"
            f"</b> of catalogued bytes are in a confirmed collection "
            f"({human_bytes(report['filed_bytes'])} of "
            f"{human_bytes(report['total_bytes'])}). "
            f"{human_bytes(report['unfiled_bytes'])} across "
            f"{report['unfiled_files']:,} files is unfiled — a legitimate "
            "permanent state for content you never intend to curate, and "
            "reported so a score over the filed share is not mistaken for a "
            "score over the archive."
        )
        if report["overlap_warning"]:
            text += (
                " <i>Some collections claim overlapping directories; those "
                "bytes are counted once.</i>"
            )
        self.coverage.setText(text)

    def _refresh_queue(self, catalog) -> None:
        self.queue.clear()
        for row in candidates_module.pending(catalog):
            item = QTreeWidgetItem(
                [
                    row["proposed_title"],
                    f"{row['files']:,}",
                    human_bytes(row["bytes"]),
                    f"{row['confidence']:.2f}",
                ]
            )
            item.setData(0, Qt.UserRole, dict(row))
            self.queue.addTopLevelItem(item)

        # No default selection: nothing should be one keystroke from confirmed.
        self.queue.setCurrentItem(None)
        self.evidence.clear()

    def _refresh_confirmed(self, catalog) -> None:
        self.confirmed.clear()
        for collection in collections_module.all_collections(catalog):
            stats = collections_module.stats(catalog, collection.id)
            card = scorecard.score(catalog, collection.id)
            item = QTreeWidgetItem(
                [
                    collection.title,
                    f"{stats['files']:,}",
                    human_bytes(stats["bytes"]),
                    collection.license or "not set",
                    card.grade,
                ]
            )
            item.setData(0, Qt.UserRole, collection.id)
            if not collection.license:
                item.setToolTip(
                    3,
                    "FAIR R1.1: unlicensed data is not reusable by anyone else, "
                    "so this caps the grade at 'incomplete' however well the "
                    "rest scores.",
                )
            self.confirmed.addTopLevelItem(item)

    # -- selection ----------------------------------------------------------

    def selected_candidate(self) -> dict | None:
        item = self.queue.currentItem()
        return None if item is None else item.data(0, Qt.UserRole)

    def selected_collection_id(self) -> int | None:
        item = self.confirmed.currentItem()
        return None if item is None else int(item.data(0, Qt.UserRole))

    def _show_evidence(self, current, _previous=None) -> None:
        if current is None:
            self.evidence.clear()
            return
        row = current.data(0, Qt.UserRole)
        evidence = json.loads(row["evidence_json"])

        lines = [
            f"<b>{row['proposed_title']}</b>",
            f"<code>{evidence.get('path', '')}</code>",
            f"{row['files']:,} files · {human_bytes(row['bytes'])}",
        ]
        if evidence.get("date_span_days") is not None:
            lines.append(f"Modified across {evidence['date_span_days']} days")

        lines.append("<br><b>Why this was proposed</b><ul>")
        for signal in evidence.get("signals", []):
            if signal["score"] > 0:
                lines.append(f"<li>{signal['detail']}</li>")
        lines.append("</ul>")

        if evidence.get("top_extensions"):
            types = ", ".join(
                f".{ext or 'none'} ({count})"
                for ext, count in evidence["top_extensions"]
            )
            lines.append(f"<b>File types:</b> {types}")

        if evidence.get("sample_paths"):
            lines.append("<br><b>Sample files</b><ul>")
            for sample in evidence["sample_paths"]:
                lines.append(f"<li><code>{sample}</code></li>")
            lines.append("</ul>")

        self.evidence.setHtml("<br>".join(lines))

    def _show_card(self, current, _previous=None) -> None:
        if current is None:
            self.card.clear()
            return
        catalog = self.session.catalog()
        collection_id = int(current.data(0, Qt.UserRole))
        card = scorecard.score(catalog, collection_id)
        levels = ndsa.score(catalog, collection_id)

        lines = [f"<b>FAIR: {card.summary()}</b>", f"<i>{card.rubric}</i>"]

        if card.failures:
            lines.append("<br><b>Gaps, worst first</b><ul>")
            for failure in sorted(
                card.failures, key=lambda r: (not r.blocking, -r.weight)
            ):
                flag = " <b>(blocking)</b>" if failure.blocking else ""
                lines.append(
                    f"<li><b>{failure.id}</b> {failure.title}{flag}<br>"
                    f"{failure.detail}<br><i>{failure.remedy}</i></li>"
                )
            lines.append("</ul>")

        if card.not_assessed:
            not_scored = ", ".join(
                f"{r.id} ({r.detail})" for r in card.not_assessed
            )
            # Said plainly: otherwise a high percentage over a few criteria
            # reads as a high score over all of them.
            lines.append(f"<b>Not scored:</b> {not_scored}")

        lines.append(f"<br><b>{levels.summary()}</b><ul>")
        for area in levels.areas:
            ceiling = (
                f" (this tool can assess up to level {area.ceiling})"
                if area.ceiling is not None and area.ceiling < 4
                else ""
            )
            lines.append(f"<li>{area.title}: level {area.reached}{ceiling}")
            for level in area.levels:
                if level.status == "fail":
                    lines.append(f"<br>Next: {level.statement} — {level.detail}")
                    break
            lines.append("</li>")
        lines.append("</ul>")

        recommendations = scorecard.format_recommendations(catalog, collection_id)
        if recommendations:
            lines.append(
                "<b>At-risk formats</b> — a recommendation only; Drive Fusion "
                "never converts anything.<ul>"
            )
            for entry in recommendations[:5]:
                alternative = (
                    f" Consider: {entry['alternative']}."
                    if entry["alternative"]
                    else ""
                )
                lines.append(
                    f"<li>.{entry['extension']} — {entry['files']:,} files, "
                    f"{human_bytes(entry['bytes'])}. {entry['why']}.{alternative}"
                    "</li>"
                )
            lines.append("</ul>")

        self.card.setHtml("".join(lines))

    # -- actions ------------------------------------------------------------

    def request_proposals(self) -> None:
        if self.runner is None:
            return

        def body(catalog, progress, cancel):
            from drivefusion.gui.workers import Progress

            progress(Progress("Scoring subtrees…"))
            cancel.raised()
            proposals = candidates_module.propose(catalog)
            return candidates_module.record(catalog, proposals)

        self.proposeRequested.emit(body)

    def accept_selected(self) -> None:
        row = self.selected_candidate()
        if row is None:
            QMessageBox.information(
                self, "Select a proposal", "Choose a proposal to confirm."
            )
            return

        dialog = DescribeDialog(
            {"title": row["proposed_title"]}, confirming=True, parent=self
        )
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.values()
        title = values.pop("title", None) or row["proposed_title"]
        catalog = self.session.catalog()
        candidates_module.accept(catalog, int(row["id"]), title=title, **values)
        self.refresh()
        self.catalogChanged.emit()

    def merge_selected(self) -> None:
        row = self.selected_candidate()
        if row is None:
            return
        catalog = self.session.catalog()
        existing = collections_module.all_collections(catalog)
        if not existing:
            QMessageBox.information(
                self,
                "No collections yet",
                "Confirm one collection first, then proposals can be merged "
                "into it.",
            )
            return

        titles = [f"[{c.id}] {c.title}" for c in existing]
        choice, ok = QInputDialog.getItem(
            self, "Merge into", "Add this subtree to:", titles, 0, False
        )
        if not ok:
            return
        collection_id = existing[titles.index(choice)].id
        candidates_module.merge_into(catalog, int(row["id"]), collection_id)
        self.refresh()
        self.catalogChanged.emit()

    def reject_selected(self) -> None:
        row = self.selected_candidate()
        if row is None:
            return
        answer = QMessageBox.question(
            self,
            "Not a collection?",
            f"Drive Fusion will stop proposing:\n\n{row['proposed_title']}\n\n"
            "Nothing on the drive changes. The proposal is remembered as "
            "declined so it is not offered after every scan.",
        )
        if answer == QMessageBox.Yes:
            candidates_module.reject(self.session.catalog(), int(row["id"]))
            self.refresh()

    def describe_selected(self) -> None:
        collection_id = self.selected_collection_id()
        if collection_id is None:
            QMessageBox.information(
                self, "Select a collection", "Choose a collection to describe."
            )
            return

        catalog = self.session.catalog()
        collection = collections_module.get(catalog, collection_id)
        dialog = DescribeDialog(collection.row, parent=self)
        if dialog.exec() == QDialog.Accepted:
            changes = dialog.changes(collection.row)
            if changes:
                collections_module.update(catalog, collection_id, **changes)
                self.refresh()
                self.catalogChanged.emit()


class DescribeDialog(QDialog):
    """The metadata the scorecard asks for, with the controlled terms offered.

    Licence is first and labelled, because it is the one field whose absence
    caps the grade — and the vocabulary includes "not for sharing", so a private
    collection has a clear answer rather than being pushed toward a blank.
    """

    FIELDS = (
        ("title", "Title", False),
        ("description", "Description", True),
        ("creators_json", "Creators (comma-separated)", False),
        ("contact", "Contact", False),
        ("project", "Project", False),
        ("retention_note", "Retention note", True),
        ("external_pid", "DOI or accession", False),
    )

    def __init__(self, existing: dict, *, confirming: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm collection" if confirming else "Describe collection")
        self._widgets: dict = {}

        layout = QVBoxLayout(self)
        intro = QLabel(
            "A licence is the one field whose absence caps the FAIR grade: "
            "unlicensed data is not reusable by anyone else. If it must not be "
            "shared, say so — that is a clear answer, and a blank is not."
            if confirming
            else "These fields are what the FAIR scorecard measures."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        vocab = standards.vocabularies()

        self._licence = QLineEdit(str(existing.get("license") or ""))
        licences = ", ".join(entry["id"] for entry in vocab["licenses"])
        self._licence.setPlaceholderText(licences)
        self._licence.setToolTip(f"Controlled terms: {licences}")
        form.addRow("Licence (FAIR R1.1)", self._licence)

        self._data_type = QLineEdit(str(existing.get("data_type") or ""))
        types = ", ".join(entry["id"] for entry in vocab["data_types"])
        self._data_type.setPlaceholderText(types)
        self._data_type.setToolTip(f"Controlled terms: {types}")
        form.addRow("Data type (FAIR I2)", self._data_type)

        for name, label, multiline in self.FIELDS:
            value = existing.get(name)
            if name == "creators_json" and value:
                try:
                    parsed = json.loads(value)
                    value = ", ".join(parsed) if isinstance(parsed, list) else str(value)
                except (TypeError, json.JSONDecodeError):
                    value = str(value)
            if multiline:
                widget = QPlainTextEdit(str(value or ""))
                widget.setMaximumHeight(70)
            else:
                widget = QLineEdit(str(value or ""))
            self._widgets[name] = widget
            form.addRow(label, widget)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _text(self, widget) -> str:
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText().strip()
        return widget.text().strip()

    def values(self) -> dict:
        """Everything typed, as the collection fields expect them."""
        out: dict = {}
        licence = self._licence.text().strip()
        if licence:
            out["license"] = licence
        data_type = self._data_type.text().strip()
        if data_type:
            out["data_type"] = data_type

        for name, widget in self._widgets.items():
            text = self._text(widget)
            if not text:
                continue
            if name == "creators_json":
                out[name] = json.dumps(
                    [part.strip() for part in text.split(",") if part.strip()]
                )
            else:
                out[name] = text
        return out

    def changes(self, existing: dict) -> dict:
        """Only what actually changed, so a save does not overwrite the CLI."""
        proposed = self.values()
        out: dict = {}

        for name in ("license", "data_type"):
            widget = self._licence if name == "license" else self._data_type
            typed = widget.text().strip()
            current = "" if existing.get(name) is None else str(existing[name])
            if typed != current:
                out[name] = typed or None

        for name, widget in self._widgets.items():
            typed = self._text(widget)
            current = existing.get(name)
            if name == "creators_json" and current:
                try:
                    parsed = json.loads(current)
                    current = ", ".join(parsed) if isinstance(parsed, list) else current
                except (TypeError, json.JSONDecodeError):
                    pass
            current = "" if current is None else str(current)
            if typed != current:
                out[name] = proposed.get(name) or None
        return out
