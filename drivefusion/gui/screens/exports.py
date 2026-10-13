"""Exports: pick a folder, see what will be written, write it.

§11 screen 7. The whole screen is one decision — *where* — and one
consequence: a folder appears, containing documents. So the design work is
almost entirely in making that true rather than surprising.

* **The destination is named before anything happens**, and the screen says
  plainly that a new timestamped subfolder will be created inside it and that
  nothing existing will be replaced. "Export" is the only verb on this screen
  that writes, and it writes only there.
* **Choosing a folder inside the archive is refused, with the reason.** It is
  the one realistic way this tool could write to user media, and a user who
  picks their archive drive deserves an explanation rather than a silent
  success they discover later.
* **The contents are listed afterwards**, with sizes, because the only way to
  trust "it wrote 23 files" is to see them.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.export import bundle as bundle_module
from drivefusion.core.export import naming
from drivefusion.core.export.writer import ExportError
from drivefusion.gui.format import human_bytes

CONTENT_COLUMNS = ("File", "Size")


class ExportsScreen(QWidget):
    """Write a bundle, and show what was written."""

    exportRequested = Signal(object)
    catalogChanged = Signal()

    def __init__(self, session, runner=None, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner
        self.result = None
        self.plan = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "<b>Exports</b> — a folder of documents describing your "
                "archive: reports, metadata, per-drive manifests. Not a copy "
                "of your files, and nothing in your archive is changed to "
                "produce it."
            )
        )

        layout.addWidget(self._destination_box())
        layout.addWidget(self._options_box())

        self.export_button = QPushButton("Write export bundle")
        self.export_button.clicked.connect(self._export)
        layout.addWidget(self.export_button)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

        self.contents = QTreeWidget()
        self.contents.setColumnCount(len(CONTENT_COLUMNS))
        self.contents.setHeaderLabels(list(CONTENT_COLUMNS))
        self.contents.setRootIsDecorated(False)
        self.contents.setAlternatingRowColors(True)
        self.contents.header().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.contents, 1)

        self.previous = QTextBrowser()
        self.previous.setMaximumHeight(120)
        layout.addWidget(self.previous)

    # -- the destination ----------------------------------------------------

    def _destination_box(self) -> QWidget:
        box = QGroupBox("Where to write it")
        row = QHBoxLayout(box)

        self.destination = QLineEdit()
        self.destination.setPlaceholderText(
            "a folder outside your archive — each export goes in a new "
            "timestamped subfolder"
        )
        row.addWidget(self.destination, 1)

        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose)
        row.addWidget(choose)
        return box

    def _choose(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where should the export folder be created?"
        )
        if chosen:
            self.destination.setText(chosen)

    def _options_box(self) -> QWidget:
        box = QGroupBox("What to include")
        form = QFormLayout(box)

        self.include_plan = QCheckBox("The most recent saved plan")
        self.include_plan.setChecked(True)
        self.include_plan.setToolTip(
            "Including a plan marks it 'exported' — which is a plan's final "
            "state. There is no 'applied'."
        )
        form.addRow(self.include_plan)

        self.rename_template = QComboBox()
        self.rename_template.setEditable(True)
        self.rename_template.addItem("")
        self.rename_template.addItem(naming.DEFAULT_TEMPLATE)
        self.rename_template.addItem("{collection}/{date}/{stem}{ext}")
        self.rename_template.addItem("{project}/{collection}/{parent}/{stem}{ext}")
        self.rename_template.setToolTip(
            "Leave blank for no rename mapping. A mapping is written only "
            "when the table that undoes it can be written too: once files are "
            "renamed, the old names exist nowhere else."
        )
        form.addRow("Rename mapping", self.rename_template)
        return box

    # -- writing ------------------------------------------------------------

    def _export(self) -> None:
        destination = self.destination.text().strip()
        if not destination:
            self.status.setText(
                "<b>Choose a folder first.</b> It should be outside the drives "
                "you catalogue."
            )
            return

        template = self.rename_template.currentText().strip() or None
        if template:
            try:
                naming.validate_template(template)
            except naming.NamingError as error:
                self.status.setText(f"<b>That template will not work.</b> {error}")
                return

        plan = self._latest_plan() if self.include_plan.isChecked() else None

        def body(catalog, progress, cancel):
            from drivefusion.gui.workers import Progress

            progress(Progress("Writing the bundle…"))
            cancel.raised()
            return bundle_module.build(
                catalog, destination, plan=plan, rename_template=template
            )

        if self.runner is None:
            try:
                self.show_result(
                    bundle_module.build(
                        self.session.catalog(),
                        destination,
                        plan=plan,
                        rename_template=template,
                    )
                )
            except ExportError as error:
                self.show_refusal(error)
            return

        self.exportRequested.emit(body)

    def _latest_plan(self):
        """Rebuild the most recently saved plan, or None if there is none.

        Rebuilt from its stored policy rather than reassembled from step rows:
        the bundle needs the whole document, and a plan is cheap to re-solve.
        """
        from drivefusion.core.plan import document
        from drivefusion.core.policy import from_snapshot

        catalog = self.session.catalog()
        rows = document.plans(catalog, limit=1)
        if not rows:
            return None

        row = rows[0]
        plan = document.build(
            catalog,
            name=row["name"],
            policy=from_snapshot(row["policy_snapshot"]),
            use_cp_sat=False,
        )
        plan.plan_id = int(row["id"])
        return plan

    def show_result(self, result) -> None:
        self.result = result
        self.contents.clear()

        for relative in sorted(result.files):
            path = result.root / relative
            size = path.stat().st_size if path.is_file() else 0
            self.contents.addTopLevelItem(
                QTreeWidgetItem([relative, human_bytes(size)])
            )

        problems = self._validate(result.root)
        lines = [
            f"Wrote <b>{len(result.files)}</b> file(s) to "
            f"<code>{result.root}</code>.",
            "Start with <code>data/report.html</code>.",
        ]
        if problems:
            lines.append(
                "<b>This bundle does not match its own manifest:</b> "
                + "; ".join(problems[:3])
            )
        else:
            lines.append("Verified against its own manifest.")
        if result.warnings:
            lines.append("<b>Warnings:</b> " + "; ".join(result.warnings))

        self.status.setText("<br>".join(lines))
        self.catalogChanged.emit()
        self.refresh()

    def show_refusal(self, error: Exception) -> None:
        """A refused export is an answer, not a crash.

        Shown in place rather than in a dialog: the destination field is right
        there, and the fix is to change it.
        """
        self.contents.clear()
        self.status.setText(
            f"<b>Nothing was written.</b> {error}"
        )

    @staticmethod
    def _validate(root: Path) -> list[str]:
        from drivefusion.core.export import bagit

        return bagit.validate(root)

    # -- history ------------------------------------------------------------

    def refresh(self) -> None:
        catalog = self.session.catalog()
        try:
            rows = catalog.conn.execute(
                "SELECT id, created_at, root_path, files_written "
                "FROM export ORDER BY id DESC LIMIT 10"
            ).fetchall()
        except Exception:                      # noqa: BLE001 - pre-v8 catalog
            rows = []

        if not rows:
            self.previous.setHtml(
                "<i>No exports yet. Everything this screen writes goes into a "
                "folder you choose, in a new subfolder each time.</i>"
            )
            return

        lines = ["<b>Previous exports</b><ul>"]
        for row in rows:
            entry = dict(row)
            lines.append(
                f"<li>{entry['created_at'][:16]} — "
                f"{entry['files_written']} file(s) — "
                f"<code>{entry['root_path']}</code></li>"
            )
        lines.append("</ul>")
        self.previous.setHtml("".join(lines))
