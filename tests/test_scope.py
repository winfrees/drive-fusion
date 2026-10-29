"""Scope rules: exclusions and root validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drivefusion.core.scope.registry import (
    DEFAULT_EXCLUDES,
    ExclusionSet,
    ScopeError,
    normalise_root,
    validate_new_root,
)


@pytest.mark.parametrize(
    "name",
    ["$RECYCLE.BIN", "$Recycle.Bin", "System Volume Information", "pagefile.sys"],
)
def test_default_exclusions_match_regardless_of_case(name: str) -> None:
    excludes = ExclusionSet.for_root(())
    assert excludes.excludes_name(name)
    assert excludes.excludes_name(name.upper())
    assert excludes.excludes_name(name.lower())


def test_ordinary_names_are_not_excluded() -> None:
    excludes = ExclusionSet.for_root(())
    for name in ("Photos", "report.docx", "2024", "backup.tar.gz"):
        assert not excludes.excludes_name(name)


def test_user_patterns_split_into_name_and_path_rules() -> None:
    excludes = ExclusionSet.for_root(["*.iso", "cache/*"])
    assert excludes.excludes_name("ubuntu.iso")
    assert excludes.excludes_relpath("cache/thumbs.db")
    assert not excludes.excludes_relpath("photos/thumbs.db")


def test_defaults_can_be_disabled() -> None:
    excludes = ExclusionSet.for_root((), use_defaults=False)
    assert not excludes.excludes_name("$RECYCLE.BIN")


def test_normalise_strips_trailing_separators(tmp_path: Path) -> None:
    assert normalise_root(str(tmp_path) + os.sep) == str(tmp_path)


def test_new_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ScopeError, match="not a directory"):
        validate_new_root(str(tmp_path / "missing"), [])


def test_nested_roots_are_rejected_in_both_directions(tmp_path: Path) -> None:
    """A directory must belong to exactly one root, or tombstoning is ambiguous."""
    outer = tmp_path / "archive"
    inner = outer / "photos"
    inner.mkdir(parents=True)

    with pytest.raises(ScopeError, match="already covered"):
        validate_new_root(str(inner), [str(outer)])

    with pytest.raises(ScopeError, match="contains existing scope root"):
        validate_new_root(str(outer), [str(inner)])


def test_sibling_roots_are_allowed(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert validate_new_root(str(second), [str(first)]) == str(second)


def test_similar_prefixes_are_not_treated_as_nested(tmp_path: Path) -> None:
    """``/data-backup`` is not inside ``/data``, despite the string prefix."""
    data = tmp_path / "data"
    backup = tmp_path / "data-backup"
    data.mkdir()
    backup.mkdir()
    assert validate_new_root(str(backup), [str(data)]) == str(backup)


def test_defaults_are_non_empty() -> None:
    assert len(DEFAULT_EXCLUDES) > 5


# -- volume binding (docs/REVIEW.md F-01) -------------------------------------

def _catalog_with_root(tmp_path: Path, guid: str):
    from drivefusion.core.store.catalog import Catalog

    root = tmp_path / "Research"
    root.mkdir()
    catalog = Catalog(tmp_path / "cat" / "catalog.db")
    volume_id = catalog.upsert_volume(volume_guid=guid, label="ARCHIVE-A")
    catalog.add_scope_root(volume_id, str(root))
    return catalog, root


def _volume_at(guid: str, label: str = "BACKUP-B"):
    from drivefusion.core.discovery import VolumeInfo

    return lambda path: VolumeInfo(volume_guid=guid, label=label)


def test_a_root_on_its_catalogued_volume_is_bound(tmp_path: Path, monkeypatch) -> None:
    from drivefusion.core import discovery
    from drivefusion.core.scope import bind_roots

    catalog, _root = _catalog_with_root(tmp_path, "vol:A")
    monkeypatch.setattr(discovery, "identify_volume", _volume_at("vol:A", "ARCHIVE-A"))
    with catalog:
        binding = bind_roots(catalog, catalog.scope_roots())
    assert binding.refused == []
    assert len(binding.root_ids) == 1


def test_another_drive_at_the_same_path_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Drive B given drive A's letter must not be read as drive A."""
    from drivefusion.core import discovery
    from drivefusion.core.scope import bind_roots

    catalog, root = _catalog_with_root(tmp_path, "vol:A")
    monkeypatch.setattr(discovery, "identify_volume", _volume_at("vol:B"))
    with catalog:
        binding = bind_roots(catalog, catalog.scope_roots())
    assert binding.bound == []
    [(refused_root, reason)] = binding.refused
    assert refused_root.path == str(root)
    assert "vol:B" in reason and "vol:A" in reason
    assert "not the catalogued volume" in reason


def test_a_root_whose_volume_cannot_be_identified_is_refused(tmp_path: Path) -> None:
    """An unplugged drive: the path is gone, so nothing may be read there."""
    from drivefusion.core.scope import bind_roots

    catalog, root = _catalog_with_root(tmp_path, "vol:A")
    root.rmdir()
    with catalog:
        binding = bind_roots(catalog, catalog.scope_roots())
    assert binding.bound == []
    assert "is not there" in binding.refused[0][1]


def test_a_root_whose_volume_discovery_fails_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    from drivefusion.core import discovery
    from drivefusion.core.scope import bind_roots

    catalog, _root = _catalog_with_root(tmp_path, "vol:A")

    def fail(path):
        raise discovery.DiscoveryError("no volume mount point")

    monkeypatch.setattr(discovery, "identify_volume", fail)
    with catalog:
        binding = bind_roots(catalog, catalog.scope_roots())
    assert binding.bound == []
    assert "cannot identify the volume" in binding.refused[0][1]


def _windows_defaults():
    from drivefusion.core.scope.registry import DEFAULT_EXCLUDE_PREFIXES, ExclusionSet

    return ExclusionSet(prefixes=DEFAULT_EXCLUDE_PREFIXES)


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        (r"C:\Windows", True),
        (r"C:\Windows\System32\drivers", True),
        (r"c:\windows\temp", True),                        # case-insensitive
        (r"C:\WindowsBackups\2019", False),                # a folder of the user's
        (r"C:\Windows.old-photos", False),
        (r"C:\Program Files (x86)\App", True),
        (r"C:\Users\alice\AppData\Local\Temp", True),      # ROADMAP A3
        (r"C:\Users\alice\AppData\Local\Temp\xyz.dat", True),
        (r"E:\Users\bob\AppData\Local\Temp\old", True),    # a copied old profile
        (r"C:\Users\alice\AppData\Local\Templates", False),
        (r"C:\Users\alice\Documents\Temp", False),
        (r"D:\Research\Windows", False),
    ],
)
def test_default_prefixes_match_whole_folders(path: str, excluded: bool) -> None:
    assert _windows_defaults().excludes_absolute(path) is excluded
