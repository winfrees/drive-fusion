"""REVIEW F-07: the elevation launcher proves what it elevates (ROADMAP B1)."""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

from drivefusion.gui import elevation
from drivefusion.gui.elevation import (
    Decision,
    ElevationError,
    ElevationState,
    check_helper,
    helper_parameters,
    request,
)

HELPER = "dfscan-helper.exe"


def _helper(tmp_path: Path, body: bytes = b"MZ genuine helper") -> tuple[Path, str]:
    path = tmp_path / HELPER
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


# -- the trust decision ----------------------------------------------------------

def test_a_pinned_build_elevates_only_the_helper_it_shipped(tmp_path: Path) -> None:
    path, digest = _helper(tmp_path)
    check_helper(path, digest, pinned=digest, writable=True)       # writable is fine: proven
    check_helper(path, digest.upper(), pinned=digest, writable=False)
    with pytest.raises(ElevationError, match="not the helper this build shipped"):
        check_helper(path, "0" * 64, pinned=digest, writable=False)


def test_an_unpinned_build_refuses_a_helper_anyone_could_have_replaced(tmp_path: Path) -> None:
    path, digest = _helper(tmp_path)
    with pytest.raises(ElevationError, match="can be written to"):
        check_helper(path, digest, pinned=None, writable=True)
    check_helper(path, digest, pinned=None, writable=False)        # e.g. under Program Files


def test_the_pin_is_read_from_the_module_the_build_packs(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "drivefusion_helper_pin", raising=False)
    assert elevation.pinned_helper_sha256() is None, "a source checkout has no pin"
    module = types.ModuleType("drivefusion_helper_pin")
    module.HELPER_SHA256 = "AB" * 32
    monkeypatch.setitem(sys.modules, "drivefusion_helper_pin", module)
    assert elevation.pinned_helper_sha256() == "ab" * 32


# -- request() refuses before any prompt --------------------------------------

def _prompt_state(path: Path) -> ElevationState:
    return ElevationState(Decision.CAN_PROMPT, path, "")


def test_a_tampered_helper_is_refused_before_the_prompt(tmp_path: Path, monkeypatch) -> None:
    path, digest = _helper(tmp_path, b"MZ replaced by something else")
    monkeypatch.setattr(elevation, "pinned_helper_sha256", lambda: "1" * 64)
    with pytest.raises(ElevationError, match="not the helper this build shipped"):
        request(_prompt_state(path), ["--volume", "C:"])


def test_an_unpinned_helper_in_a_writable_folder_is_refused(tmp_path: Path, monkeypatch) -> None:
    path, _ = _helper(tmp_path)
    monkeypatch.setattr(elevation, "pinned_helper_sha256", lambda: None)
    with pytest.raises(ElevationError, match="can be written to"):
        request(_prompt_state(path))


# -- parameters ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--volume", "\\\\?\\Volume{1234}", "--mode", "enum"],
         "--volume \\\\?\\Volume{1234} --mode enum"),
        (["--label", "My Passport"], '--label "My Passport"'),        # a space stays one argument
        (["--label", 'say "hi"'], '--label "say \\"hi\\""'),          # quotes are escaped
        (["--path", "C:\\dir with space\\"], '--path "C:\\dir with space\\\\"'),
    ],
)
def test_parameters_are_quoted_for_the_command_line(arguments, expected) -> None:
    assert helper_parameters(arguments) == expected


def test_no_arguments_means_no_parameter_string() -> None:
    assert helper_parameters(None) is None and helper_parameters([]) is None


# -- the lock, where it means something ---------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="share modes are a Windows feature")
def test_the_helper_cannot_be_changed_while_it_is_being_checked(tmp_path: Path) -> None:
    import os

    path, _ = _helper(tmp_path)
    with elevation._locked(path) as handle:
        assert handle.read(2) == b"MZ"
        with pytest.raises(PermissionError):
            with open(path, "r+b"):
                pass
        with pytest.raises(PermissionError):
            os.replace(path, tmp_path / "moved.exe")
    with open(path, "r+b"):        # released afterwards
        pass
