"""Shared fixtures. Keeps the repo root importable for ``tools`` and ``tests``."""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures import tree as tree_fixture  # noqa: E402
from tests.fixtures import vhdx as vhdx_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def _disclaimer_accepted_for_tests(monkeypatch) -> None:
    """Scripted acceptance (docs/PLAN.md §1), as a CI job would give it. The
    gate's own tests remove it."""
    monkeypatch.setenv("DRIVEFUSION_ACCEPT_DISCLAIMER", "1")


@pytest.fixture(autouse=True)
def _audit_actor_is_reset():
    """The CLI and GUI name themselves process-wide; each test starts clean."""
    from drivefusion.core.store import audit

    audit.set_front_end("library")
    yield
    audit.set_front_end("library")


@pytest.fixture(autouse=True)
def _temp_drive_counts_as_the_system_drive(monkeypatch) -> None:
    """Tests keep the catalog and the scanned folders under one temp directory.

    That is only allowed on the system drive (ROADMAP D8), and the temp
    directory is usually there, but not always (``TEMP`` on ``D:``, ``/tmp`` on
    tmpfs). Treating its drive as the system drive keeps the suite independent
    of where the machine puts temp files. Tests of that rule replace this with
    their own answer.
    """
    import tempfile

    from drivefusion.core import discovery

    try:
        temp_volume = discovery.volume_guid_of(tempfile.gettempdir())
    except (discovery.DiscoveryError, OSError):
        return
    monkeypatch.setattr(discovery, "system_volume_guid", lambda: temp_volume)


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


# -- a scanned archive with a project series (ROADMAP M6) -----------------------------

def _write_dated(path: Path, size: int, when: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    stamp = when.replace(tzinfo=dt.timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def archive(tmp_path: Path):
    """Projects/Surveys/<three dated survey folders of CSVs>, and Misc/ (mixed, old)."""
    from drivefusion.cli.main import main
    from drivefusion.core.store.catalog import Catalog

    root = tmp_path / "archive"
    for i, month in enumerate((3, 6, 9)):
        folder = root / "Projects" / "Surveys" / f"2021-{month:02d} Lake Survey"
        for n in range(8):
            _write_dated(folder / f"station_{n:02d}.csv", 2000 + 97 * n + i,
                   dt.datetime(2021, month, 1 + n))
    for n, (ext, year) in enumerate([("docx", 2013), ("jpg", 2016), ("pdf", 2019),
                                     ("txt", 2023), ("xlsx", 2015), ("zip", 2021)]):
        _write_dated(root / "Misc" / f"thing{n}.{ext}", 500 + n, dt.datetime(year, 5, 1))
    catalog = tmp_path / "catalog" / "catalog.db"
    assert main(["--catalog", str(catalog), "--accept-disclaimer", "scope", "add",
                 str(root)]) == 0
    assert main(["--catalog", str(catalog), "scan"]) == 0
    with Catalog(catalog) as cat:
        yield cat, catalog


