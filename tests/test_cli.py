"""CLI behaviour: exit codes, output, and the absence of destructive verbs."""

from __future__ import annotations

from pathlib import Path

import pytest

from drivefusion.cli.main import build_parser, main


@pytest.fixture
def cli(tmp_path: Path):
    catalog = tmp_path / "catalog" / "catalog.db"

    def run(*args: str) -> int:
        return main(["--catalog", str(catalog), *args])

    return run


def test_no_destructive_verbs_exist() -> None:
    """The CLI surface is part of the guarantee, not just the internals."""
    parser = build_parser()
    actions = [
        action
        for action in parser._actions
        if getattr(action, "choices", None) and hasattr(action.choices, "keys")
    ]
    verbs = set()
    for action in actions:
        verbs.update(action.choices.keys())

    forbidden = {
        "apply", "run", "execute", "move", "copy", "delete", "remove",
        "dedupe", "deduplicate", "clean", "purge", "reclaim", "sync",
    }
    assert not (verbs & forbidden), f"destructive verb exposed: {verbs & forbidden}"


def test_version_and_help(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "drivefusion" in capsys.readouterr().out

    assert main([]) == 0
    assert "read-only" in capsys.readouterr().out


def test_scope_add_scan_find_status(cli, sample_tree: Path, capsys) -> None:
    assert cli("scope", "add", str(sample_tree)) == 0
    assert "added scope root 1" in capsys.readouterr().out

    assert cli("scope", "list") == 0
    assert str(sample_tree) in capsys.readouterr().out

    assert cli("scope", "preview", "1") == 0
    preview = capsys.readouterr().out
    assert "nothing was written" in preview

    assert cli("scan") == 0
    scan_output = capsys.readouterr().out
    assert "coverage:" in scan_output

    assert cli("find", "report.txt") == 0
    assert "report.txt" in capsys.readouterr().out

    assert cli("status") == 0
    status = capsys.readouterr().out
    assert "files:" in status and "bytes/file" in status


def test_find_reports_no_matches(cli, sample_tree: Path, capsys) -> None:
    cli("scope", "add", str(sample_tree))
    cli("scan")
    capsys.readouterr()

    assert cli("find", "nothing-matches-this") == 1
    assert "no matches" in capsys.readouterr().out


def test_nested_scope_is_refused(cli, sample_tree: Path, capsys) -> None:
    assert cli("scope", "add", str(sample_tree)) == 0
    capsys.readouterr()
    assert cli("scope", "add", str(sample_tree / "docs")) == 1
    assert "already covered" in capsys.readouterr().err


def test_removing_scope_keeps_the_catalog(cli, sample_tree: Path, capsys) -> None:
    cli("scope", "add", str(sample_tree))
    cli("scan")
    capsys.readouterr()

    assert cli("scope", "remove", "1") == 0
    out = capsys.readouterr().out
    assert "nothing on disk was touched" in out

    assert cli("find", "report.txt") == 0


def test_unknown_scope_root_is_an_error(cli, capsys) -> None:
    assert cli("scope", "preview", "99") == 1
    assert "no scope root 99" in capsys.readouterr().err


def test_planned_verbs_report_their_milestone(cli, capsys) -> None:
    for verb, milestone in (("score", "M6"), ("plan", "M7"), ("export", "M8")):
        assert cli(verb) == 2
        assert milestone in capsys.readouterr().err


def test_hash_report_and_verify(cli, tmp_path: Path, capsys) -> None:
    tree = tmp_path / "media"
    (tree / "sub").mkdir(parents=True)
    payload = b"duplicated content" * 60
    (tree / "one.bin").write_bytes(payload)
    (tree / "sub" / "two.bin").write_bytes(payload)
    (tree / "solo.bin").write_bytes(b"x" * 977)

    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    capsys.readouterr()

    assert cli("hash") == 0
    assert "quick-hashed" in capsys.readouterr().out

    assert cli("report") == 0
    report = capsys.readouterr().out
    assert "one.bin" in report and "two.bin" in report
    # The solo file has a size no other file shares, so it is never opened —
    # and the report has to say so rather than implying full coverage.
    assert "no content identity yet" in report
    # A reclamation figure must never read as an instruction.
    assert "never deletes" in report

    assert cli("report", "duplicates") == 0
    assert "more than one path" in capsys.readouterr().out

    assert cli("verify") == 0
    verified = capsys.readouterr().out
    assert "checked:    2" in verified
    assert "mismatched: 0" in verified


def test_verify_exits_nonzero_on_corruption(cli, tmp_path: Path, capsys) -> None:
    """A fixity failure is a finding; it must not look like a clean run."""
    import os

    tree = tmp_path / "media"
    tree.mkdir()
    payload = b"fixity" * 100
    (tree / "a.bin").write_bytes(payload)
    (tree / "b.bin").write_bytes(payload)

    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    assert cli("hash") == 0
    capsys.readouterr()

    target = tree / "a.bin"
    before = os.stat(target)
    damaged = bytearray(payload)
    damaged[len(payload) // 2] = ord("!")
    target.write_bytes(bytes(damaged))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert cli("verify") == 1
    assert "mismatched: 1" in capsys.readouterr().out

    assert cli("report", "integrity") == 0
    incidents = capsys.readouterr().out
    assert "mismatch" in incidents and "a.bin" in incidents


def test_report_on_an_empty_catalog_says_so(cli, capsys) -> None:
    assert cli("report") == 0
    out = capsys.readouterr().out
    assert "distinct content:  0" in out


def test_scan_without_scope_is_an_error(cli, capsys) -> None:
    assert cli("scan") == 1
    assert "no scope roots" in capsys.readouterr().out
