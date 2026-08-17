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
    def __init__(self, path: str, permit_raw_handles: bool) -> None:
        self.path = path
        self.permit_raw_handles = permit_raw_handles
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
            if func.id == "open":
                self._check_open(node)
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

        elif isinstance(func, ast.Attribute):
            if func.attr in ALWAYS_BANNED:
                self._add(
                    node,
                    "DF002",
                    f"calls mutating method {func.attr}(); Drive Fusion never "
                    "modifies catalogued media",
                )
            elif isinstance(func.value, ast.Name):
                module = self.module_aliases.get(func.value.id)
                if module and func.attr in MODULE_MUTATORS[module]:
                    self._add(
                        node,
                        "DF001",
                        f"calls mutating {module}.{func.attr}()",
                    )
            if func.attr == "open" and self._is_pathlib_receiver(func.value):
                self._check_open(node, qualified="Path.open")

        self.generic_visit(node)

    def _is_pathlib_receiver(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return self.module_aliases.get(node.func.id) == "pathlib"
        if isinstance(node, ast.Name):
            return self.module_aliases.get(node.id) == "pathlib"
        return False

    def _check_open(self, node: ast.Call, qualified: str = "open") -> None:
        mode_node: ast.AST | None = None
        # open(path, mode) / Path.open(mode)
        positional = node.args[1:] if qualified == "open" else node.args
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
        return []
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return [Finding(rel_path, exc.lineno or 0, exc.offset or 0, "DF999", str(exc))]

    permit_raw = rel_path in RAW_HANDLE_MODULES
    visitor = _Visitor(rel_path, permit_raw_handles=permit_raw)
    visitor.visit(tree)
    findings = visitor.findings

    if permit_raw:
        findings.extend(_check_write_access_tokens(rel_path, source))
    return findings


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
    for entry in RAW_HANDLE_MODULES:
        if not (REPO_ROOT / entry).exists():
            findings.append(
                Finding(
                    "tools/ro_lint.py", 0, 0, "DF000",
                    f"RAW_HANDLE_MODULES lists {entry}, which does not exist",
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
