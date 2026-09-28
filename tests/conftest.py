"""Shared fixtures. Keeps the repo root importable for ``tools`` and ``tests``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures import tree as tree_fixture  # noqa: E402
from tests.fixtures import vhdx as vhdx_fixture  # noqa: E402


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A synthetic tree with duplicates, unicode names, and deep paths."""
    return tree_fixture.build_tree(tmp_path / "fixture-tree")


def _volume_fixture(request, tmp_path_factory, filesystem: str):
    reason = vhdx_fixture.skip_reason()
    if reason:
        pytest.skip(reason)
    image = tmp_path_factory.mktemp("vhdx") / f"df-{filesystem}.vhdx"
    volume = vhdx_fixture.create_volume(image, filesystem=filesystem)
    request.addfinalizer(lambda: vhdx_fixture.destroy_volume(volume))
    return volume


@pytest.fixture(scope="session")
def ntfs_volume(request, tmp_path_factory):
    """A real NTFS volume. Skips off Windows or without admin rights."""
    return _volume_fixture(request, tmp_path_factory, "ntfs")


@pytest.fixture(scope="session")
def exfat_volume(request, tmp_path_factory):
    """A real exFAT volume with a 128 KiB cluster size."""
    return _volume_fixture(request, tmp_path_factory, "exfat")


# -- GUI ---------------------------------------------------------------------

def _qt_unavailable() -> str | None:
    """Why GUI tests cannot run here, or None if they can.

    PySide6 is an optional dependency, and a machine can import it and still fail
    to construct a QApplication for want of platform libraries. Both are skips
    with a stated reason, never a silent pass.
    """
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return "PySide6 is not installed (pip install -e '.[gui]')"

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        return f"Qt platform libraries missing: {exc}"

    if QApplication.instance() is None:
        try:
            QApplication([])
        except Exception as exc:  # noqa: BLE001
            return f"Qt could not start: {exc}"
    return None


@pytest.fixture(scope="session")
def qt_app():
    """A QApplication for the whole session; Qt permits only one."""
    reason = _qt_unavailable()
    if reason:
        pytest.skip(reason)
    from PySide6.QtWidgets import QApplication

    return QApplication.instance()
