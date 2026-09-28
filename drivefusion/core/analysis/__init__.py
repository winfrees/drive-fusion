"""Redundancy, duplication, and integrity analysis."""

from drivefusion.core.analysis.browse import (
    count_files_in_dir,
    child_dirs,
    dir_summary,
    drive_inventory,
    files_in_dir,
)
from drivefusion.core.analysis.duplicates import (
    ContentGroup,
    content_paths,
    duplicate_groups,
    integrity_incidents,
    reclamation_candidates,
    redundancy_summary,
    under_protected,
)

__all__ = [
    "child_dirs", "count_files_in_dir", "dir_summary", "drive_inventory",
    "files_in_dir",
    "ContentGroup", "content_paths", "duplicate_groups", "integrity_incidents",
    "reclamation_candidates", "redundancy_summary", "under_protected",
]
