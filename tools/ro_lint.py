#!/usr/bin/env python3
"""Static guard: no module may acquire the ability to modify user data.

This is one of the four independent mechanisms behind the read-only guarantee
(docs/PLAN.md §3.2). It fails the build if any module outside the two permitted
writer packages so much as references a mutating filesystem call.

Run it directly::

    python tools/ro_lint.py

It is also asserted from the test suite, so a developer who never runs it
locally still cannot merge past it.

Scope and honesty about limits
------------------------------
AST analysis cannot resolve arbitrary receivers: ``handle.write(...)`` where
``handle`` came from somewhere untrackable will not be caught here. That gap is
deliberate and covered by the other three mechanisms — the gateway exposes no
writer, OS handles are opened without write access, and the no-touch test
asserts fixture trees are byte-identical after a full run. This checker is the
cheap, fast layer that catches the realistic mistake: someone reaching for
``os.remove`` or ``shutil.move`` in a scanning code path.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Packages allowed to write, and allowed only to their own targets: the
#: catalog database and the user-chosen export directory.
PERMITTED_WRITER_PREFIXES = (
    "drivefusion/core/store",
    "drivefusion/core/export",
)

#: Calls a writer package may not make either (DF009).
#:
#: Being allowed to *create* files is not being allowed to destroy or move
#: them. §3.2 says the export package "has no delete function"; until this rule
#: existed nothing enforced that, because a permitted writer was exempt from
#: every check and could have grown an ``shutil.rmtree`` without the lint
#: noticing. A writer may bring files into existence and nothing else.
WRITER_BANNED = {
    "os": frozenset(
        {
            "remove", "unlink", "rmdir", "removedirs", "rename", "renames",
            "replace", "truncate", "ftruncate", "chmod", "chown", "utime",
        }
    ),
    "shutil": frozenset({"rmtree", "move", "copystat", "copymode", "unpack_archive"}),
    "pathlib": frozenset(
        {"unlink", "rmdir", "rename", "replace", "chmod", "lchmod"}
    ),
}

#: Method names banned in a writer package whatever they are called on.
#:
#: Unambiguous names only, the same discipline ALWAYS_BANNED follows: ``remove``
#: is also a list method, ``replace`` and ``copy`` are str and dict methods, and
#: ``rename`` belongs to half the libraries in Python. Those are caught by
#: qualified name through WRITER_BANNED instead, so that ``text.replace("a", "b")``
#: inside an export module is not reported as a filesystem write.
WRITER_BANNED_METHODS = frozenset(
    {
        "rmtree", "rmdir", "removedirs", "unlink", "copystat", "copymode",
        "ftruncate", "chmod", "lchmod", "fchmod", "chown", "fchown", "utime",
        "unpack_archive", "removexattr", "lremovexattr", "renames", "rename",
    }
)

#: Copying is banned in the export package and allowed in the store package.
#:
#: The distinction is whose file it is. ``core/store`` copies Drive Fusion's own
#: catalog aside before a schema migration, which is how that migration is made
#: safe. ``core/export`` writes documents it generated and must never open a
#: user's file for any purpose: copying the archive is the user's job, from a
#: checklist, with their own tools. A copy routine here would also be the one
#: plausible way bytes of user data could end up somewhere the tool put them.
#: Matched as ``shutil.<name>`` only, never as a bare ``.copy()``: a dict copy
#: is not a file copy, and a lint that cannot tell them apart gets ignored.
EXPORT_BANNED_COPIERS = frozenset(
    {"copy", "copy2", "copyfile", "copytree", "copyfileobj", "make_archive"}
)

#: The one module in the export package permitted to create a file (DF010).
#:
#: §3.2 promises that *every* write routes through a single function asserting
#: the target is inside the chosen root and does not already exist. A second
#: module calling ``open`` or ``Path.write_text`` directly would be a write
#: that skipped those assertions while the guarantee still claimed otherwise.
EXPORT_WRITE_ENTRYPOINT = "drivefusion/core/export/writer.py"

#: Ways to create a file, banned everywhere in the export package but its
#: entrypoint.
EXPORT_FILE_CREATORS = frozenset(
    {"open", "write_text", "write_bytes", "touch", "mkdir", "makedirs", "mkstemp"}
)

#: Modules permitted to call raw Win32 handle APIs. Enumeration backends join
#: this list at M2; each addition is a deliberate review point.
RAW_HANDLE_MODULES = (
    "drivefusion/core/fsio.py",
    "drivefusion/core/discovery/windows.py",
    "drivefusion/core/enum/winio.py",
)

#: Win32 access rights that would make a handle capable of writing. Their mere
#: appearance in a raw-handle module is a finding.
WRITE_ACCESS_TOKENS = (
    "GENERIC_WRITE",
    "GENERIC_ALL",
    "FILE_GENERIC_WRITE",
    "FILE_WRITE_DATA",
    "FILE_APPEND_DATA",
    "FILE_WRITE_ATTRIBUTES",
    "FILE_WRITE_EA",
    "MAXIMUM_ALLOWED",
)

RAW_HANDLE_APIS = ("CreateFileW", "CreateFileA", "DeviceIoControl")

#: Method names with no plausible non-mutating meaning. Banned on any receiver.
ALWAYS_BANNED = frozenset(
    {
        "rmtree", "rmdir", "removedirs", "makedirs", "unlink",
        "copytree", "copyfile", "copystat", "copymode", "copy2",
        "write_text", "write_bytes", "mkfifo", "mknod",
        "symlink_to", "hardlink_to", "lchmod", "chmod", "chown",
        "fchmod", "fchown", "utime", "truncate", "ftruncate",
        "renames", "setxattr", "removexattr", "lremovexattr",
        "make_archive", "unpack_archive",
        # Added after probing the checker rather than reading it: a rename is a
        # modification of catalogued media, and ``Path(p).rename(q)`` was
        # caught by nothing — the qualified rule needs the receiver to be a
        # name bound to the module, which ``pathlib.Path(p)`` is not. Neither
        # name collides with a common method of another type.
        "rename", "touch",
    }
)

#: Names that are only mutating when called on a filesystem module. ``remove``
#: is also a list method and ``replace`` is also a string method, so these are
#: flagged by qualified name to keep the checker free of false positives.
MODULE_MUTATORS = {
    "os": frozenset(
        {
            "remove", "unlink", "rmdir", "removedirs", "rename", "renames",
            "replace", "truncate", "ftruncate", "mkdir", "makedirs", "chmod",
            "chown", "utime", "link", "symlink", "mknod", "mkfifo",
        }
    ),
    "shutil": frozenset(
        {
            "rmtree", "move", "copy", "copy2", "copyfile", "copytree",
            "copystat", "copymode", "make_archive", "unpack_archive",
        }
    ),
    "pathlib": frozenset(
        {
            "unlink", "rmdir", "mkdir", "rename", "replace", "write_text",
            "write_bytes", "touch", "chmod", "lchmod", "symlink_to",
            "hardlink_to",
        }
    ),
}

#: Open flags that request the ability to create, write, or truncate.
WRITE_INTENT_FLAGS = frozenset(
    {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_EXCL", "O_TMPFILE"}
)

#: The only file mode the tool ever opens user data with.
ALLOWED_OPEN_MODES = frozenset({"rb"})

#: Functions that open a file and take a mode. ``os.fdopen`` was missing, and
#: with it an ``os.open`` on a descriptor could have been wrapped into a
#: writable text handle without the mode ever being checked.
OPEN_LIKE = frozenset({"open", "fdopen"})

#: Modules permitted to start an external process, and what each one runs.
#:
#: An external program can change a drive in ways no filesystem-call rule would
#: notice: ``smartctl -t long`` starts a multi-hour self-test, ``smartctl -s
#: off`` disables SMART. Neither is a write() and neither would trip DF001-DF007,
#: so process creation is confined to modules that constrain their own arguments
#: — each of these holds an allowlist of the flags it will pass.
PERMITTED_SUBPROCESS_MODULES = (
    "drivefusion/core/enum/helper/client.py",   # spawns dfscan-helper, read-only
    "drivefusion/core/health/smart.py",         # spawns smartctl, read-only flags
    "drivefusion/core/discovery/windows.py",    # volume discovery
)

#: Ways to start a process. Spelled out rather than matched on the module name
#: alone, because `os.system` and `Popen` arrive by several routes.
PROCESS_STARTERS = frozenset(
    {
        "system",
        "popen",
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "spawn",
        "spawnl",
        "spawnv",
        "execv",
        "execvp",
        "fork",
        "posix_spawn",
    }
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: {self.code} {self.message}"


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        path: str,
        permit_raw_handles: bool,
        permit_subprocess: bool = False,
        writer_mode: bool = False,
    ) -> None:
        self.path = path
        self.permit_raw_handles = permit_raw_handles
        self.permit_subprocess = permit_subprocess
        #: A package allowed to create files. Creation passes; destruction,
        #: moving and re-permissioning still do not.
        self.writer_mode = writer_mode
        self.is_export = path.startswith("drivefusion/core/export")
        self.is_export_entrypoint = path == EXPORT_WRITE_ENTRYPOINT
        self.findings: list[Finding] = []
        # alias -> canonical module name
        self.module_aliases: dict[str, str] = {}
        # names imported directly from a filesystem module
        self.imported_names: dict[str, str] = {}

    def _add(self, node: ast.AST, code: str, message: str) -> None:
        self.findings.append(
            Finding(self.path, node.lineno, node.col_offset, code, message)
        )

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in MODULE_MUTATORS:
                self.module_aliases[alias.asname or alias.name] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = (node.module or "").split(".")[0]
        for alias in node.names:
            local = alias.asname or alias.name
            if module in MODULE_MUTATORS:
                if alias.name in MODULE_MUTATORS[module]:
                    self._add(
                        node,
                        "DF005",
                        f"imports mutating name {module}.{alias.name}",
                    )
                self.imported_names[local] = module
                if alias.name == "Path":
                    self.module_aliases[local] = "pathlib"
            if module == "os" and alias.name in WRITE_INTENT_FLAGS:
                self._add(
                    node, "DF004", f"imports write-intent flag os.{alias.name}"
                )
        self.generic_visit(node)

    # -- attribute references ---------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            module = self.module_aliases.get(node.value.id)
            if module == "os" and node.attr in WRITE_INTENT_FLAGS:
                self._add(
                    node,
                    "DF004",
                    f"references write-intent flag os.{node.attr}; "
                    "catalogued media is opened read-only",
                )
        if node.attr in RAW_HANDLE_APIS and not self.permit_raw_handles:
            self._add(
                node,
                "DF007",
                f"uses raw handle API {node.attr} outside a permitted module "
                f"({', '.join(RAW_HANDLE_MODULES)})",
            )
        self.generic_visit(node)

    # -- calls -------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Name):
            if func.id in OPEN_LIKE:
                if not self.writer_mode:
                    self._check_open(node, qualified=func.id)
            elif func.id in RAW_HANDLE_APIS and not self.permit_raw_handles:
                self._add(
                    node,
                    "DF007",
                    f"uses raw handle API {func.id} outside a permitted module",
                )
            elif func.id in self.imported_names:
                module = self.imported_names[func.id]
                if func.id in MODULE_MUTATORS.get(module, ()):  # pragma: no cover
                    self._add(
                        node, "DF001", f"calls mutating {module}.{func.id}"
                    )

        if isinstance(func, ast.Name) and self.writer_mode:
            self._check_writer_creation(node, func.id)

        elif isinstance(func, ast.Attribute):
            if (
                func.attr in PROCESS_STARTERS
                and isinstance(func.value, ast.Name)
                and func.value.id in ("subprocess", "os")
                and not self.permit_subprocess
            ):
                self._add(
                    node,
                    "DF008",
                    f"starts an external process ({func.value.id}.{func.attr}); "
                    "a subprocess can change a drive in ways the filesystem "
                    "rules cannot see. Confine it to a module that allowlists "
                    "its own arguments (see PERMITTED_SUBPROCESS_MODULES)",
                )
            banned_methods = (
                WRITER_BANNED_METHODS if self.writer_mode else ALWAYS_BANNED
            )
            mutators = WRITER_BANNED if self.writer_mode else MODULE_MUTATORS
            code = "DF009" if self.writer_mode else "DF002"

            module = (
                self.module_aliases.get(func.value.id)
                if isinstance(func.value, ast.Name)
                else None
            )

            if func.attr in banned_methods:
                self._add(
                    node,
                    code,
                    f"calls mutating method {func.attr}(); "
                    + (
                        "a writer package may only bring files into existence"
                        if self.writer_mode
                        else "Drive Fusion never modifies catalogued media"
                    ),
                )
            elif module and func.attr in mutators.get(module, ()):
                self._add(
                    node,
                    "DF009" if self.writer_mode else "DF001",
                    f"calls mutating {module}.{func.attr}()"
                    + (
                        "; a writer package may only bring files into existence"
                        if self.writer_mode
                        else ""
                    ),
                )
            elif (
                self.is_export
                and module == "shutil"
                and func.attr in EXPORT_BANNED_COPIERS
            ):
                self._add(
                    node,
                    "DF009",
                    f"calls shutil.{func.attr}(); the export package writes "
                    "only documents it generated and never opens a user's "
                    "file. Copying the archive is the user's job, from a "
                    "checklist, with their own tools",
                )

            if self.writer_mode and func.attr in EXPORT_FILE_CREATORS:
                self._check_writer_creation(node, func.attr)
            elif func.attr in OPEN_LIKE and not (
                func.attr == "open"
                and isinstance(func.value, ast.Name)
                and self.module_aliases.get(func.value.id) == "os"
            ):
                # os.open takes flags, not a mode string, and has its own rule
                # (WRITE_INTENT_FLAGS). os.fdopen does take a mode, so it stays.
                # Any receiver, not only one recognisable as a Path. The check
                # used to require the receiver to be a name bound to pathlib,
                # so `target = Path(x); target.open("w")` passed — the single
                # most likely way a write path gets reintroduced by accident.
                receiver = (
                    func.value.id if isinstance(func.value, ast.Name) else "…"
                )
                self._check_open(
                    node, qualified=f"{receiver}.{func.attr}", drops_path=True
                )
            elif func.attr == "replace" and self._is_path_replace(node):
                self._add(
                    node,
                    "DF009" if self.writer_mode else "DF002",
                    "calls replace() with one argument, which is "
                    "Path.replace() — it overwrites the target. str.replace "
                    "takes two arguments and is not flagged",
                )

        self.generic_visit(node)

    def _check_writer_creation(self, node: ast.Call, name: str) -> None:
        """In the export package, only the single entrypoint may create a file.

        §3.2 promises that *every* write routes through one function that
        checks the target is inside the chosen root and does not already exist.
        A second module calling ``open`` or ``Path.write_text`` would be a
        write that skipped both checks while the guarantee still claimed
        otherwise.
        """
        if not self.is_export or self.is_export_entrypoint:
            return
        if name not in EXPORT_FILE_CREATORS:
            return
        self._add(
            node,
            "DF010",
            f"{name}() creates a file outside {EXPORT_WRITE_ENTRYPOINT}; every "
            "export write must go through writer.write(), which checks the "
            "target is inside the chosen root and does not already exist",
        )

    def _is_path_replace(self, node: ast.Call) -> bool:
        """Whether a ``.replace()`` call is ``Path.replace``, not ``str.replace``.

        Told apart by arity, which is exact: ``str.replace`` needs the old and
        the new substring, ``Path.replace`` takes the target alone. The name
        could not simply be banned — ``text.replace("a", "b")`` appears all
        over this codebase — and leaving it unchecked left a call that silently
        overwrites a file.
        """
        return len(node.args) == 1 and not node.keywords

    def _is_pathlib_receiver(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return self.module_aliases.get(node.func.id) == "pathlib"
        if isinstance(node, ast.Name):
            return self.module_aliases.get(node.id) == "pathlib"
        return False

    def _check_open(
        self, node: ast.Call, qualified: str = "open", drops_path: bool = False
    ) -> None:
        mode_node: ast.AST | None = None
        # open(path, mode) and os.fdopen(fd, mode) take the mode second;
        # Path.open(mode) and file-like .open(mode) take it first.
        positional = node.args if drops_path else node.args[1:]
        if positional:
            mode_node = positional[0]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value

        if mode_node is None:
            self._add(
                node,
                "DF003",
                f"{qualified}() without an explicit mode; catalogued media is "
                'opened as "rb" through drivefusion.core.fsio',
            )
            return
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            if mode_node.value not in ALLOWED_OPEN_MODES:
                self._add(
                    node,
                    "DF003",
                    f'{qualified}() with mode {mode_node.value!r}; only "rb" '
                    "is permitted outside the store and export packages",
                )
            return
        self._add(
            node,
            "DF003",
            f"{qualified}() with a non-literal mode, which cannot be proven "
            "read-only",
        )


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def is_permitted_writer(rel_path: str) -> bool:
    return rel_path.startswith(PERMITTED_WRITER_PREFIXES)


def check_source(rel_path: str, source: str) -> list[Finding]:
    """Check one module's source. Exposed for the checker's own tests."""
    if is_permitted_writer(rel_path):
        # Not exempt, narrowed: a writer may create files and may not destroy,
        # move or re-permission them, and in the export package only the single
        # write entrypoint may create one at all.
        return check_writer_source(rel_path, source)
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return [Finding(rel_path, exc.lineno or 0, exc.offset or 0, "DF999", str(exc))]

    permit_raw = rel_path in RAW_HANDLE_MODULES
    permit_subprocess = rel_path in PERMITTED_SUBPROCESS_MODULES
    visitor = _Visitor(
        rel_path,
        permit_raw_handles=permit_raw,
        permit_subprocess=permit_subprocess,
    )
    visitor.visit(tree)
    findings = visitor.findings

    if permit_raw:
        findings.extend(_check_write_access_tokens(rel_path, source))
    return findings


def check_writer_source(rel_path: str, source: str) -> list[Finding]:
    """The narrowed check for a package allowed to write (DF009, DF010).

    Runs the same visitor as everything else, with the name sets swapped: a
    writer may create files, and may not destroy, move or re-permission them.
    Reusing the visitor rather than walking the tree separately matters because
    it is the visitor that tracks import aliases, and without that the check
    cannot tell ``os.remove(p)`` from ``items.remove(x)``.
    """
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return [Finding(rel_path, exc.lineno or 0, exc.offset or 0, "DF999", str(exc))]

    visitor = _Visitor(
        rel_path,
        permit_raw_handles=rel_path in RAW_HANDLE_MODULES,
        permit_subprocess=rel_path in PERMITTED_SUBPROCESS_MODULES,
        writer_mode=True,
    )
    visitor.visit(tree)
    return visitor.findings


def _qualified_name(node: ast.AST) -> str:
    """A dotted name for a call target, as far back as it can be read."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _check_write_access_tokens(rel_path: str, source: str) -> list[Finding]:
    """A raw-handle module must never name a write access right.

    Checked against source text rather than the AST so that a constant hidden
    in a string or a comment is caught too — in a module that opens volume
    handles, the presence of the token at all is worth a human looking.
    """
    findings: list[Finding] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for token in WRITE_ACCESS_TOKENS:
            col = line.find(token)
            if col >= 0:
                findings.append(
                    Finding(
                        rel_path,
                        lineno,
                        col,
                        "DF006",
                        f"raw-handle module names write access right {token}; "
                        "handles must request read access only",
                    )
                )
    return findings


def check_paths(roots: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for root in roots:
        files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for file in files:
            rel = _relative(file)
            findings.extend(check_source(rel, file.read_text(encoding="utf-8")))
    return findings


def check_config() -> list[Finding]:
    """Fail on stale configuration: an allowlist entry that no longer exists.

    An allowlist pointing at a deleted module is a silent widening of trust the
    next time someone recreates that path.
    """
    findings = []
    for name, entries in (
        ("RAW_HANDLE_MODULES", RAW_HANDLE_MODULES),
        ("PERMITTED_SUBPROCESS_MODULES", PERMITTED_SUBPROCESS_MODULES),
        ("PERMITTED_WRITER_PREFIXES", PERMITTED_WRITER_PREFIXES),
        ("EXPORT_WRITE_ENTRYPOINT", (EXPORT_WRITE_ENTRYPOINT,)),
    ):
        for entry in entries:
            if not (REPO_ROOT / entry).exists():
                findings.append(
                    Finding(
                        "tools/ro_lint.py", 0, 0, "DF000",
                        f"{name} lists {entry}, which does not exist",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        default=["drivefusion"],
        help="files or directories to check (default: drivefusion)",
    )
    args = parser.parse_args(argv)

    roots = [Path(p) for p in args.paths]
    missing = [p for p in roots if not p.exists()]
    if missing:
        print(f"ro_lint: no such path: {missing[0]}", file=sys.stderr)
        return 2

    findings = check_config() + check_paths(roots)
    for finding in findings:
        print(finding)

    checked = sum(len(list(r.rglob("*.py"))) if r.is_dir() else 1 for r in roots)
    if findings:
        print(
            f"\nro_lint: {len(findings)} finding(s) across {checked} file(s). "
            "Drive Fusion must not be able to modify catalogued media.",
            file=sys.stderr,
        )
        return 1
    print(f"ro_lint: clean ({checked} files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
