"""Naming-template mappings and copy-only command previews (ROADMAP M8.4)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from drivefusion.core.export import commands, mappings, writer
from drivefusion.core.export.writer import ExportError
from drivefusion.core.planner import document
from drivefusion.core.store import curation
from tests.test_export_manifests import fleet  # noqa: F401  (the fixture)


def test_the_mapping_is_one_to_one_and_dated(fleet) -> None:  # noqa: F811
    coll = curation.collections(fleet.c)[0]
    fleet.c.conn.execute("UPDATE file SET mtime_ns = 1700000000000000000")   # 2023-11-14
    fleet.file("A", fleet.project, 9)
    sub = fleet.mkdir("A", "sub", fleet.project)
    clash = fleet.c.conn.execute("SELECT name FROM file WHERE dir_id = ? LIMIT 1",
                                 (fleet.project,)).fetchone()["name"]
    fleet.c.conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes) "
                         "VALUES (?, ?, ?, 4)", (fleet.vol["A"], sub, clash))
    pairs = mappings.mapping(fleet.c, coll["id"])
    olds, news = [o for o, _ in pairs], [n for _, n in pairs]
    assert len(set(olds)) == len(olds) == 4 and len(set(n.lower() for n in news)) == 4
    assert all(n.startswith("Project/") for n in news)
    assert any("/2023/" in n for n in news) and any("/undated/" in n for n in news)
    with pytest.raises(ExportError, match="unknown template field"):
        mappings.mapping(fleet.c, coll["id"], "{nope}")
    with pytest.raises(ExportError, match="relative path"):
        mappings.mapping(fleet.c, coll["id"], "../{name}")


def test_clashing_names_get_distinct_paths(fleet) -> None:  # noqa: F811
    coll = curation.collections(fleet.c)[0]
    sub = fleet.mkdir("A", "sub", fleet.project)
    for d in (fleet.project, sub):
        fleet.c.conn.execute("INSERT INTO file (volume_id, dir_id, name, size_bytes) "
                             "VALUES (?, ?, 'same.txt', 1)", (fleet.vol["A"], d))
    news = [n for _, n in mappings.mapping(fleet.c, coll["id"], "{collection}/{name}")]
    assert "Project/same.txt" in news and "Project/same (2).txt" in news


@pytest.mark.parametrize("line", [
    'robocopy "a" "b" /MIR', 'robocopy "a" "b" /purge', 'rsync -a --delete "a/" "b/"',
    'rsync -a --delete-after "a/" "b/"', 'del "x"', 'rm -rf "/"', 'format C:',
    'robocopy "a" "b" /E & del x', 'diskpart', 'rsync -a "a/" "b/" ; rm x',
    'robocopy "a" "b" /MOVE', 'rsync --remove-source-files "a/" "b/"', 'robocopy "a "b" /E',
])
def test_the_denylist_refuses_anything_destructive(line) -> None:
    with pytest.raises(ExportError):
        commands.check("x.cmd", line.encode())


def test_copy_only_previews_pass_and_quote_their_paths() -> None:
    lines = [commands.robocopy("E:/Photos/del Mar", "<ARCHIVE-2>/Drive Fusion copies/x",
                               recursive=True),
             commands.rsync("/media/a/rm folder", "<ARCHIVE-2>/y", recursive=False),
             "REM Nothing here deletes", "# rm is mentioned only in a comment"]
    commands.check("x", "\n".join(lines).encode())
    assert lines[0].startswith('robocopy "E:\\Photos\\del Mar" "<ARCHIVE-2>\\Drive Fusion')
    assert "--exclude=*/" in lines[1]
    with pytest.raises(ExportError, match="quote"):
        commands.rsync('/a/"; rm -rf ~ #', "/b", recursive=True)


def test_written_with_the_plan_previews(fleet) -> None:  # noqa: F811
    document.save(fleet.c, document.make(fleet.c, use_cpsat=False))
    bundle = writer.open_bundle(fleet.c, fleet.out, "mappings")
    summary = mappings.write(bundle, fleet.c)
    bundle.close(summary=summary)
    assert summary["command_previews"] == 2 and summary["mapped"] == 2
    coll = curation.collections(fleet.c)[0]
    folder = bundle.path / coll["local_pid"]
    forward = list(csv.reader((folder / "mapping.csv").open(encoding="utf-8", newline="")))[1:]
    inverse = list(csv.reader((folder / "inverse.csv").open(encoding="utf-8", newline="")))[1:]
    assert {(o, n) for o, n in forward} == {(o, n) for n, o in inverse}
    plan_dir = next(p for p in bundle.path.iterdir() if p.name.startswith("plan-"))
    sh = (plan_dir / "copy-commands.sh").read_text(encoding="utf-8")
    assert "rsync -a --protect-args" in sh and "<ARCHIVE-2>" in sh
    commands.check("sh", sh.encode())
    commands.check("cmd", (plan_dir / "copy-commands.cmd").read_bytes())


def test_a_destructive_preview_is_never_written(fleet, monkeypatch) -> None:  # noqa: F811
    document.save(fleet.c, document.make(fleet.c, use_cpsat=False))
    monkeypatch.setattr(commands, "previews", lambda c, p: {
        "copy-commands.sh": 'rsync -a --delete "a/" "b/"\n'})
    bundle = writer.open_bundle(fleet.c, fleet.out, "mappings")
    with pytest.raises(ExportError, match="denylist"):
        mappings.write(bundle, fleet.c)
    assert not list(bundle.path.rglob("copy-commands.sh"))
