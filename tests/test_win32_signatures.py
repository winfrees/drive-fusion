"""REVIEW F-14: every Win32 call that takes or returns a handle is declared.

With ctypes' default ``c_int`` return type a failed ``CreateFileW`` returns -1,
which never equals ``INVALID_HANDLE_VALUE`` (``c_void_p(-1).value``), so an
"access denied" surfaces later as "invalid handle"; and a 64-bit handle can't
be passed back in as an argument. These tests run on any platform: a stand-in
``WinDLL`` records what each module declares.
"""

from __future__ import annotations

import ctypes
import importlib
import sys

import pytest

wintypes = pytest.importorskip("ctypes.wintypes")

#: name -> (positions of handle arguments, returns a handle)
HANDLE_CALLS = {
    "CreateFileW": ((6,), True),
    "CloseHandle": ((0,), False),
    "DeviceIoControl": ((0,), False),
    "GetFileInformationByHandleEx": ((0,), False),
    "FindFirstVolumeW": ((), True),
    "FindNextVolumeW": ((0,), False),
    "FindVolumeClose": ((0,), False),
}


class _Function:
    def __init__(self) -> None:
        self.argtypes = None
        self.restype = ctypes.c_int      # ctypes' own default


class FakeWinDLL:
    def __init__(self, *_args, **_kwargs) -> None:
        self._functions: dict[str, _Function] = {}

    def __getattr__(self, name: str) -> _Function:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._functions.setdefault(name, _Function())


def _assert_declared(lib: FakeWinDLL, names: list[str]) -> None:
    for name in names:
        function = lib._functions.get(name)
        assert function is not None, f"{name} is not declared"
        positions, returns_handle = HANDLE_CALLS[name]
        assert function.argtypes is not None, f"{name}: no argtypes"
        for position in positions:
            assert function.argtypes[position] is wintypes.HANDLE, (
                f"{name}: argument {position} is not a HANDLE"
            )
        if returns_handle:
            assert function.restype is wintypes.HANDLE, f"{name}: restype is not HANDLE"
        else:
            assert function.restype is wintypes.BOOL, f"{name}: restype is not BOOL"


def test_winio_declares_every_handle_call(monkeypatch) -> None:
    from drivefusion.core.enum import winio

    monkeypatch.setattr(ctypes, "WinDLL", FakeWinDLL, raising=False)
    monkeypatch.setattr(winio, "WINDOWS", True)
    winio.kernel32.cache_clear()
    try:
        lib = winio.kernel32()
        _assert_declared(
            lib, ["CreateFileW", "CloseHandle", "DeviceIoControl", "GetFileInformationByHandleEx"]
        )
        assert winio.kernel32() is lib, "one declared instance, shared"
    finally:
        winio.kernel32.cache_clear()


def test_discovery_declares_every_handle_call(monkeypatch) -> None:
    import drivefusion.core.discovery as package

    monkeypatch.setattr(ctypes, "WinDLL", FakeWinDLL, raising=False)
    name = "drivefusion.core.discovery.windows"
    saved_module = sys.modules.pop(name, None)
    # ``from drivefusion.core.discovery import windows`` reads the package
    # attribute first, so it must be restored too, or later tests (on Windows,
    # real discovery) would be handed the module loaded against the fake.
    missing = object()
    saved_attribute = package.__dict__.get("windows", missing)
    try:
        module = importlib.import_module(name)
        _assert_declared(
            module.kernel32,
            ["CreateFileW", "CloseHandle", "DeviceIoControl", "FindFirstVolumeW",
             "FindNextVolumeW", "FindVolumeClose"],
        )
    finally:
        sys.modules.pop(name, None)
        if saved_module is not None:
            sys.modules[name] = saved_module
        if saved_attribute is missing:
            package.__dict__.pop("windows", None)
        else:
            package.windows = saved_attribute


def test_a_failed_open_now_matches_invalid_handle_value() -> None:
    """The comparison the declarations exist for (REVIEW R9)."""
    failed = wintypes.HANDLE(ctypes.c_void_p(-1).value).value
    assert failed == ctypes.c_void_p(-1).value
