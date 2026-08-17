"""Path reconstruction from parent file-reference numbers."""

from __future__ import annotations

import pytest

from drivefusion.core.enum.pathtree import (
    MAX_DEPTH,
    NTFS_ROOT_FRN,
    FrnTree,
    PathTreeError,
    build_dir_tree,
)
from drivefusion.core.enum.records import parse_usn_records
from tests.fixtures import winbuf

DIRECTORY = winbuf.FILE_ATTRIBUTE_DIRECTORY


def volume_tree() -> FrnTree:
    tree = FrnTree()
    tree.add_root(NTFS_ROOT_FRN, "")
    tree.add(100, NTFS_ROOT_FRN, "Research")
    tree.add(200, 100, "2024")
    tree.add(300, 200, "imaging")
    return tree


def test_resolves_a_nested_path() -> None:
    tree = volume_tree()
    assert tree.components(300) == ("Research", "2024", "imaging")
    assert tree.path(300) == "Research\\2024\\imaging"
    assert tree.path(300, "/") == "Research/2024/imaging"


def test_root_resolves_to_empty() -> None:
    assert volume_tree().components(NTFS_ROOT_FRN) == ()


def test_self_parenting_directory_is_a_root() -> None:
    """NTFS marks the volume root by making it its own parent."""
    tree = FrnTree(root_frns=())
    tree.add(5, 5, "")
    tree.add(9, 5, "data")
    assert tree.is_root(5)
    assert tree.components(9) == ("data",)


def test_orphans_are_reported_not_invented() -> None:
    """A file under an unknown directory must not be given a fabricated path."""
    tree = FrnTree()
    tree.add_root(NTFS_ROOT_FRN, "")
    tree.add(400, 999, "detached")  # parent 999 was never seen

    assert tree.components(400) is None
    assert tree.path(400) is None
    assert tree.orphans() == [400]


def test_cycles_are_detected() -> None:
    """A corrupt journal can produce a loop; it must not hang the scan."""
    tree = FrnTree()
    tree.add(1, 2, "a")
    tree.add(2, 1, "b")
    with pytest.raises(PathTreeError, match="cycle"):
        tree.components(1)


def test_unreasonable_depth_is_treated_as_corruption() -> None:
    # No declared root and no self-parent, so nothing legitimately stops the
    # walk; frns start well above NTFS_ROOT_FRN so the chain cannot pass
    # through the real root and terminate early.
    tree = FrnTree(root_frns=())
    for index in range(MAX_DEPTH + 10):
        tree.add(1000 + index, 1001 + index, f"d{index}")
    with pytest.raises(PathTreeError, match="exceeded"):
        tree.components(1000)


def test_rename_replaces_the_earlier_name() -> None:
    """Deltas deliver renames as a new record for a known frn."""
    tree = volume_tree()
    tree.add(200, 100, "2024-archived")
    assert tree.components(300) == ("Research", "2024-archived", "imaging")


def test_reparent_moves_the_whole_subtree() -> None:
    tree = volume_tree()
    tree.add(500, NTFS_ROOT_FRN, "Elsewhere")
    tree.add(200, 500, "2024")  # moved
    assert tree.components(300) == ("Elsewhere", "2024", "imaging")


def test_topological_order_puts_parents_first() -> None:
    tree = volume_tree()
    seen: set[int] = set()
    for node in tree.iter_topological():
        if node.frn != NTFS_ROOT_FRN and not tree.is_root(node.frn):
            assert node.parent_frn in seen, (
                f"{node.name} came before its parent"
            )
        seen.add(node.frn)
    assert len(seen) == 4


def test_topological_order_omits_orphans() -> None:
    tree = volume_tree()
    tree.add(400, 999, "detached")
    emitted = {node.frn for node in tree.iter_topological()}
    assert 400 not in emitted
    assert tree.orphans() == [400]


def test_depths_are_reported() -> None:
    tree = volume_tree()
    assert tree.depth(NTFS_ROOT_FRN) == 0
    assert tree.depth(100) == 1
    assert tree.depth(300) == 3


def test_builds_from_a_usn_stream_keeping_only_directories() -> None:
    """The memory claim in the plan, asserted: files are not retained."""
    buffer = winbuf.usn_buffer(
        [
            winbuf.usn_record_v2(frn=100, parent_frn=NTFS_ROOT_FRN,
                                 name="Research", attributes=DIRECTORY),
            winbuf.usn_record_v2(frn=200, parent_frn=100, name="2024",
                                 attributes=DIRECTORY),
        ]
        + [
            winbuf.usn_record_v2(frn=1000 + i, parent_frn=200, name=f"f{i}.raw")
            for i in range(50)
        ]
    )
    stream = parse_usn_records(buffer, offset=8)
    tree, file_count = build_dir_tree(stream)
    tree.add_root(NTFS_ROOT_FRN, "")

    assert file_count == 50
    # Two directories plus the root marker; none of the 50 files are retained,
    # which is the memory property the whole design depends on.
    assert len(tree) == 3
    assert tree.components(200) == ("Research", "2024")
