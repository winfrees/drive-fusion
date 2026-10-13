"""The export writer: the only thing in Drive Fusion that creates a file.

Every assertion here is about a way an export could reach outside the folder
the user chose, or destroy something already in it. Those are the only two ways
this package could do harm, so they are the only things these tests are about.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from drivefusion.core.export import writer
from drivefusion.core.export.writer import ExportError
from drivefusion.core.scan import scan_root
from drivefusion.core.store.catalog import Catalog


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return writer.prepare_root(tmp_path / "exports")


# -- containment --------------------------------------------------------------

@pytest.mark.parametrize(
    "relative",
    [
        "../escaped.txt",
        "../../escaped.txt",
        "sub/../../escaped.txt",
        "a/b/../../../escaped.txt",
    ],
)
def test_a_path_cannot_climb_out_of_the_bundle(root: Path, relative: str) -> None:
    """Either guard may catch this — the explicit ``..`` rejection or the
    containment check on the resolved path. Both are deliberate, so the test
    accepts either message and asserts the thing that matters: no file appeared
    outside the bundle.
    """
    with pytest.raises(ExportError) as raised:
        writer.write(root, relative, "x")
    message = str(raised.value)
    assert "climbs out" in message or "outside the export root" in message
    assert not (root.parent / "escaped.txt").exists()
    assert not (root.parent.parent / "escaped.txt").exists()


def test_an_absolute_path_is_refused_rather_than_reinterpreted(root: Path) -> None:
    """Silently treating it as relative would write somewhere the caller did
    not ask for; silently honouring it would write outside the bundle.

    Asserted on a distinctive phrase rather than with ``match="absolute"``.
    That version passed with the check removed, because pytest builds its
    temporary directory from the *test's own name* — which contains the word
    "absolute" — and the fallback error message quotes the export root path.
    The test was matching its own name.
    """
    for relative in ("/etc/passwd", str(root.parent / "elsewhere.txt")):
        with pytest.raises(ExportError) as raised:
            writer.write(root, relative, "x")
        assert "is an absolute path" in str(raised.value), (
            f"refused for the wrong reason: {raised.value}"
        )


def test_a_symlinked_component_cannot_redirect_a_write(
    root: Path, tmp_path: Path
) -> None:
    """The check is on the resolved path, because a string prefix test is
    exactly what a symlink defeats."""
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow the symlink")

    with pytest.raises(ExportError, match="outside the export root"):
        writer.write(root, "link/sneaky.txt", "x")
    assert not (outside / "sneaky.txt").exists()


def test_a_sibling_directory_with_a_shared_prefix_is_still_outside(
    tmp_path: Path,
) -> None:
    """``/data/archive-old`` is not inside ``/data/archive``, though it looks
    like it is to a string comparison."""
    assert not writer._within(tmp_path / "archive-old", tmp_path / "archive")
    assert writer._within(tmp_path / "archive" / "sub", tmp_path / "archive")
    assert writer._within(tmp_path / "archive", tmp_path / "archive")


def test_a_nested_path_inside_the_bundle_is_fine(root: Path) -> None:
    target = writer.write(root, "data/metadata/record.json", "{}")
    assert target.read_text() == "{}"
    assert writer._within(target, root)


# -- never overwriting --------------------------------------------------------

def test_writing_over_an_existing_file_is_refused(root: Path) -> None:
    """There is no overwrite parameter. A bundle is a new directory, so a
    collision is a bug in whatever composed it, and replacing the file would
    hide that bug."""
    writer.write(root, "report.txt", "first")
    with pytest.raises(ExportError, match="already exists"):
        writer.write(root, "report.txt", "second")
    assert (root / "report.txt").read_text() == "first"


def test_the_writer_has_no_way_to_remove_or_replace_anything() -> None:
    """Not "none is exposed": none exists. Asserted over the module's surface
    so adding one is a visible decision rather than a quiet convenience."""
    surface = [name for name in dir(writer) if not name.startswith("_")]
    for forbidden in (
        "delete", "remove", "unlink", "rmtree", "rmdir", "overwrite",
        "replace", "move", "rename", "truncate", "copy", "clear",
    ):
        assert not any(forbidden in name.lower() for name in surface), (
            f"the writer exposes {forbidden!r}: {surface}"
        )


def test_the_file_is_opened_exclusively_so_a_race_cannot_defeat_the_check(
    root: Path,
) -> None:
    """The existence check and the open are two steps; the ``xb`` mode makes
    the second one refuse as well, so there is no window between them."""
    source = Path(writer.__file__).read_text()
    assert 'open(target, "xb")' in source, (
        "the writer must open exclusively, not truncate"
    )
    assert 'open(target, "wb")' not in source


# -- a fresh directory every time ---------------------------------------------

def test_each_export_gets_its_own_directory(tmp_path: Path) -> None:
    first = writer.prepare_root(tmp_path / "exports")
    second = writer.prepare_root(tmp_path / "exports")
    assert first != second
    assert first.exists() and second.exists()


def test_a_second_export_in_the_same_second_does_not_join_the_first(
    tmp_path: Path, monkeypatch
) -> None:
    """Timestamped names collide at one-second resolution, and joining an
    existing bundle would mean a later export could overwrite an earlier one."""
    roots = {writer.prepare_root(tmp_path / "exports") for _ in range(4)}
    assert len(roots) == 4


def test_the_root_is_created_and_was_not_there_before(tmp_path: Path) -> None:
    base = tmp_path / "exports"
    assert not base.exists()
    root = writer.prepare_root(base)
    assert root.is_dir()
    assert not any(root.iterdir()), "a fresh bundle starts empty"


# -- not into the archive -----------------------------------------------------

def test_an_export_cannot_be_written_inside_a_scanned_tree(tmp_path: Path) -> None:
    """The one realistic way this tool could write to user media is the user
    choosing their archive drive as the destination."""
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "a.bin").write_bytes(b"a" * 50)

    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        volume_id = catalog.upsert_volume(volume_guid="export:1")
        root_id = catalog.add_scope_root(volume_id, str(media))
        scan_root(
            catalog, root_path=str(media), root_id=root_id, volume_id=volume_id
        )

        with pytest.raises(ExportError, match="inside the scanned tree"):
            writer.prepare_root(media / "exports", catalog=catalog)
        with pytest.raises(ExportError, match="inside the scanned tree"):
            writer.prepare_root(media, catalog=catalog)

        # And nothing was created in the attempt.
        assert sorted(p.name for p in media.iterdir()) == ["sub"]


def test_a_bundle_beside_the_archive_is_a_reasonable_choice(tmp_path: Path) -> None:
    """Only one direction of containment matters.

    ``D:\\exports`` next to ``D:\\Research`` touches nothing in the archive,
    and refusing it would be a false alarm on an ordinary setup. The inverse
    check — a scanned tree inside the bundle — was written and then removed as
    unreachable: a bundle is always a new empty directory.
    """
    media = tmp_path / "shared" / "media"
    media.mkdir(parents=True)
    (media / "a.bin").write_bytes(b"a" * 10)

    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        volume_id = catalog.upsert_volume(volume_guid="export:2")
        catalog.add_scope_root(volume_id, str(media))

        root = writer.prepare_root(tmp_path / "shared" / "exports", catalog=catalog)
        assert root.is_dir()
        assert (media / "a.bin").read_bytes() == b"a" * 10


def test_a_destination_outside_the_archive_is_allowed(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.bin").write_bytes(b"a")

    with Catalog(tmp_path / "cat" / "catalog.db") as catalog:
        volume_id = catalog.upsert_volume(volume_guid="export:3")
        catalog.add_scope_root(volume_id, str(media))

        root = writer.prepare_root(tmp_path / "elsewhere", catalog=catalog)
        assert root.is_dir()


# -- what gets written --------------------------------------------------------

def test_text_is_checked_against_the_denylist_before_it_is_written(
    root: Path,
) -> None:
    """A document telling someone to delete their data must not reach a file,
    whichever part of the codebase composed it."""
    from drivefusion.core.wording import DestructiveLanguageError

    with pytest.raises(DestructiveLanguageError):
        writer.write(root, "steps.md", "1. Copy it across\n2. Then delete the original\n")
    assert not (root / "steps.md").exists(), "the file was created anyway"


def test_the_tools_own_disclaimer_can_be_written(root: Path) -> None:
    """It mentions deletion in order to rule it out, and must not be refused."""
    target = writer.write(root, "README.txt", writer.DISCLAIMER)
    assert "has no code that can do any of" in target.read_text()


def test_the_disclaimer_states_every_claim_section_one_requires() -> None:
    text = writer.DISCLAIMER.lower()
    assert "read-only" in text
    assert "no executor" in text
    assert "not a compliance" in text
    assert "data management and sharing plan" in text
    assert "identifiable human-subjects data" in text


def test_binary_content_is_written_verbatim(root: Path) -> None:
    payload = bytes(range(256))
    target = writer.write(root, "blob.bin", payload)
    assert target.read_bytes() == payload


def test_lines_are_newline_terminated(root: Path) -> None:
    target = writer.write_lines(root, "list.txt", ["one", "two"])
    assert target.read_text() == "one\ntwo\n"


def test_content_reaches_the_disk_rather_than_a_buffer(root: Path) -> None:
    """A bundle half-written when the process dies is worse than no bundle: it
    looks complete."""
    source = Path(writer.__file__).read_text()
    assert "fsync" in source

    target = writer.write(root, "durable.txt", "x" * 1000)
    assert os.stat(target).st_size == 1000
