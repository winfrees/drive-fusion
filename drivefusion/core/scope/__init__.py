"""User-defined scan scope. Nothing is catalogued until it is added here."""

from drivefusion.core.scope.binding import RootBinding, bind_roots, check_root
from drivefusion.core.scope.registry import (
    DEFAULT_EXCLUDES,
    ExclusionSet,
    ScopeError,
    validate_new_root,
)

__all__ = [
    "DEFAULT_EXCLUDES",
    "ExclusionSet",
    "RootBinding",
    "ScopeError",
    "bind_roots",
    "check_root",
    "validate_new_root",
]
