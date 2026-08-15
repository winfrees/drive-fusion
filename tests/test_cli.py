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
    for verb, milestone in (("report", "M3"), ("plan", "M7"), ("export", "M8")):
        assert cli(verb) == 2
        assert milestone in capsys.readouterr().err


def test_scan_without_scope_is_an_error(cli, capsys) -> None:
    assert cli("scan") == 1
    assert "no scope roots" in capsys.readouterr().out
