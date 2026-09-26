"""Catalog access from more than one thread.

A ``sqlite3.Connection`` belongs to the thread that created it. The GUI has at
least two: the UI thread, which must stay responsive, and worker threads running
scans and hash passes that take minutes. Sharing one connection between them is
the standard way to get either ``ProgrammingError: SQLite objects created in a
thread can only be used in that same thread`` or — worse, if someone "fixes" it
with ``check_same_thread=False`` — silent interleaving of two transactions on
one connection.

So: **one catalog per thread**, opened on first use and closed with the session.
Callers ask for :meth:`Session.catalog` and get the one belonging to whatever
thread they are on. Nothing is shared but the path.

SQLite handles the concurrency itself. The catalog is opened in WAL mode
(``core/store/schema.py``), so a reader on the UI thread is never blocked by a
writer on a worker, which is what keeps the browser scrolling while a scan runs.
"""

from __future__ import annotations

import threading
from pathlib import Path

from drivefusion.core.store.catalog import Catalog


class Session:
    """Per-thread catalog handles for one catalog file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._open: list[Catalog] = []

    def catalog(self) -> Catalog:
        """The calling thread's catalog, opened on first use."""
        existing = getattr(self._local, "catalog", None)
        if existing is not None:
            return existing

        catalog = Catalog(self.path)
        self._local.catalog = catalog
        with self._lock:
            self._open.append(catalog)
        return catalog

    def close(self) -> None:
        """Close every connection this session opened, on any thread.

        Closing from a different thread is permitted by SQLite and is the only
        option at shutdown: the worker that opened it is already gone.
        """
        with self._lock:
            handles, self._open = self._open, []
        for catalog in handles:
            try:
                catalog.close()
            except Exception:
                # Shutdown must not fail because a connection was already closed
                # or its thread died mid-statement.
                pass
        self._local = threading.local()

    @property
    def connections(self) -> int:
        """How many connections are open. Used by tests and diagnostics."""
        with self._lock:
            return len(self._open)

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
