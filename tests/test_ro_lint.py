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
        # DF002 rather than DF001: `rename` is now banned on any receiver, so
        # `Path(p).rename(q)` is caught too, and the any-receiver rule is the
        # one that reports first.
        ("import os\nos.rename(a, b)\n", "DF002"),
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
        # DF008: an external process can change a drive in ways no
        # filesystem-call rule sees. `smartctl -t long` starts a multi-hour
        # self-test; `smartctl -s off` disables SMART. Neither is a write().
        ("import subprocess\nsubprocess.run(['smartctl', '-t', 'long'])\n", "DF008"),
        ("import subprocess\nsubprocess.Popen(argv)\n", "DF008"),
        ("import subprocess\nsubprocess.check_output(argv)\n", "DF008"),
        ("import os\nos.system('smartctl -t long /dev/sda')\n", "DF008"),
        ("import os\nos.popen(cmd)\n", "DF008"),
        ("import os\nos.execv(path, argv)\n", "DF008"),
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


STORE = "drivefusion/core/store/catalog.py"
EXPORT = "drivefusion/core/export/bundle.py"
EXPORT_WRITER = "drivefusion/core/export/writer.py"


def test_a_writer_package_may_create_files() -> None:
    """Being the one place allowed to write is the point of the exemption."""
    assert codes("open(p, 'wb')\n", path=STORE) == set()
    assert codes("open(p, 'xb')\n", path=EXPORT_WRITER) == set()
    assert codes("import os\nos.makedirs(p)\n", path=STORE) == set()
    assert codes(
        "import pathlib\npathlib.Path(p).mkdir(parents=True)\n", path=EXPORT_WRITER
    ) == set()


def test_a_writer_package_still_may_not_destroy_or_move() -> None:
    """§3.2 says the export package has no delete function.

    Until this rule existed nothing enforced it: a permitted writer was exempt
    from every check, so ``shutil.rmtree`` in core/export would have passed a
    clean lint while the guarantee still claimed the package could not delete.
    """
    for source in (
        "import os\nos.remove(p)\n",
        "import os\nos.unlink(p)\n",
        "import os\nos.rename(a, b)\n",
        "import os\nos.replace(a, b)\n",
        "import os\nos.truncate(p, 0)\n",
        "import shutil\nshutil.rmtree(p)\n",
        "import shutil\nshutil.move(a, b)\n",
        "import pathlib\npathlib.Path(p).unlink()\n",
        "import pathlib\npathlib.Path(p).rename(q)\n",
    ):
        for path in (STORE, EXPORT, EXPORT_WRITER):
            assert "DF009" in codes(source, path=path), (
                f"not caught in {path}: {source!r}"
            )


def test_the_export_package_may_not_copy_a_users_file() -> None:
    """Copying the archive is the user's job. A copy routine here would be the
    one plausible way bytes of user data end up somewhere the tool put them."""
    for name in ("copy", "copy2", "copyfile", "copytree", "make_archive"):
        source = f"import shutil\nshutil.{name}(a, b)\n"
        assert "DF009" in codes(source, path=EXPORT), name
        assert "DF009" in codes(source, path=EXPORT_WRITER), name

    # The store package copies Drive Fusion's own catalog aside before a schema
    # migration, which is how that migration is made safe.
    assert codes("import shutil\nshutil.copy2(a, b)\n", path=STORE) == set()


def test_only_the_entrypoint_may_create_a_file_in_the_export_package() -> None:
    """Every export write must pass the inside-the-root and does-not-exist
    checks, and a second module calling open() would skip both."""
    for source in (
        "open(p, 'wb')\n",
        "import pathlib\npathlib.Path(p).write_text(s)\n",
        "import pathlib\npathlib.Path(p).write_bytes(b)\n",
        "import pathlib\npathlib.Path(p).touch()\n",
        "import os\nos.makedirs(p)\n",
    ):
        assert "DF010" in codes(source, path=EXPORT), source
        assert "DF010" not in codes(source, path=EXPORT_WRITER), source
        # The rule is about the export package specifically.
        assert "DF010" not in codes(source, path=STORE), source


def test_a_writer_package_does_not_get_false_positives_on_ordinary_code() -> None:
    """The trap the existing denylist warns about, in the new rules too:
    ``replace`` is a str method and ``copy`` is a dict method, and a lint that
    cannot tell them apart is a lint that gets switched off."""
    for source in (
        "name = value.replace('a', 'b')\n",
        "config = defaults.copy()\n",
        "items.remove(x)\n",
        "rows = list(cursor)\nrows.remove(rows[0])\n",
        "entry = dict(row)\nentry.update(extra)\n",
        "text = template.format(x=1)\n",
    ):
        for path in (STORE, EXPORT, EXPORT_WRITER):
            assert codes(source, path=path) == set(), f"{path}: {source!r}"


def test_a_writer_package_may_not_start_a_process() -> None:
    """Being allowed to write files is not being allowed to run programs."""
    assert "DF008" in codes(
        "import subprocess\nsubprocess.run(['x'])\n", path=EXPORT
    )


# -- the real package ---------------------------------------------------------

def test_package_is_clean() -> None:
    findings = ro_lint.check_paths([REPO_ROOT / "drivefusion"])
    assert not findings, "read-only violations in drivefusion/:\n" + "\n".join(
        str(f) for f in findings
    )


def test_the_subprocess_allowlist_permits_its_own_modules() -> None:
    """The modules that must spawn a process still may.

    A guard that blocked the enumeration helper or smartctl outright would be
    removed within a week, so the exemption is asserted as deliberately as the
    rule.
    """
    source = "import subprocess\nsubprocess.run(argv)\n"
    for permitted in ro_lint.PERMITTED_SUBPROCESS_MODULES:
        assert "DF008" not in {
            f.code for f in ro_lint.check_source(permitted, source)
        }, f"{permitted} is on the allowlist but was still flagged"


def test_subprocess_is_flagged_outside_the_allowlist() -> None:
    source = "import subprocess\nsubprocess.run(['smartctl', '-t', 'long'])\n"
    codes_found = {
        f.code
        for f in ro_lint.check_source("drivefusion/core/scan/scanner.py", source)
    }
    assert "DF008" in codes_found


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


# -- gaps found by probing the checker rather than reading it -----------------

@pytest.mark.parametrize(
    "source",
    [
        # The receiver is a call, so the qualified pathlib rule never saw it.
        "from pathlib import Path\nPath(p).rename(q)\n",
        "import pathlib\npathlib.Path(p).rename(q)\n",
        # The receiver is a plain variable, which is how path handling is
        # actually written.
        "p = get_path()\np.rename(q)\n",
        "target = base / name\ntarget.rename(q)\n",
        # touch() creates a file and stamps an existing one.
        "from pathlib import Path\nPath(p).touch()\n",
        # Path.replace overwrites its target. Told from str.replace by arity.
        "from pathlib import Path\nPath(p).replace(q)\n",
        "target = base / name\ntarget.replace(other)\n",
    ],
)
def test_catches_a_mutating_call_on_a_path_that_is_not_a_module_attribute(
    source: str,
) -> None:
    """These all modify catalogued media and none of them was caught.

    The qualified rules need the receiver to be a name bound to ``os``,
    ``shutil`` or ``pathlib``, which ``Path(p)`` and ``target`` are not — so
    every natural way of writing path code slipped past. Found by running the
    checker against candidate sources, not by reading it.
    """
    assert codes(source), f"not caught: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        # The mode check required a recognisable pathlib receiver, so a Path
        # held in a variable could be opened for writing unnoticed. This is the
        # likeliest accidental reintroduction of a write path in the codebase.
        "t = get_path()\nt.open('w')\n",
        "t = get_path()\nt.open('wb')\n",
        "t = get_path()\nt.open('a')\n",
        "t = get_path()\nwith t.open('r+') as handle:\n    pass\n",
        "t = get_path()\nt.open()\n",
        "t = get_path()\nt.open(mode)\n",
        # os.fdopen can wrap a descriptor into a writable handle, and was not
        # checked at all.
        "import os\nos.fdopen(fd, 'w')\n",
        "import os\nos.fdopen(fd)\n",
    ],
)
def test_every_open_has_its_mode_checked_whatever_it_is_called_on(
    source: str,
) -> None:
    assert "DF003" in codes(source), f"unchecked open: {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        # str.replace takes two arguments; Path.replace takes one. The arity is
        # what tells them apart, and str.replace is everywhere in this codebase.
        "name = text.replace('a', 'b')\n",
        "s = text.replace(old, new)\n",
        "s = text.replace(old, new, 1)\n",
        # A read-only open on a path in a variable stays fine.
        "t = get_path()\nt.open('rb')\n",
        # os.open takes flags, not a mode string, and has its own rule.
        "import os\nfd = os.open(p, os.O_RDONLY)\n",
        # Unrelated methods that happen to share a name.
        "frame = frame.copy()\n",
        "sock = listener.open\n",
    ],
)
def test_the_new_rules_do_not_fire_on_ordinary_code(source: str) -> None:
    """A checker with false positives gets switched off, and then none of the
    four mechanisms in §3.2 is three."""
    assert codes(source) == set(), f"false positive: {source!r}"


def test_os_open_write_flags_are_still_caught() -> None:
    """The flags rule must survive os.open being excluded from the mode check."""
    assert codes("import os\nos.open(p, os.O_WRONLY)\n")
    assert codes("import os\nos.open(p, os.O_RDONLY | os.O_CREAT)\n")
