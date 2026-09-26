"""The file table: keyset paging, and a row count that never lies.

Three rules from §11, and what each protects against:

**Fetch pages on demand.** ``rowCount()`` returns what has been *loaded*, not
what exists. Qt calls ``rowCount`` constantly while painting; if it meant "run
``COUNT(*)`` over 50M rows" the window would stall on every repaint.

**Keyset, never OFFSET.** ``LIMIT 200 OFFSET 4000000`` makes SQLite walk four
million index entries to throw them away. Seeking on ``(dir_id, name)`` — the
unique index that already exists — costs the same on page one and page twenty
thousand.

**A count that is still running says so.** The total is computed off-thread and
shown as "counting…" until it arrives. The number of files is never inferred
from what happens to be loaded: a user reading "200 files" from a table that has
merely loaded 200 of 9,000 has been told something false.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from drivefusion.core.analysis.browse import PAGE_ROWS, FileRow, files_in_dir
from drivefusion.gui.format import human_bytes

COLUMNS = ("Name", "Size", "Copies")
NAME, SIZE, COPIES = range(3)

#: Shown where a file has no content identity yet. Not "1", not "0" — unknown.
UNKNOWN = "—"


class FileTableModel(QAbstractTableModel):
    """A directory's files, loaded a page at a time."""

    #: Emitted when a page arrives, so a status bar can follow along.
    pageLoaded = Signal(int)

    def __init__(
        self,
        fetch_page: Callable[[int, str | None, int], list[FileRow]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[FileRow] = []
        self._dir_id: int | None = None
        self._exhausted = True
        self._fetch_page = fetch_page or _no_source
        #: Authoritative count, filled in asynchronously. ``None`` = counting.
        self._total: int | None = None

    @classmethod
    def over(cls, catalog, parent=None) -> "FileTableModel":
        """A model reading directly from ``catalog`` on the calling thread."""
        return cls(
            lambda dir_id, after, limit: files_in_dir(
                catalog, dir_id, after_name=after, limit=limit
            ),
            parent,
        )

    # -- source ------------------------------------------------------------

    def set_directory(self, dir_id: int | None) -> None:
        """Point the table at a directory and load its first page."""
        self.beginResetModel()
        self._rows = []
        self._dir_id = dir_id
        self._exhausted = dir_id is None
        self._total = None
        self.endResetModel()
        if dir_id is not None:
            self._load_next_page()

    @property
    def directory(self) -> int | None:
        return self._dir_id

    def set_total(self, total: int | None) -> None:
        """Record the authoritative row count (or ``None`` while counting)."""
        self._total = total
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, len(COLUMNS) - 1)
            self.dataChanged.emit(top, bottom)

    @property
    def total(self) -> int | None:
        return self._total

    @property
    def loaded(self) -> int:
        return len(self._rows)

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def status_text(self) -> str:
        """What the view prints above the table."""
        if self._dir_id is None:
            return "No directory selected"
        if self._total is None:
            return f"{len(self._rows):,} loaded (counting…)"
        if self._exhausted:
            return f"{self._total:,} files"
        return f"{len(self._rows):,} of {self._total:,} files"

    # -- paging ------------------------------------------------------------

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802
        if parent.isValid():
            return False
        return not self._exhausted

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802
        if parent.isValid() or self._exhausted:
            return
        self._load_next_page()

    def _load_next_page(self) -> None:
        assert self._dir_id is not None
        # The cursor is the last name already loaded. Because (dir_id, name) is
        # unique, this can neither repeat a row nor skip one; an ordering that
        # were not unique would do both, and only under load.
        after = self._rows[-1].name if self._rows else None
        page = self._fetch_page(self._dir_id, after, PAGE_ROWS)

        if not page:
            self._exhausted = True
            return

        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(page) - 1)
        self._rows.extend(page)
        self.endInsertRows()

        # A short page means the end; a full page might be the end, and the next
        # fetch settles it. Never guess from the page size alone.
        if len(page) < PAGE_ROWS:
            self._exhausted = True
        self.pageLoaded.emit(len(self._rows))

    def load_all(self, limit_pages: int = 10_000) -> int:
        """Load every remaining page. For exports and tests, never for paint."""
        pages = 0
        while not self._exhausted and pages < limit_pages:
            self._load_next_page()
            pages += 1
        return len(self._rows)

    # -- Qt model ----------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        # Deliberately the loaded count, not the table's true size: Qt calls this
        # on every repaint, and a COUNT(*) here would be a stall per frame.
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return COLUMNS[section]

    def row_at(self, row: int) -> FileRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == NAME:
                return entry.name
            if column == SIZE:
                return human_bytes(entry.size_bytes)
            if column == COPIES:
                return UNKNOWN if entry.drives is None else str(entry.drives)

        if role == Qt.ToolTipRole and column == COPIES:
            if entry.drives is None:
                return (
                    "No content identity yet — run a hash pass.\n"
                    "Unknown, not zero: this file may well exist elsewhere."
                )
            if entry.drives == 1:
                return "On one drive only. If that drive fails, this is gone."
            return f"On {entry.drives} distinct drives."

        if role == Qt.TextAlignmentRole and column in (SIZE, COPIES):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.UserRole:
            return entry
        return None


def _no_source(dir_id: int, after: str | None, limit: int) -> list[FileRow]:
    return []
