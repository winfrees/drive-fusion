"""Authenticode signing, when a certificate is configured (ROADMAP M8.8, D21)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import sign  # noqa: E402


def test_unconfigured_signs_nothing_and_says_so(capsys) -> None:
    calls = []
    assert sign.sign(["a.exe"], environ={}, run=lambda *a, **k: calls.append(a)) is False
    assert calls == [] and "unsigned" in capsys.readouterr().out


def test_configured_signs_with_sha256_and_a_timestamp_then_verifies(monkeypatch) -> None:
    monkeypatch.setattr(sign, "signtool", lambda: "signtool.exe")
    calls = []
    env = {"DRIVEFUSION_SIGN_PFX": "c.pfx", "DRIVEFUSION_SIGN_PASSWORD": "pw"}
    assert sign.sign(["a.exe", "b.exe"], environ=env,
                     run=lambda argv, check: calls.append(argv)) is True
    signing, verify = calls
    assert signing[:2] == ["signtool.exe", "sign"]
    assert signing[signing.index("/fd") + 1] == "SHA256" and "/tr" in signing
    assert signing[-2:] == ["a.exe", "b.exe"]
    assert verify == ["signtool.exe", "verify", "/pa", "a.exe", "b.exe"]


def test_version_says_when_a_frozen_build_is_unsigned(monkeypatch) -> None:
    import types

    from drivefusion.cli import main

    assert main.build_note() == ""                      # from source: nothing to say
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "drivefusion_helper_pin",
                        types.SimpleNamespace(SIGNED=False))
    assert main.build_note() == " (unsigned build)"
    monkeypatch.setitem(sys.modules, "drivefusion_helper_pin",
                        types.SimpleNamespace(SIGNED=True))
    assert main.build_note() == " (signed)"


def test_the_spec_signs_the_helper_before_pinning_it() -> None:
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "drivefusion.spec").read_text()
    assert spec.index("sign.sign([helper_exe.name])") < spec.index("HELPER_SHA256 = hashlib")
