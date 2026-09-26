"""GUI entry point.

Kept separate from :mod:`main_window` so importing the window in a test does not
construct a ``QApplication`` as a side effect, and so a missing PySide6 fails
here with an instruction rather than as an ImportError deep in a screen.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_CATALOG = Path.home() / ".drivefusion" / "catalog.db"


def main(argv: list[str] | None = None, catalog: Path | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "The GUI needs PySide6, which is an optional dependency:\n"
            "    pip install 'drivefusion[gui]'\n"
            "The command line works without it: drivefusion --help",
            file=sys.stderr,
        )
        return 2

    from drivefusion.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication(list(argv or sys.argv))
    window = MainWindow(catalog or DEFAULT_CATALOG)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
