"""The directory tree, expanded lazily.

A catalog of 50M files has millions of directories. Building a tree of them at
startup would take minutes and gigabytes, so a node's children are read the first
time it is expanded, and ``hasChildren`` is answered from a single ``EXISTS``
rather than by loading the children to see whether there are any — the difference
between an instant expand arrow and a stall on every root.

Subtree sizes come from ``dir_rollup``, already maintained by the scanner, so
"how big is this folder" is a primary-key lookup rather than a recursive walk.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt

from drivefusion.core.analysis.browse import DirRow, child_dirs
from drivefusion.gui.format import human_bytes

COLUMNS = ("Folder", "Subtree size", "Files")
FOLDER, SUBTREE_BYTES, SUBTREE_FILES = range(3)


class _Node:
    """One directory. Children are ``None`` until the node is expanded."""

    __slots__ = ("row", "parent", "children", "child_row")

    def __init__(self, row: DirRow | None, parent: "_Node | None") -> None:
        self.row = row
        self.parent = parent
        self.children: list["_Node"] | None = None
        #: Index within the parent's child list, for an O(1) ``parent()``.
        self.child_row = 0

    @property
    def dir_id(self) -> int | None:
        return None if self.row is None else self.row.id


class DirTreeModel(QAbstractItemModel):
    """Scope roots and their descendants."""

    def __init__(
        self,
        fetch_children: Callable[[int | None], list[DirRow]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch_children or (lambda parent_id: [])
        self._root = _Node(None, None)

    @classmethod
    def over(cls, catalog, parent=None) -> "DirTreeModel":
        return cls(lambda parent_id: child_dirs(catalog, parent_id), parent)

    def refresh(self) -> None:
        """Re-read from the catalog. Used after a scan finishes."""
        self.beginResetModel()
        self._root = _Node(None, None)
        self.endResetModel()

    # -- structure ---------------------------------------------------------

    def _children_of(self, node: _Node) -> list[_Node]:
        if node.children is None:
            rows = self._fetch(node.dir_id)
            node.children = []
            for position, row in enumerate(rows):
                child = _Node(row, node)
                child.child_row = position
                node.children.append(child)
        return node.children

    def _node(self, index: QModelIndex) -> _Node:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        children = self._children_of(self._node(parent))
        if row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index: QModelIndex = QModelIndex()) -> QModelIndex:  # noqa: A003
        node = self._node(index)
        if node is self._root or node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self.createIndex(node.parent.child_row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.column() > 0:
            return 0
        return len(self._children_of(self._node(parent)))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(COLUMNS)

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802
        node = self._node(parent)
        if node is self._root:
            return True
        if node.children is not None:
            return bool(node.children)
        # Answered by the EXISTS in the query, so the expand arrow is correct
        # without a single child having been loaded.
        return node.row is not None and node.row.has_children

    # -- data --------------------------------------------------------------

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return COLUMNS[section]

    def dir_id_at(self, index: QModelIndex) -> int | None:
        return self._node(index).dir_id

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node = self._node(index)
        if node.row is None:
            return None

        if role == Qt.DisplayRole:
            if index.column() == FOLDER:
                return node.row.name
            if index.column() == SUBTREE_BYTES:
                return human_bytes(node.row.subtree_bytes)
            if index.column() == SUBTREE_FILES:
                return f"{node.row.subtree_files:,}"

        if role == Qt.TextAlignmentRole and index.column() in (
            SUBTREE_BYTES,
            SUBTREE_FILES,
        ):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        if role == Qt.UserRole:
            return node.row
        return None
