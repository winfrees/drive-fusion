"""Drive names: what a person calls a drive, and never two the same."""

from pathlib import Path

from drivefusion.core.analysis.names import drive_names
from drivefusion.core.store.catalog import Catalog
from tests.test_planner_units import Fleet


def _fleet(tmp_path: Path) -> Fleet:
    return Fleet(Catalog(tmp_path / "catalog.db"))


def test_a_nickname_then_a_volume_label_then_the_model(tmp_path: Path) -> None:
    f = _fleet(tmp_path)
    nick = f.drive("SER-1", nickname="Archive A", model="WD Elements")
    label = f.drive("SER-2", model="Seagate One")
    f.c.conn.execute("UPDATE volume SET label = 'PHOTOS' WHERE drive_id = ?", (label,))
    model = f.drive("SER-3", model="Samsung T7")
    names = drive_names(f.c)
    assert names == {nick: "Archive A", label: "PHOTOS", model: "Samsung T7"}


def test_drives_that_would_share_a_name_are_told_apart(tmp_path: Path) -> None:
    """Three drives bought together: one model, no nicknames."""
    f = _fleet(tmp_path)
    ids = [f.drive(serial, model="WD Elements") for serial in ("WX11AAAA", "WX22BBBB", "WX33CCCC")]
    for drive_id, letter in zip(ids, ("E:", "F:", "G:")):
        f.c.conn.execute("UPDATE volume SET last_letter = ? WHERE drive_id = ?", (letter, drive_id))
    assert [drive_names(f.c)[i] for i in ids] == [
        "WD Elements (E:)", "WD Elements (F:)", "WD Elements (G:)"]

    # Without distinct letters, the end of the serial tells them apart.
    f.c.conn.execute("UPDATE volume SET last_letter = NULL")
    assert [drive_names(f.c)[i] for i in ids] == [
        "WD Elements (…AAAA)", "WD Elements (…BBBB)", "WD Elements (…CCCC)"]
    assert len(set(drive_names(f.c).values())) == 3
