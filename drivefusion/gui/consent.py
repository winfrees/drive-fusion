"""The scope disclaimer in the GUI (docs/PLAN.md §1, REVIEW SR-25).

Shown at startup, before the window, and again by any action that would read
a drive while it is still unaccepted. Accepting is recorded in the catalog's
audit log; declining at startup closes the application without reading
anything.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from drivefusion.core import disclaimer

TITLE = "Before Drive Fusion reads any drive"


def _ask(parent) -> bool:
    """Show the text; True if the user accepted it."""
    box = QMessageBox(parent)
    box.setWindowTitle(TITLE)
    box.setIcon(QMessageBox.Information)
    box.setText(disclaimer.TEXT)
    box.setInformativeText(
        "Your acceptance is recorded, with the date, in this catalog's audit log."
    )
    accept = box.addButton("I accept", QMessageBox.AcceptRole)
    box.addButton("Quit", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is accept


def ensure(parent, catalog) -> bool:
    """True when the disclaimer is, or has just been, accepted."""
    if disclaimer.accepted(catalog):
        return True
    if disclaimer.accepted_by_environment():
        disclaimer.accept(catalog, "env")
        return True
    if _ask(parent):
        disclaimer.accept(catalog, "dialog")
        return True
    return False
