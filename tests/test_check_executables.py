"""The build's executables: distinct names, and the right PE subsystem each (tools/check_executables.py)."""

import importlib.util
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_executables", ROOT / "tools" / "check_executables.py")
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _pe(subsystem: int) -> bytes:
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 24 + 68, subsystem)
    return bytes(data)


def test_spec_names_differ_by_more_than_case():
    """Windows compares file names without case: two such names are one file."""
    spec = (ROOT / "packaging" / "drivefusion.spec").read_text(encoding="utf-8")
    names = re.findall(r'^\s+name="([^"]+)",', spec, re.M)
    exes = names[:-1]                       # the last is the COLLECT folder
    assert len(exes) == len(ce.EXPECTED)
    assert len({n.lower() for n in exes}) == len(exes), exes
    assert {f"{n}.exe" for n in exes} == set(ce.EXPECTED)


def test_a_good_build_passes(tmp_path):
    for name, kind in ce.EXPECTED.items():
        (tmp_path / name).write_bytes(_pe(kind))
    assert ce.check(str(tmp_path)) == []


def test_the_overwritten_window_is_caught(tmp_path):
    """What every build before 2026-11-01 shipped: the console program under the window's name."""
    (tmp_path / "DriveFusion.exe").write_bytes(_pe(ce.CONSOLE))
    (tmp_path / "dfscan-helper.exe").write_bytes(_pe(ce.CONSOLE))
    problems = ce.check(str(tmp_path))
    assert "DriveFusion.exe is console, not windowed" in problems
    assert any(p.startswith("drivefusion-cli.exe is missing") for p in problems)


def test_a_name_in_the_wrong_case_is_missing(tmp_path):
    for name, kind in ce.EXPECTED.items():
        (tmp_path / (name.lower() if name == "DriveFusion.exe" else name)).write_bytes(_pe(kind))
    assert ce.check(str(tmp_path)) == ["DriveFusion.exe is missing (found driveFusion.exe)".replace("driveFusion", "drivefusion")]


def test_not_an_executable():
    with pytest.raises(ValueError):
        ce.subsystem(b"hello")
