"""The version each release gets, and stamping it into a build (tools/release_version.py)."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_version", ROOT / "tools" / "release_version.py")
rv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rv)


@pytest.mark.parametrize("base, tags, head, expected", [
    ("0.1.0", [], [], "0.1.0"),                                   # the first release
    ("0.1.0", ["v0.1.0"], [], "0.1.1"),
    ("0.1.0", ["v0.1.0", "v0.1.1", "v0.1.9"], [], "0.1.10"),       # numeric, not text, order
    ("0.1.0", ["v0.1.3", "v0.0.7", "v1.0.0"], [], "0.1.4"),        # other series don't count
    ("0.2.0", ["v0.1.0", "v0.1.1"], [], "0.2.0"),                  # a new minor series
    ("0.1.5", ["v0.1.2"], [], "0.1.5"),                            # never below the base
    ("0.1.0", ["v0.1.0", "v0.1.1-rc1", "nightly"], [], "0.1.1"),   # only vX.Y.Z tags
    ("0.1.0", ["v0.1.0", "v0.1.1"], ["v0.1.1"], "0.1.1"),          # a re-run keeps its tag
])
def test_next_version(base, tags, head, expected):
    assert rv.next_version(base, tags, head) == expected


def test_the_source_has_a_base_version():
    assert rv._VERSION.match(rv.base_version())


def test_stamp_rewrites_every_file_that_names_the_version(tmp_path, monkeypatch):
    stamps = []
    for path, pattern, replacement in rv.STAMPS:
        copy = tmp_path / path.name
        shutil.copy(path, copy)
        stamps.append((copy, pattern, replacement))
    monkeypatch.setattr(rv, "STAMPS", stamps)
    rv.stamp("3.4.5")
    init, pyproject, manifest = (p.read_text(encoding="utf-8") for p, _, _ in stamps)
    assert '__version__ = "3.4.5"' in init
    assert 'version = "3.4.5"' in pyproject
    assert 'name="DriveFusion" version="3.4.5.0"' in manifest
    with pytest.raises(ValueError):
        rv.stamp("3.4")
