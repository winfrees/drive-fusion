"""Scope rules: what may be catalogued, and what is skipped by default.

The default exclusions are not tidiness. At 10–50 million files, not
enumerating ten million pieces of operating-system churn is a performance
feature as much as a scoping one (docs/PLAN.md §4).
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Iterable, Sequence

#: Name patterns skipped on every scope, matched case-insensitively at any
#: depth. These are churn, not content.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "$RECYCLE.BIN",
    "$Recycle.Bin",
    "System Volume Information",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "DumpStack.log*",
    "*.tmp",
    "~$*",                       # Office lock files
    ".Trash-*",
    ".Spotlight-V100",
    "$WinREAgent",
    "Config.Msi",
)

#: Absolute prefixes skipped by default on Windows. Kept separate from name
#: patterns because "Windows" as a bare name would exclude a legitimate folder
#: called Windows anywhere on an archive drive.
DEFAULT_EXCLUDE_PREFIXES: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData\Package Cache",
    r"C:\$WinREAgent",
)


class ScopeError(ValueError):
    """Raised when a scope root is invalid or conflicts with an existing one."""


@dataclass(frozen=True)
class ExclusionSet:
    """Name and path patterns that remove entries from a scan."""

    name_patterns: tuple[str, ...] = DEFAULT_EXCLUDES
    path_patterns: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()

    @classmethod
    def for_root(
        cls, user_patterns: Sequence[str] = (), *, use_defaults: bool = True
    ) -> "ExclusionSet":
        """Combine the defaults with a root's own patterns.

        A pattern containing a separator is treated as a path pattern matched
        against the path relative to the root; otherwise it matches names at
        any depth.
        """
        names = list(DEFAULT_EXCLUDES) if use_defaults else []
        paths: list[str] = []
        for pattern in user_patterns:
            if "/" in pattern or "\\" in pattern:
                paths.append(pattern.replace("\\", "/"))
            else:
                names.append(pattern)
        prefixes = DEFAULT_EXCLUDE_PREFIXES if use_defaults and os.name == "nt" else ()
        return cls(tuple(names), tuple(paths), tuple(prefixes))

    def excludes_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(
            fnmatch.fnmatchcase(lowered, pattern.lower())
            for pattern in self.name_patterns
        )

    def excludes_relpath(self, relpath: str) -> bool:
        normalised = relpath.replace("\\", "/").lower()
        return any(
            fnmatch.fnmatchcase(normalised, pattern.lower())
            for pattern in self.path_patterns
        )

    def excludes_absolute(self, path: str) -> bool:
        lowered = path.lower()
        return any(lowered.startswith(prefix.lower()) for prefix in self.prefixes)

    def excludes(self, *, name: str, relpath: str = "", absolute: str = "") -> bool:
        return (
            self.excludes_name(name)
            or (bool(relpath) and self.excludes_relpath(relpath))
            or (bool(absolute) and self.excludes_absolute(absolute))
        )


def normalise_root(path: str) -> str:
    """Absolute, normalised, without a trailing separator."""
    absolute = os.path.abspath(os.path.expanduser(path))
    if len(absolute) > 1:
        absolute = absolute.rstrip("\\/")
    return absolute


def _is_within(child: str, parent: str) -> bool:
    child_key = child.lower() if os.name == "nt" else child
    parent_key = parent.lower() if os.name == "nt" else parent
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key.rstrip("\\/") + os.sep)


def validate_new_root(new_path: str, existing: Iterable[str]) -> str:
    """Return the normalised root, or raise if it is unusable or nested.

    Nesting is rejected rather than merged because a directory must belong to
    exactly one scope root: ``dir.root_id`` is what scopes tombstoning, so a
    path reachable from two roots would make "this file is gone" ambiguous
    (docs/PLAN.md §7.2, ``Catalog._mark_vanished``).
    """
    root = normalise_root(new_path)
    if not os.path.isdir(root):
        raise ScopeError(f"not a directory: {root}")

    for other in existing:
        other_root = normalise_root(other)
        if _is_within(root, other_root):
            raise ScopeError(
                f"{root} is already covered by scope root {other_root}"
            )
        if _is_within(other_root, root):
            raise ScopeError(
                f"{root} contains existing scope root {other_root}; remove the "
                "narrower root first"
            )
    return root
