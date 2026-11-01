"""The V&V dashboard: ``python -m vv gui``.

Pick the drives (simulated, or a folder on each real drive), the suites to
judge, and how many runs; press Run. Each run builds a fresh dataset, runs
Drive Fusion through it, judges the chosen requirements, and cleans up. The
results grid fills in as runs finish; Open dashboard shows the campaign's
HTML report, and Open campaign… reads any earlier one back.

This is test equipment, like the rest of ``vv/``: it writes test data onto
the drives you give it, in a ``DriveFusion-VV`` folder it creates and
removes. Drive Fusion itself only reads them.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vv import campaign, dashboard
from vv.suites import SUITES

COLOURS = dashboard.COLOURS
SIZES = ("unlimited", "1GB", "2GB", "4GB", "8GB", "16GB")


class Worker(QObject):
    logged = Signal(str)
    stepped = Signal(int, int, str)
    run_finished = Signal(str)                 # the campaign folder, its summary saved
    done = Signal(str, str)                    # campaign folder, error ("" if none)

    def __init__(self, config: campaign.Config) -> None:
        super().__init__()
        self.config = config
        self.stop = threading.Event()

    def work(self) -> None:
        folder, error = self.config.out, ""

        try:
            folder = str(campaign.run(self.config, log=self.logged.emit,
                                      progress=self.stepped.emit, cancelled=self.stop.is_set,
                                      after_run=lambda out: self.run_finished.emit(str(out))))
        except Exception as exc:  # noqa: BLE001 - shown to the person, not swallowed
            error = f"{type(exc).__name__}: {exc}"
        self.done.emit(folder, error)


class Window(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Drive Fusion V&V")
        self.resize(1200, 760)
        self.folder: Path | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None

        left = QWidget()
        form = QVBoxLayout(left)

        drives = QGroupBox("Drives")
        dl = QVBoxLayout(drives)
        self.simulated = QRadioButton("Simulated drives (folders on this computer)")
        self.real = QRadioButton("Real drives: one folder on each physical drive")
        group = QButtonGroup(self)
        group.addButton(self.simulated)
        group.addButton(self.real)
        self.simulated.setChecked(True)
        row = QHBoxLayout()
        self.drive_count = QSpinBox(minimum=1, maximum=16, value=3)
        self.drive_size = QComboBox()
        self.drive_size.addItems(SIZES)
        self.drive_size.setCurrentText("4GB")
        row.addWidget(QLabel("Count"))
        row.addWidget(self.drive_count)
        row.addWidget(QLabel("Size"))
        row.addWidget(self.drive_size)
        row.addStretch(1)
        self.paths = QListWidget()
        self.paths.setMaximumHeight(110)
        prow = QHBoxLayout()
        self.add_path = QPushButton("Add drive folder…")
        self.drop_path = QPushButton("Take off the list")
        prow.addWidget(self.add_path)
        prow.addWidget(self.drop_path)
        prow.addStretch(1)
        note = QLabel("Each run writes test data into a new DriveFusion-VV folder inside each "
                      "folder, and removes it after the run. Use one folder per physical drive.")
        note.setWordWrap(True)
        for w in (self.simulated,):
            dl.addWidget(w)
        dl.addLayout(row)
        dl.addWidget(self.real)
        dl.addWidget(self.paths)
        dl.addLayout(prow)
        dl.addWidget(note)
        form.addWidget(drives)

        suites = QGroupBox("Suites")
        sl = QVBoxLayout(suites)
        self.suite_boxes: dict[str, QCheckBox] = {}
        for s in SUITES.values():
            box = QCheckBox(f"{s.title}  ({len(s.requirements())})")
            box.setToolTip(f"{s.description}\n{', '.join(s.requirements())}")
            box.setChecked(True)
            sl.addWidget(box)
            self.suite_boxes[s.name] = box
        srow = QHBoxLayout()
        every = QPushButton("All")
        none = QPushButton("None")
        every.clicked.connect(lambda: [b.setChecked(True) for b in self.suite_boxes.values()])
        none.clicked.connect(lambda: [b.setChecked(False) for b in self.suite_boxes.values()])
        srow.addWidget(every)
        srow.addWidget(none)
        srow.addStretch(1)
        sl.addLayout(srow)
        form.addWidget(suites)

        runs = QGroupBox("Runs")
        rl = QFormLayout(runs)
        self.iterations = QSpinBox(minimum=1, maximum=500, value=3)
        self.files = QSpinBox(minimum=5, maximum=100000, value=120)
        self.seed = QLineEdit()
        self.seed.setPlaceholderText("random")
        self.keep = QCheckBox("Keep each run's test data")
        out_row = QHBoxLayout()
        self.out = QLineEdit(str(Path.home() / "DriveFusion-VV" /
                                 dt.datetime.now().strftime("campaign-%Y%m%d-%H%M%S")))
        browse = QPushButton("…")
        browse.clicked.connect(self.choose_out)
        out_row.addWidget(self.out, 1)
        out_row.addWidget(browse)
        rl.addRow("Runs", self.iterations)
        rl.addRow("Files per drive", self.files)
        rl.addRow("First seed", self.seed)
        rl.addRow("", self.keep)
        rl.addRow("Results folder", out_row)
        form.addWidget(runs)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.stop_button = QPushButton("Stop after this run")
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.stop_button)
        form.addLayout(buttons)
        form.addStretch(1)

        right = QWidget()
        rlay = QVBoxLayout(right)
        top = QHBoxLayout()
        self.verdict = QLabel("No campaign yet")
        self.verdict.setStyleSheet("font-size:16px;font-weight:bold")
        self.suite_line = QLabel()
        self.suite_line.setWordWrap(True)
        self.open_dash = QPushButton("Open dashboard")
        self.open_folder = QPushButton("Open folder")
        self.load = QPushButton("Open campaign…")
        for w in (self.open_dash, self.open_folder):
            w.setEnabled(False)
        top.addWidget(self.verdict, 1)
        top.addWidget(self.open_dash)
        top.addWidget(self.open_folder)
        top.addWidget(self.load)
        rlay.addLayout(top)
        rlay.addWidget(self.suite_line)
        self.progress = QProgressBar()
        self.status = QLabel()
        rlay.addWidget(self.progress)
        rlay.addWidget(self.status)
        self.grid = QTableWidget()
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(20000)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.grid)
        split.addWidget(self.log)
        split.setSizes([480, 200])
        rlay.addWidget(split, 1)

        main = QSplitter()
        main.addWidget(left)
        main.addWidget(right)
        main.setSizes([400, 800])
        self.setCentralWidget(main)

        self.add_path.clicked.connect(self.choose_drive)
        self.drop_path.clicked.connect(lambda: [self.paths.takeItem(self.paths.row(i))
                                                for i in self.paths.selectedItems()])
        self.run_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.request_stop)
        self.open_dash.clicked.connect(lambda: self._open(self.folder / "dashboard.html"))
        self.open_folder.clicked.connect(lambda: self._open(self.folder))
        self.load.clicked.connect(self.choose_campaign)
        self.simulated.toggled.connect(self._mode_changed)
        self._mode_changed()

    # -- choosing --------------------------------------------------------------------

    def _mode_changed(self) -> None:
        sim = self.simulated.isChecked()
        for w in (self.drive_count, self.drive_size):
            w.setEnabled(sim)
        for w in (self.paths, self.add_path, self.drop_path):
            w.setEnabled(not sim)

    def choose_drive(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "A folder on a real drive")
        if folder:
            self.paths.addItem(folder)

    def choose_out(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Where to put the campaign")
        if folder:
            self.out.setText(str(Path(folder) /
                                 dt.datetime.now().strftime("campaign-%Y%m%d-%H%M%S")))

    def choose_campaign(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "A campaign folder")
        if folder:
            self.show_campaign(Path(folder))

    def config(self) -> campaign.Config:
        chosen = [n for n, b in self.suite_boxes.items() if b.isChecked()]
        if not chosen:
            raise ValueError("choose at least one suite")
        paths = [self.paths.item(i).text() for i in range(self.paths.count())]
        if self.real.isChecked() and len(paths) < 1:
            raise ValueError("add at least one folder on a real drive")
        seed = self.seed.text().strip()
        size = self.drive_size.currentText()
        return campaign.Config(
            out=self.out.text().strip(), suites=chosen, iterations=self.iterations.value(),
            drives=self.drive_count.value(),
            drive_paths=paths if self.real.isChecked() else [],
            drive_size=None if size == "unlimited" or self.real.isChecked() else size,
            files_per_drive=self.files.value(), large_files=3,
            first_seed=int(seed) if seed else None, keep=self.keep.isChecked())

    # -- running ---------------------------------------------------------------------

    def start(self) -> None:
        try:
            config = self.config()
        except ValueError as exc:
            QMessageBox.warning(self, "Can't start", str(exc))
            return
        self.folder = Path(config.out)
        self.log.clear()
        self.verdict.setText("Running…")
        self.progress.setRange(0, config.iterations)
        self.progress.setValue(0)
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.thread = QThread()
        self.worker = Worker(config)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.work)
        self.worker.logged.connect(self.log.appendPlainText)
        self.worker.stepped.connect(self._stepped)
        self.worker.run_finished.connect(lambda out: self.show_campaign(Path(out), final=False))
        self.worker.done.connect(self._done)
        self.worker.done.connect(self.thread.quit)
        # Qt, not Python's garbage collector, ends both objects' lives, once the
        # thread has stopped: a QObject left to be collected at interpreter exit,
        # after its thread is gone, can crash the process on the way out.
        self.thread.finished.connect(self._thread_finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        """Closing mid-campaign stops after the current run and waits for it."""
        if self.busy():
            self.request_stop()
            self.thread.quit()
            self.thread.wait()
        super().closeEvent(event)

    def request_stop(self) -> None:
        if self.worker is not None:
            self.worker.stop.set()
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping after the current run…")

    def _stepped(self, index: int, total: int, what: str) -> None:
        self.progress.setValue(index - 1)
        self.status.setText(f"Run {index} of {total}: {what}")

    def _done(self, folder: str, error: str) -> None:
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.setValue(self.progress.maximum())
        self.status.setText(f"Finished. Results in {folder}")
        if error:
            self.verdict.setText("The campaign could not run")
            self.status.setText(error)
            QMessageBox.warning(self, "Campaign stopped", error)
        if Path(folder, "campaign.json").is_file():
            self.show_campaign(Path(folder))

    def busy(self) -> bool:
        return self.thread is not None and self.thread.isRunning()

    # -- results ---------------------------------------------------------------------

    def show_campaign(self, folder: Path, final: bool = True) -> None:
        try:
            if not (folder / "summary.json").is_file():
                dashboard.write(folder)         # an older campaign, never summarised
            summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            if final:
                QMessageBox.warning(self, "Not a campaign", f"{folder}: {exc}")
            return
        self.folder = folder
        for w in (self.open_dash, self.open_folder):
            w.setEnabled(True)
        self.verdict.setText(f"{summary['verdict']} · {len(summary['runs'])} run(s)")
        self.suite_line.setText("Requirements passed in every run: " + " · ".join(
            f"{s['title']} {s['passed']}/{s['requirements']}"
            + (f" ({s['failed']} failed)" if s["failed"] else "")
            for s in summary["suites"].values()))
        self.verdict.setStyleSheet(f"font-size:16px;font-weight:bold;color:"
                                   f"{COLOURS.get(summary['verdict'], '#000')}")
        reqs = summary["requirements"]
        runs = summary["runs"]
        self.grid.clear()
        self.grid.setRowCount(len(reqs))
        self.grid.setColumnCount(2 + len(runs))
        self.grid.setHorizontalHeaderLabels(
            ["Suite", "All runs"] + [f"Run {r['run']}" for r in runs])
        self.grid.setVerticalHeaderLabels(list(reqs))
        for row, (req, info) in enumerate(reqs.items()):
            self.grid.setItem(row, 0, QTableWidgetItem(info["suite"] or ""))
            for col, verdict in enumerate([info["overall"], *info["runs"]], start=1):
                item = QTableWidgetItem(verdict)
                item.setForeground(QBrush(QColor("white")))
                item.setBackground(QBrush(QColor(COLOURS.get(verdict, "#888"))))
                item.setToolTip(info["text"])
                self.grid.setItem(row, col, item)

    def _open(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()
