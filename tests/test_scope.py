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
