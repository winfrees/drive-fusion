"""Tests for the read-only lint, in both directions.

A guard that silently matches nothing is worse than no guard, because it
produces confidence without coverage. So this file asserts both that the real
package is clean *and* that the checker actually catches each thing it claims
to catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import ro_lint

REPO_ROOT = Path(__file__).resolve().parent.parent

GUARDED = "drivefusion/core/scan/walker.py"


def codes(source: str, path: str = GUARDED) -> set[str]:
    return {finding.code for finding in ro_lint.check_source(path, source)}


# -- negative controls: each of these must be caught --------------------------

@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nos.remove(p)\n", "DF001"),
        ("import os\nos.rename(a, b)\n", "DF001"),
        ("import os\nos.replace(a, b)\n", "DF001"),
        ("import os as operating\noperating.remove(p)\n", "DF001"),
        ("import shutil\nshutil.move(a, b)\n", "DF001"),
        ("import shutil\nshutil.rmtree(p)\n", "DF002"),
        ("from pathlib import Path\nPath(p).unlink()\n", "DF002"),
        ("from pathlib import Path\nPath(p).write_bytes(b'')\n", "DF002"),
        ("target.chmod(0o644)\n", "DF002"),
        ("handle.truncate(0)\n", "DF002"),
        ("open(p, 'w')\n", "DF003"),
        ("open(p, 'a')\n", "DF003"),
        ("open(p, 'r+b')\n", "DF003"),
        ("open(p)\n", "DF003"),
        ("open(p, mode=chosen)\n", "DF003"),
        ("import os\nos.open(p, os.O_WRONLY)\n", "DF004"),
        ("import os\nos.open(p, os.O_CREAT | os.O_TRUNC)\n", "DF004"),
        ("from os import remove\n", "DF005"),
        ("from os import O_TRUNC\n", "DF004"),
        ("import ctypes\nctypes.windll.kernel32.CreateFileW(x)\n", "DF007"),
        ("DeviceIoControl(handle, code)\n", "DF007"),
        # REVIEW F-08: each of these passed with zero findings before.
        ("import subprocess\nsubprocess.run(['cmd', '/c', 'del', 'x'])\n", "DF008"),
        ("from subprocess import Popen\n", "DF008"),
        ("import os\nos.system('del x')\n", "DF008"),
        ("import os\nos.startfile(p)\n", "DF008"),
        ("import os\nos.popen('x')\n", "DF008"),
        ("from os import execv\n", "DF008"),
        ("import ctypes\nctypes.windll.shell32.ShellExecuteW(0, 'open', p)\n", "DF008"),
        ("import os\nos.write(3, b'x')\n", "DF009"),
        ("from os import pwrite\n", "DF009"),
        ("import io\nio.open(p, 'wb')\n", "DF003"),
        ("import io\nio.open(p)\n", "DF003"),
        ("import io\nio.FileIO(p, 'w')\n", "DF003"),
        ("from io import FileIO\nFileIO(p, mode='a')\n", "DF003"),
        ("import os\nos.fdopen(fd, 'w')\n", "DF003"),
        ("import codecs\ncodecs.open(p, 'w', 'utf-8')\n", "DF003"),
        ("import os\nos.open(p, 0x241)\n", "DF010"),
        ("import os\nos.open(p, os.O_RDONLY | 0x40)\n", "DF010"),
        ("import os\nos.open(p, flags=chosen)\n", "DF010"),
        ("import os\nflags = 0x241\nos.open(p, flags)\n", "DF010"),
        ("import os\ndef f(p, flags):\n    return os.open(p, flags)\n", "DF010"),
        ("import os\nf = os.O_RDONLY\nf += 1\nos.open(p, f)\n", "DF010"),
        ("import os\nos.open(p, getattr(os, 'O_WRONLY', 0))\n", "DF010"),
        ("import sqlite3\nsqlite3.connect('E:/data/x.db')\n", "DF011"),
        ("from sqlite3 import connect\n", "DF011"),
        ("import ctypes\nctypes.windll.kernel32.DeleteFileW('x')\n", "DF012"),
        ("kernel32.SetFileTime(h, None, None, None)\n", "DF012"),
        ("fn = kernel32['MoveFileExW']\n", "DF012"),
        ("cldapi.CfHydratePlaceholder(h, 0, -1, 0, None)\n", "DF012"),
        ("import os\ngetattr(os, 're' + 'move')('x')\n", "DF013"),
        ("import shutil\ngetattr(shutil, name)(a)\n", "DF013"),
        ("import os\ngetattr(os, 'remove')('x')\n", "DF001"),
        ("import os\ngetattr(os, 'system')('x')\n", "DF008"),
        ("__import__('o' + 's')\n", "DF013"),
        ("import importlib\nimportlib.import_module(name)\n", "DF013"),
    ],
)
def test_catches_mutating_code(source: str, expected: str) -> None:
    assert expected in codes(source), f"lint missed {expected} in: {source!r}"


def test_raw_handle_module_may_not_request_write_access() -> None:
    """fsio may open volume handles — but only for reading."""
    source = "access = GENERIC_WRITE\n"
    found = codes(source, path="drivefusion/core/fsio.py")
    assert "DF006" in found


# -- positive controls: ordinary read-only code must stay clean ---------------

@pytest.mark.parametrize(
    "source",
    [
        "import os\nfor entry in os.scandir(p):\n    pass\n",
        "import os\nst = os.stat(p)\n",
        "import os\nfd = os.open(p, os.O_RDONLY)\n",
        "open(p, 'rb')\n",
        "name = value.replace('a', 'b')\n",          # str.replace, not os.replace
        "items.remove(x)\n",                          # list.remove, not os.remove
        "config = defaults.copy()\n",                 # dict.copy, not shutil.copy
        "cursor.move(3)\n",                           # unrelated .move
        "import hashlib\nhashlib.sha256(data).hexdigest()\n",
        # The shapes core/fsio.py uses to open read-only, which must stay legal.
        "import os\nos.open(p, os.O_RDONLY | getattr(os, 'O_NOATIME', 0))\n",
        "import os\nflags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)\n"
        "noatime = getattr(os, 'O_NOATIME', 0)\nos.open(p, flags | noatime)\n",
        "import io\nio.FileIO(fd, mode='rb', closefd=True)\n",
        "import io\nio.open(p, 'rb')\n",
        "import os\nos.fdopen(fd, 'rb')\n",
        "import io\nbuffer = io.BytesIO()\nbuffer.write(b'x')\n",   # in memory
        "import sys\nsys.stdout.write('x')\n",
        "import os\nvalue = getattr(os, 'sep')\n",
        "label = 'DeleteFileW is never called'\n",                    # prose, not a name
        "import importlib\nimportlib.import_module('json')\n",
    ],
)
def test_leaves_read_only_code_alone(source: str) -> None:
    assert codes(source) == set(), f"false positive on: {source!r}"


def test_process_launch_is_permitted_only_where_allowlisted() -> None:
    source = "import subprocess\nsubprocess.Popen(argv)\n"
    for allowed in ro_lint.PROCESS_LAUNCH_MODULES:
        assert "DF008" not in codes(source, path=allowed)
    assert "DF008" in codes(source, path="drivefusion/core/identity/pipeline.py")


def test_allowlisted_launchers_are_still_held_to_every_other_rule() -> None:
    source = "import os\nos.remove(p)\nos.write(1, b'x')\n"
    for allowed in ro_lint.PROCESS_LAUNCH_MODULES:
        assert {"DF001", "DF009"} <= codes(source, path=allowed)


def test_every_process_launch_entry_gives_its_reason() -> None:
    for entry, reason in ro_lint.PROCESS_LAUNCH_MODULES.items():
        assert len(reason.split()) >= 5, f"{entry}: say why it may start a process"


def test_permitted_writers_are_exempt() -> None:
    source = "import os\nos.remove(p)\n"
    assert codes(source, path="drivefusion/core/store/catalog.py") == set()
    assert codes(source, path="drivefusion/core/export/bundle.py") == set()


# -- the real package ---------------------------------------------------------

def test_package_is_clean() -> None:
    findings = ro_lint.check_paths([REPO_ROOT / "drivefusion"])
    assert not findings, "read-only violations in drivefusion/:\n" + "\n".join(
        str(f) for f in findings
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlist pointing at a deleted module silently widens trust."""
    assert not ro_lint.check_config()


def test_cli_exits_non_zero_on_findings(tmp_path: Path, capsys) -> None:
    """CI relies on the exit code, so the exit code gets a test."""
    offender = tmp_path / "drivefusion" / "core" / "scan"
    offender.mkdir(parents=True)
    (offender / "bad.py").write_text("import shutil\nshutil.rmtree(p)\n")

    assert ro_lint.main([str(offender)]) == 1
    assert "DF002" in capsys.readouterr().out


def test_cli_exits_zero_on_the_real_package(capsys) -> None:
    assert ro_lint.main([str(REPO_ROOT / "drivefusion")]) == 0
    assert "clean" in capsys.readouterr().out


# -- DF014: smartctl only reads (ROADMAP M5.3, R-HL-04) --------------------------

SMART = "drivefusion/core/health/smart.py"


@pytest.mark.parametrize(
    "source",
    [
        "argv = ['smartctl', '-t', 'short', dev]\n",
        "argv = ['smartctl', '--test=long', dev]\n",
        "argv = [exe, '-s', 'off', dev]\n",
        "OPTIONS = ('--set=standby,now',)\n",
        "argv += ['-X']\n",
    ],
)
def test_state_changing_smartctl_options_are_refused(source: str) -> None:
    assert "DF014" in codes(source, path=SMART)


def test_the_list_that_forbids_them_may_name_them() -> None:
    assert "DF014" not in codes("SMARTCTL_FORBIDDEN = ('-t', '--test', '-s')\n", path=SMART)


def test_read_only_smartctl_arguments_are_fine() -> None:
    source = "argv = [exe, '--json=c', '-i', '-H', '-A', '-l', 'selftest', '-d', 'sat', dev]\n"
    assert codes(source, path=SMART) == set()


def test_df014_applies_only_where_processes_may_start() -> None:
    """Elsewhere '-t' is just a string; there it can't reach smartctl."""
    assert "DF014" not in codes("flags = ['-t']\n")
