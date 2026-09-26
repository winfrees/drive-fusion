"""Dashboard: the headline numbers, with their coverage stated.

§11 screen 2. The hard part is not drawing the tiles, it is not overstating them.
Every figure here describes the part of the archive that has actually been
hashed, and the coverage line says what share that is. A dashboard reading
"0 bytes duplicated" over an unhashed catalog would be true and useless and read
as reassuring — so when coverage is partial the panel says so first.

Expected-loss-per-year and the scorecard distribution arrive with M5 and M6; the
tiles for them are absent rather than showing a zero that would look like an
answer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from drivefusion.core.analysis import redundancy_summary
from drivefusion.gui.format import human_bytes, percent
from drivefusion.gui.workers import Progress


class Tile(QFrame):
    """One headline figure with a caption."""

    def __init__(self, caption: str, tooltip: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)

        self.value = QLabel("—")
        font = self.value.font()
        font.setPointSize(max(14, font.pointSize() + 6))
        font.setBold(True)
        self.value.setFont(font)

        self.caption = QLabel(caption)
        self.caption.setWordWrap(True)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)
        if tooltip:
            self.setToolTip(tooltip)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class DashboardScreen(QWidget):
    """Totals, durability exposure, and how much of it is actually known."""

    hashRequested = Signal(object)

    def __init__(self, session, runner, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.runner = runner

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Dashboard</b>"))

        self.coverage = QLabel()
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)

        self.coverage_bar = QProgressBar()
        self.coverage_bar.setFormat("%p% of catalogued files have content identity")
        layout.addWidget(self.coverage_bar)

        grid = QGridLayout()
        self.tiles = {
            "files": Tile("Files catalogued"),
            "path_bytes": Tile("Bytes over all paths"),
            "unique_bytes": Tile(
                "Distinct bytes", "What the content would occupy stored once."
            ),
            "duplicate_overhead_bytes": Tile(
                "Duplicate weight",
                "Bytes beyond one copy of each item. Not all waste: copies on "
                "separate drives are the point.",
            ),
            "under_protected_bytes": Tile(
                "On too few drives",
                "Content held on fewer distinct physical drives than your copy "
                "target. This is the queue that matters.",
            ),
            "reclaimable_bytes": Tile(
                "Duplicated within one drive",
                "Space with no durability benefit. A report, never an action — "
                "Drive Fusion does not delete.",
            ),
        }
        order = (
            "files",
            "path_bytes",
            "unique_bytes",
            "duplicate_overhead_bytes",
            "under_protected_bytes",
            "reclaimable_bytes",
        )
        for position, key in enumerate(order):
            grid.addWidget(self.tiles[key], position // 3, position % 3)
        layout.addLayout(grid)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        layout.addWidget(self.detail)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.hash_button = QPushButton("Identify content (reads file bodies)")
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.hash_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.refresh_button.clicked.connect(self.refresh)
        self.hash_button.clicked.connect(self.request_hash)

        self.refresh()

    # -- data ---------------------------------------------------------------

    def refresh(self) -> None:
        catalog = self.session.catalog()
        counts = catalog.counts()
        summary = redundancy_summary(catalog)

        self.tiles["files"].set_value(f"{counts['files']:,}")
        for key in (
            "path_bytes",
            "unique_bytes",
            "duplicate_overhead_bytes",
            "under_protected_bytes",
            "reclaimable_bytes",
        ):
            self.tiles[key].set_value(human_bytes(summary[key]))

        live = counts["files"]
        identified = counts["identified"]
        self.coverage_bar.setMaximum(max(1, live))
        self.coverage_bar.setValue(identified)

        if live == 0:
            self.coverage.setText(
                "Nothing catalogued yet. Add a root on the Scope screen and scan it."
            )
        elif summary["unhashed_files"]:
            self.coverage.setText(
                f"<b>Coverage: {percent(identified, live)}</b> — "
                f"{summary['unhashed_files']:,} of {live:,} catalogued files have "
                "no content identity yet, so the duplication and durability "
                "figures below describe only the rest. Files whose size is "
                "shared with no other file are never opened and never will be."
            )
        else:
            self.coverage.setText(
                f"<b>Coverage: {percent(identified, live)}</b> — every catalogued "
                "file that can have an identity has one."
            )

        self.detail.setText(
            f"{summary['distinct_content']:,} distinct content items across "
            f"{summary['catalogued_paths']:,} paths · "
            f"{summary['under_protected_items']:,} items on fewer than 2 drives"
            "<br><i>Reclamation figures are observations. Drive Fusion never "
            "deletes and never emits a command that would.</i>"
        )

    def request_hash(self) -> None:
        from drivefusion.core.identity import hash_pass

        def body(catalog, progress, cancel):
            progress(Progress("Hashing candidates…"))

            def report(counters):
                progress(
                    Progress(
                        f"{counters.quick_hashed:,} hashed, "
                        f"{counters.full_hashed:,} fully read",
                        counters.quick_hashed,
                        counters.candidates or None,
                    )
                )
                cancel.raised()

            return hash_pass(catalog, progress=report)

        self.hashRequested.emit(body)
