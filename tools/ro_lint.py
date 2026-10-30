#!/usr/bin/env python3
"""Static guard: no module may acquire the ability to modify user data.

This is one of four complementary mechanisms behind the read-only guarantee
(docs/PLAN.md §3.2). It fails the build if any module outside the two permitted
writer packages so much as references a mutating filesystem call, launches a
process, or names a Win32 API that changes files.

Run it directly::

    python tools/ro_lint.py

It is also asserted from the test suite, so a developer who never runs it
locally still cannot merge past it.

Scope and honesty about limits
------------------------------
AST analysis cannot resolve arbitrary receivers: ``handle.write(...)`` where
``handle`` came from somewhere untrackable will not be caught here, and neither
will ``getattr(some_dll, computed_name)``. The other mechanisms overlap that gap
only in part — the gateway exposes no writer, OS handles are opened without
write access, and the no-touch test (with its audit hook) asserts fixture trees
are byte-identical after a full run — so the rules below err on the side of
flagging. REVIEW F-08 lists the classes this checker was extended to cover:
process launches, raw descriptor writes, ``io``/``codecs`` opens, numeric
``os.open`` flags, ``sqlite3.connect``, Win32 mutators, and name indirection.

Rules
-----
DF001  call to a mutating ``os``/``shutil``/``pathlib`` function
DF002  call to a method name with no non-mutating meaning
DF003  file opened with a mode other than ``"rb"`` (``open``, ``Path.open``,
       ``io.open``, ``io.FileIO``, ``os.fdopen``, ``codecs.open``)
DF004  write-intent ``os.O_*`` flag
DF005  import of a mutating name
DF006  write access right named in a raw-handle module
DF007  raw handle API outside a raw-handle module
DF008  process launch outside a module in ``PROCESS_LAUNCH_MODULES``
DF009  write to a raw descriptor (``os.write`` and relatives)
DF010  ``os.open`` flags that cannot be proven read-only
DF011  ``sqlite3.connect`` outside the catalog store
DF012  Win32 API that changes files, attributes, volumes or placeholders
DF013  name indirection (``getattr``/``__import__``) that defeats the rules above
DF014  a state-changing smartctl option named in a module that may start a
       process, outside a list assigned to a name ending ``FORBIDDEN``
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
    # Opens bundled executables read-only and share-read-only, so they can't be
    # swapped between being hashed and being run (REVIEW F-07, ROADMAP M5.3).
    "drivefusion/core/pinned.py",
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

#: Modules permitted to start another process, each with the reason it was
#: allowed. A process can do anything, including delete files, so every entry
#: is a deliberate review point; the reason is printed if the entry goes stale.
PROCESS_LAUNCH_MODULES = {
    "drivefusion/core/enum/helper/client.py": (
        "spawns dfscan-helper.exe with a fixed argv to enumerate a volume "
        "read-only (PLAN §6.5)"
    ),
    "drivefusion/core/health/smart.py": (
        "runs the bundled, hash-pinned smartctl with fixed read-only arguments "
        "(ROADMAP M5.3); DF014 forbids its state-changing options"
    ),
    "drivefusion/gui/elevation.py": (
        "ShellExecuteW('runas') of dfscan-helper.exe only, after a name check "
        "(PLAN §6.5; hardening tracked as REVIEW F-07)"
    ),
}

#: Ways to start a process. Flagged on reference, not only on call.
PROCESS_LAUNCH_OS_NAMES = frozenset(
    {
        "system", "popen", "startfile", "fork", "forkpty", "posix_spawn",
        "posix_spawnp", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
        "spawnve", "spawnvp", "spawnvpe", "execl", "execle", "execlp",
        "execlpe", "execv", "execve", "execvp", "execvpe",
    }
)
PROCESS_LAUNCH_WIN32 = frozenset(
    {
        "ShellExecuteW", "ShellExecuteA", "ShellExecuteExW", "ShellExecuteExA",
        "CreateProcessW", "CreateProcessA", "CreateProcessAsUserW",
        "CreateProcessWithLogonW", "CreateProcessWithTokenW", "WinExec",
    }
)

#: smartctl options that change a drive: self-tests, SMART on/off, offline and
#: autosave settings, --set, aborts, captive tests (ROADMAP M5.3, R-HL-04).
SMARTCTL_STATE_CHANGING = frozenset(
    {"-t", "--test", "-s", "--smart", "-o", "--offlineauto", "-S", "--saveauto",
     "--set", "-X", "--abort", "-C", "--captive"}
)

#: Writes through a descriptor bypass every mode check.
DESCRIPTOR_WRITERS = frozenset(
    {"write", "pwrite", "writev", "pwritev", "sendfile", "copy_file_range", "splice"}
)

#: Win32 entry points that change a file, its metadata, a directory, a volume,
#: or a cloud placeholder's hydration state. Matched as attribute names and as
#: exact string literals, so ``kernel32["DeleteFileW"]`` is caught too.
WIN32_MUTATORS = frozenset(
    {
        "DeleteFileW", "DeleteFileA", "MoveFileW", "MoveFileA", "MoveFileExW",
        "MoveFileExA", "MoveFileWithProgressW", "CopyFileW", "CopyFileA",
        "CopyFileExW", "CopyFile2", "ReplaceFileW", "ReplaceFileA",
        "SetFileTime", "SetFileAttributesW", "SetFileAttributesA",
        "SetFileInformationByHandle", "SetFileShortNameW", "SetFileValidData",
        "SetEndOfFile", "WriteFile", "WriteFileEx", "WriteFileGather",
        "RemoveDirectoryW", "RemoveDirectoryA", "CreateDirectoryW",
        "CreateDirectoryA", "CreateDirectoryExW", "CreateHardLinkW",
        "CreateHardLinkA", "CreateSymbolicLinkW", "CreateSymbolicLinkA",
        "DeleteVolumeMountPointW", "SetVolumeMountPointW", "SetVolumeLabelW",
        "SetVolumeLabelA", "DefineDosDeviceW", "EncryptFileW", "DecryptFileW",
        "SetFileSecurityW", "SetNamedSecurityInfoW", "NtWriteFile",
        "NtSetInformationFile", "NtDeleteFile", "ZwWriteFile",
        "ZwSetInformationFile", "SHFileOperationW", "SHFileOperationA",
        "FormatEx", "WofSetFileDataLocation", "CfHydratePlaceholder",
        "CfDehydratePlaceholder", "CfConvertToPlaceholder", "CfSetPinState",
        "CfUpdatePlaceholder",
    }
)

#: Modules whose names are tracked through ``import x as y``. Beyond the three
#: filesystem modules, these are the ones the rules above care about.
TRACKED_MODULES = frozenset({"os", "shutil", "pathlib", "io", "codecs", "subprocess", "sqlite3"})

#: Calls that open a file the way ``open()`` does, keyed by module.
OPEN_LIKE = {"io": {"open", "FileIO"}, "os": {"fdopen"}, "codecs": {"open"}}

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

#: The only file mode the tool ever opens user data with. ``io.FileIO``
#: spells binary read as ``"r"``.
ALLOWED_OPEN_MODES = frozenset({"rb"})
FILEIO_MODES = frozenset({"r", "rb"})


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
        permit_process_launch: bool = False,
        tree: ast.AST | None = None,
    ) -> None:
        self.path = path
        self.permit_raw_handles = permit_raw_handles
        self.permit_process_launch = permit_process_launch
        self.tree = tree
        # Innermost enclosing function first; the module is the fallback scope
        # when resolving a variable passed as ``os.open`` flags.
        self.scopes: list[ast.AST] = []
        self.in_forbidden_list = False
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
            if root in TRACKED_MODULES:
                self.module_aliases[alias.asname or root] = root
            if root == "subprocess":
                self._launch(node, "imports subprocess")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = (node.module or "").split(".")[0]
        for alias in node.names:
            local = alias.asname or alias.name
            if module == "subprocess":
                self._launch(node, f"imports subprocess.{alias.name}")
            if module in TRACKED_MODULES:
                self._check_module_attribute(node, module, alias.name, imported=True)
                self.imported_names[local] = (module, alias.name)
            if module in MODULE_MUTATORS:
                if alias.name in MODULE_MUTATORS[module]:
                    self._add(
                        node,
                        "DF005",
                        f"imports mutating name {module}.{alias.name}",
                    )
                if alias.name == "Path":
                    self.module_aliases[local] = "pathlib"
            if module == "os" and alias.name in WRITE_INTENT_FLAGS:
                self._add(
                    node, "DF004", f"imports write-intent flag os.{alias.name}"
                )
        self.generic_visit(node)

    # -- scopes ------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append(node)
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- string literals ----------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        forbids = any(isinstance(t, ast.Name) and t.id.endswith("FORBIDDEN")
                      for t in node.targets)
        previous, self.in_forbidden_list = self.in_forbidden_list, forbids or self.in_forbidden_list
        self.generic_visit(node)
        self.in_forbidden_list = previous

    def visit_Constant(self, node: ast.Constant) -> None:
        if (isinstance(node.value, str) and self.permit_process_launch
                and not self.in_forbidden_list
                and node.value.split("=", 1)[0] in SMARTCTL_STATE_CHANGING):
            self._add(
                node,
                "DF014",
                f"names smartctl option {node.value!r}, which changes a drive; "
                "smartctl is only ever given read-only arguments",
            )
        if isinstance(node.value, str):
            if node.value in WIN32_MUTATORS:
                self._win32_mutator(node, node.value)
            elif node.value in PROCESS_LAUNCH_WIN32:
                self._launch(node, f"names {node.value}")

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
            elif module:
                self._check_module_attribute(node, module, node.attr)
        if node.attr in WIN32_MUTATORS:
            self._win32_mutator(node, node.attr)
        elif node.attr in PROCESS_LAUNCH_WIN32:
            self._launch(node, f"uses {node.attr}")
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
            if func.id in self.imported_names:
                module, name = self.imported_names[func.id]
                if name in MODULE_MUTATORS.get(module, ()):  # pragma: no cover
                    self._add(
                        node, "DF001", f"calls mutating {module}.{name}"
                    )
                self._check_module_call(node, module, name)
            elif func.id == "open":
                self._check_open(node)
            elif func.id in RAW_HANDLE_APIS and not self.permit_raw_handles:
                self._add(
                    node,
                    "DF007",
                    f"uses raw handle API {func.id} outside a permitted module",
                )
            elif func.id in ("getattr", "__import__"):
                self._check_indirection(node)

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
                if module and func.attr in MODULE_MUTATORS.get(module, ()):
                    self._add(
                        node,
                        "DF001",
                        f"calls mutating {module}.{func.attr}()",
                    )
                elif module:
                    self._check_module_call(node, module, func.attr)
                elif func.value.id == "importlib" and func.attr == "import_module":
                    self._check_indirection(node)
            if func.attr == "open" and self._is_pathlib_receiver(func.value):
                self._check_open(node, qualified="Path.open")

        self.generic_visit(node)

    # -- the F-08 rules -----------------------------------------------------
    def _launch(self, node: ast.AST, what: str) -> None:
        if not self.permit_process_launch:
            self._add(
                node,
                "DF008",
                f"{what}: starting a process is permitted only in "
                "PROCESS_LAUNCH_MODULES, each entry reviewed",
            )

    def _win32_mutator(self, node: ast.AST, name: str) -> None:
        self._add(
            node,
            "DF012",
            f"names Win32 API {name}, which changes files, volumes or "
            "placeholders",
        )

    def _check_module_attribute(
        self, node: ast.AST, module: str, attr: str, imported: bool = False
    ) -> None:
        """Rules that apply to merely naming ``module.attr``."""
        if module == "os" and attr in PROCESS_LAUNCH_OS_NAMES:
            self._launch(node, f"uses os.{attr}")
        elif module == "os" and attr in DESCRIPTOR_WRITERS:
            self._add(
                node,
                "DF009",
                f"uses os.{attr}, which writes through a descriptor whatever "
                "it was opened with",
            )
        elif module == "sqlite3" and attr == "connect":
            self._add(
                node,
                "DF011",
                "uses sqlite3.connect, which creates a database and its -wal "
                "wherever it is pointed; only core/store opens databases",
            )

    def _check_module_call(self, node: ast.Call, module: str, name: str) -> None:
        if name in OPEN_LIKE.get(module, ()):
            self._check_open(node, qualified=f"{module}.{name}")
        elif module == "os" and name == "open":
            self._check_os_open(node)

    def _check_indirection(self, node: ast.Call) -> None:
        """Name indirection: ``getattr(os, name)`` is ``os.name`` unseen.

        ``__import__(x)`` and ``importlib.import_module(x)`` with a computed
        ``x`` are flagged outright. ``getattr`` on a tracked module is checked
        as the attribute it names when the name is a literal, and flagged when
        it is computed.
        """
        is_getattr = isinstance(node.func, ast.Name) and node.func.id == "getattr"
        if not is_getattr:
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                self._add(node, "DF013", "imports a module by a computed name")
            return

        if len(node.args) < 2 or not isinstance(node.args[0], ast.Name):
            return
        module = self.module_aliases.get(node.args[0].id)
        if module is None:
            return
        name_node = node.args[1]
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            self._add(
                node,
                "DF013",
                f"getattr({module}, <computed name>) hides which {module} "
                "function is used",
            )
            return
        name = name_node.value
        if name in MODULE_MUTATORS.get(module, ()):
            self._add(node, "DF001", f"uses mutating {module}.{name} via getattr")
        elif module == "os" and name in WRITE_INTENT_FLAGS:
            self._add(node, "DF004", f"references write-intent flag os.{name} via getattr")
        else:
            self._check_module_attribute(node, module, name)

    def _check_os_open(self, node: ast.Call) -> None:
        flags = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "flags":
                flags = keyword.value
        if flags is None or not self._flags_read_only(flags, set()):
            self._add(
                node,
                "DF010",
                "os.open() with flags that cannot be proven read-only; use "
                "os.O_* names (or getattr(os, 'O_NAME', 0)) joined with |",
            )

    def _flags_read_only(self, node: ast.AST, seen: set[str]) -> bool:
        """True when ``node`` is provably a combination of read-only flags.

        Numbers are refused (``0x241`` is ``O_WRONLY|O_CREAT|O_TRUNC``), and a
        variable is followed to every assignment of it in the enclosing
        function, or failing that the module.
        """
        if isinstance(node, ast.Constant):
            return node.value == 0 and not isinstance(node.value, bool)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return (
                self.module_aliases.get(node.value.id) == "os"
                and node.attr.startswith("O_")
                and node.attr not in WRITE_INTENT_FLAGS
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._flags_read_only(node.left, seen) and self._flags_read_only(
                node.right, seen
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) in (2, 3)
            and isinstance(node.args[0], ast.Name)
            and self.module_aliases.get(node.args[0].id) == "os"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            name = node.args[1].value
            default_ok = len(node.args) == 2 or self._flags_read_only(node.args[2], seen)
            return name.startswith("O_") and name not in WRITE_INTENT_FLAGS and default_ok
        if isinstance(node, ast.Name):
            if node.id in seen:
                return True     # a cycle adds nothing new; the other edges decide
            seen = seen | {node.id}
            values = self._assignments(node.id)
            return bool(values) and all(self._flags_read_only(v, seen) for v in values)
        return False

    def _assignments(self, name: str) -> list[ast.AST]:
        for scope in [*reversed(self.scopes), self.tree]:
            if scope is None:
                continue
            values: list[ast.AST] = []
            for child in ast.walk(scope):
                if isinstance(child, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == name for t in child.targets):
                        values.append(child.value)
                elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                    if isinstance(child.target, ast.Name) and child.target.id == name:
                        if child.value is None:
                            continue
                        if isinstance(child, ast.AugAssign) and not isinstance(
                            child.op, ast.BitOr
                        ):
                            values.append(ast.Constant(value=-1))   # unprovable
                        else:
                            values.append(child.value)
                elif isinstance(child, ast.arg) and child.arg == name:
                    values.append(ast.Constant(value=-1))   # a parameter: unknown
            if values:
                return values
        return []

    def _is_pathlib_receiver(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return self.module_aliases.get(node.func.id) == "pathlib"
        if isinstance(node, ast.Name):
            return self.module_aliases.get(node.id) == "pathlib"
        return False

    def _check_open(self, node: ast.Call, qualified: str = "open") -> None:
        mode_node: ast.AST | None = None
        # open(path, mode) / Path.open(mode) / io.open(path, mode) / ...
        positional = node.args if qualified == "Path.open" else node.args[1:]
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
            allowed = FILEIO_MODES if qualified == "io.FileIO" else ALLOWED_OPEN_MODES
            if mode_node.value not in allowed:
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
    visitor = _Visitor(
        rel_path,
        permit_raw_handles=permit_raw,
        permit_process_launch=rel_path in PROCESS_LAUNCH_MODULES,
        tree=tree,
    )
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
    for entry, reason in PROCESS_LAUNCH_MODULES.items():
        if not reason.strip():
            findings.append(
                Finding(
                    "tools/ro_lint.py", 0, 0, "DF000",
                    f"PROCESS_LAUNCH_MODULES entry {entry} gives no reason",
                )
            )
        if not (REPO_ROOT / entry).exists():
            findings.append(
                Finding(
                    "tools/ro_lint.py", 0, 0, "DF000",
                    f"PROCESS_LAUNCH_MODULES lists {entry} ({reason}), which "
                    "does not exist",
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
