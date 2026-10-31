"""Where the catalog lives, and portable mode (ROADMAP M8.7, D20)."""

from __future__ import annotations

from pathlib import Path

from drivefusion.core import locations


def test_the_environment_then_portable_then_the_home_default(tmp_path: Path) -> None:
    program = tmp_path / "DriveFusion"
    program.mkdir()
    home = Path.home() / ".drivefusion" / "catalog.db"
    assert locations.default_catalog(environ={}, folder=program) == home
    assert locations.describe(home, environ={}, folder=program) == "the default"
    (program / "portable").write_text("")
    portable = program / "DriveFusion data" / "catalog.db"
    assert locations.default_catalog(environ={}, folder=program) == portable
    assert "portable mode" in locations.describe(portable, environ={}, folder=program)
    env = {"DRIVEFUSION_CATALOG": str(tmp_path / "x.db")}
    assert locations.default_catalog(environ=env, folder=program) == tmp_path / "x.db"
    assert "DRIVEFUSION_CATALOG" in locations.describe(tmp_path / "x.db", environ=env,
                                                       folder=program)
    assert locations.describe(tmp_path / "y.db", environ={}, folder=program) == \
        "chosen with --catalog"


def test_a_portable_marker_must_be_a_file(tmp_path: Path) -> None:
    (tmp_path / "portable").mkdir()
    assert not locations.portable(tmp_path)


def test_a_portable_catalog_on_a_scanned_drive_is_refused(tmp_path: Path, monkeypatch) -> None:
    """F-03 holds in portable mode: the thumb drive can't also be in scope."""
    import pytest

    from drivefusion.core import discovery
    from drivefusion.core.errors import CatalogLocationError
    from drivefusion.core.store.catalog import Catalog

    stick = tmp_path / "stick"
    (stick / "photos").mkdir(parents=True)
    (stick / "portable").write_text("")
    path = locations.default_catalog(environ={}, folder=stick)
    with Catalog(path) as catalog:
        volume = catalog.upsert_volume(volume_guid="STICK")
        with pytest.raises(CatalogLocationError):
            monkeypatch.setattr(discovery, "volume_guid_of", lambda p: "STICK")
            monkeypatch.setattr(discovery, "system_volume_guid", lambda: "SYS")
            catalog.add_scope_root(volume, str(stick / "photos"))
