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
    ],
)
def test_leaves_read_only_code_alone(source: str) -> None:
    assert codes(source) == set(), f"false positive on: {source!r}"


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
