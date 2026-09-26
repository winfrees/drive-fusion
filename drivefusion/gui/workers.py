"""Long operations, off the UI thread, cancellable.

A scan of twenty drives runs for hours. Doing that on the UI thread freezes the
window and Windows paints "Not Responding" over it, so the parts of this that
matter are not decorative:

* **Cancellation is cooperative and checked often.** A "Cancel" button that only
  takes effect when the work finishes is a lie. Tasks poll
  :meth:`Cancellation.raised` between units of work — per directory, not per
  root, because a root can take hours.
* **Cancelling never leaves a half-truth in the catalog.** The scan pipeline
  checkpoints and the merge is one transaction, so a cancelled scan leaves the
  previous state rather than a partial one.
* **Results cross threads as signals**, never by touching widgets from a worker.
  Qt's queued connections marshal them to the UI thread.

The task body is a plain callable taking ``(catalog, progress, cancel)`` — no Qt
types — so the work stays testable without a GUI, and the tests exercise real
workers rather than a mock of one.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from drivefusion.gui.session import Session


class Cancelled(RuntimeError):
    """Raised inside a task body when the user asked it to stop."""


class Cancellation:
    """A thread-safe stop flag handed to a running task."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raised(self) -> None:
        """Raise :class:`Cancelled` if cancellation was requested.

        Call this between units of work — after a directory, after a file —
        rather than only at the top of a loop that might run for minutes.
        """
        if self._event.is_set():
            raise Cancelled()


@dataclass(frozen=True)
class Progress:
    """A progress report. ``total`` is ``None`` when it is not yet known."""

    message: str
    done: int = 0
    total: int | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.done / self.total)


class TaskSignals(QObject):
    started = Signal()
    progress = Signal(object)   # Progress
    finished = Signal(object)   # the task's return value
    failed = Signal(str, str)   # message, traceback
    cancelled = Signal()


class Task(QRunnable):
    """One unit of background work with its own catalog connection.

    The body runs on a pool thread and is given that thread's catalog, so it
    never borrows the UI thread's connection.
    """

    def __init__(
        self,
        session: Session,
        body: Callable[[Any, Callable[[Progress], None], Cancellation], Any],
        *,
        name: str = "task",
    ) -> None:
        super().__init__()
        self.session = session
        self.body = body
        self.name = name
        self.signals = TaskSignals()
        self.cancellation = Cancellation()
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        self.signals.started.emit()
        try:
            catalog = self.session.catalog()
            result = self.body(catalog, self.signals.progress.emit, self.cancellation)
        except Cancelled:
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            # A worker traceback that only reaches stderr is invisible in a
            # frozen GUI build, so it is carried to the UI and shown there.
            self.signals.failed.emit(f"{self.name}: {exc}", traceback.format_exc())
        else:
            self.signals.finished.emit(result)


class TaskRunner:
    """Starts tasks and keeps them alive while they run.

    A ``QRunnable`` that Python garbage-collects mid-flight takes the process
    with it, so references are held until the task reports back.
    """

    def __init__(self, session: Session, pool: QThreadPool | None = None) -> None:
        self.session = session
        self.pool = pool or QThreadPool.globalInstance()
        self._running: list[Task] = []

    def start(
        self,
        body: Callable[[Any, Callable[[Progress], None], Cancellation], Any],
        *,
        name: str = "task",
    ) -> Task:
        task = Task(self.session, body, name=name)
        self._running.append(task)

        def release(*_args: object) -> None:
            if task in self._running:
                self._running.remove(task)

        task.signals.finished.connect(release)
        task.signals.failed.connect(release)
        task.signals.cancelled.connect(release)

        self.pool.start(task)
        return task

    def cancel_all(self) -> None:
        for task in list(self._running):
            task.cancel()

    @property
    def active(self) -> int:
        return len(self._running)

    def wait(self, msecs: int = 30_000) -> bool:
        """Block until the pool drains. For shutdown and for tests."""
        return self.pool.waitForDone(msecs)
