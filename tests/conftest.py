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
