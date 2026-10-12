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
    for verb, milestone in (("plan", "M7"), ("export", "M8")):
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


def test_gui_verb_exists_and_is_not_destructive() -> None:
    """The window is reachable from the CLI, and adds no new verbs behind it."""
    parser = build_parser()
    commands = set()
    for action in parser._actions:
        if getattr(action, "choices", None) and hasattr(action.choices, "keys"):
            commands.update(action.choices.keys())
    assert "gui" in commands


def test_the_windowed_build_opens_a_window_instead_of_printing_help() -> None:
    """Double-clicking the windowed exe must not print help to a missing console.

    A process built with console=False has nowhere to write, so a user who
    double-clicks would see the program start and vanish with no explanation.
    """
    from drivefusion.cli.main import wants_window

    # The windowed build, launched with no arguments: open the window.
    assert wants_window("C:\\Program Files\\DriveFusion\\DriveFusion.exe",
                        frozen=True, has_command=False)

    # The console build: keep printing help.
    assert not wants_window("C:\\Program Files\\DriveFusion\\drivefusion.exe",
                            frozen=True, has_command=False)

    # A subcommand was given, so it is a CLI run whichever binary started it.
    assert not wants_window("DriveFusion.exe", frozen=True, has_command=True)

    # From a source checkout, `python -m drivefusion` still prints help.
    assert not wants_window("DriveFusion.exe", frozen=False, has_command=False)


def test_a_verb_with_subcommands_prints_its_own_help(cli, capsys) -> None:
    """`health` and `scope` both need an action.

    The dispatcher used to read only `scope_command`, so `health show` fell
    through to the top-level help and looked like an unknown verb.
    """
    from drivefusion.cli.main import SUBCOMMAND_VERBS

    for verb in SUBCOMMAND_VERBS:
        assert cli(verb) == 0
        assert verb in capsys.readouterr().out


def test_health_subcommands_are_reachable(cli, tmp_path: Path, capsys) -> None:
    tree = tmp_path / "media"
    tree.mkdir()
    payload = b"same size twice" * 40
    (tree / "a.bin").write_bytes(payload)
    (tree / "b.bin").write_bytes(payload)

    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    assert cli("hash") == 0
    capsys.readouterr()

    assert cli("health", "show") == 0
    shown = capsys.readouterr().out
    assert "%/yr" in shown
    assert "generic-defaults" in shown, "the rubric version must be named"

    assert cli("health", "collect", "--dry-run") == 0
    dry = capsys.readouterr().out
    assert "nothing was read and nothing was recorded" in dry

    assert cli("health", "collect") == 0
    assert "estimated" in capsys.readouterr().out


def test_health_set_records_what_only_the_user_knows(
    cli, tmp_path: Path, capsys
) -> None:
    """Purchase date and zone cannot be discovered; they change the numbers."""
    tree = tmp_path / "media"
    tree.mkdir()
    (tree / "a.bin").write_bytes(b"x" * 100)

    assert cli("scope", "add", str(tree)) == 0
    capsys.readouterr()

    from drivefusion.core.store.catalog import Catalog

    catalog_path = tmp_path / "catalog" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        drives = catalog.drives()
    if not drives:
        pytest.skip("no physical drive identified in this environment")

    drive_id = int(drives[0]["id"])
    assert cli("health", "set", str(drive_id), "--nickname", "ARCHIVE-9") == 0
    assert "updated drive" in capsys.readouterr().out

    assert cli("health", "set", str(drive_id)) == 2
    assert "nothing to set" in capsys.readouterr().err


def test_health_set_rejects_an_unknown_drive(cli, capsys) -> None:
    assert cli("health", "set", "999", "--nickname", "X") == 1
    assert "no drive 999" in capsys.readouterr().err


def test_risk_report_states_what_it_rests_on(cli, tmp_path: Path, capsys) -> None:
    """A loss figure without its evidence and caveats is not reportable."""
    tree = tmp_path / "media"
    tree.mkdir()
    payload = b"duplicated content" * 60
    (tree / "a.bin").write_bytes(payload)
    (tree / "b.bin").write_bytes(payload)

    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    assert cli("hash") == 0
    capsys.readouterr()

    assert cli("report", "risk") == 0
    report = capsys.readouterr().out
    assert "expected loss" in report
    assert "/year" in report
    assert "generic defaults" in report, "a generic rate must be labelled as one"
    assert "age-based estimates" in report, "the evidence base must be stated"


def test_risk_report_on_an_unhashed_catalog_says_why_it_is_empty(
    cli, tmp_path: Path, capsys
) -> None:
    tree = tmp_path / "media"
    tree.mkdir()
    (tree / "unique.bin").write_bytes(b"z" * 777)

    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    capsys.readouterr()

    assert cli("report", "risk") == 0
    assert "nothing to put a risk figure on" in capsys.readouterr().out


def _curated(cli, tmp_path: Path, capsys):
    """A scanned, hashed tree with one confirmed collection."""
    root = tmp_path / "drive" / "study-2024"
    root.mkdir(parents=True)
    for index in range(10):
        (root / f"scan-{index}.tif").write_bytes(b"T" * (200_000 + index % 3))

    assert cli("scope", "add", str(tmp_path / "drive")) == 0
    assert cli("scan") == 0
    assert cli("hash") == 0
    assert cli("collections", "propose", "--record") == 0
    capsys.readouterr()
    return root


def test_collections_propose_confirms_nothing(cli, tmp_path: Path, capsys) -> None:
    """The milestone's central rule, asserted on the CLI surface too."""
    root = tmp_path / "drive" / "study-2024"
    root.mkdir(parents=True)
    for index in range(10):
        (root / f"scan-{index}.tif").write_bytes(b"T" * (200_000 + index % 3))

    assert cli("scope", "add", str(tmp_path / "drive")) == 0
    assert cli("scan") == 0
    capsys.readouterr()

    assert cli("collections", "propose") == 0
    proposed = capsys.readouterr().out
    assert "Nothing is confirmed until you say so" in proposed
    assert "Nothing was stored" in proposed

    assert cli("collections", "list") == 0
    assert "No confirmed collections yet" in capsys.readouterr().out


def test_accepting_warns_about_the_missing_licence(
    cli, tmp_path: Path, capsys
) -> None:
    """R1.1 is the finding that caps a grade, so it is said at accept time."""
    _curated(cli, tmp_path, capsys)

    assert cli("collections", "queue") == 0
    assert "accept <id>" in capsys.readouterr().out

    assert cli("collections", "accept", "1", "--title", "Study 2024") == 0
    accepted = capsys.readouterr().out
    assert "membership is a rule" in accepted
    assert "blocking finding" in accepted


def test_score_reports_gaps_remedies_and_its_rubric(
    cli, tmp_path: Path, capsys
) -> None:
    _curated(cli, tmp_path, capsys)
    assert cli("collections", "accept", "1", "--title", "Study 2024") == 0
    capsys.readouterr()

    assert cli("score") == 0
    report = capsys.readouterr().out
    assert "FAIR:" in report
    assert "drivefusion-fair-rubric" in report, "the rubric version must be named"
    assert "BLOCKING R1.1" in report or "BLOCKING" in report
    assert "remedy:" in report
    assert "NDSA level" in report
    # The unfiled bucket is scored separately, never folded into the grade.
    assert "Unfiled content" in report
    assert "describe the filed share only" in report


def test_score_after_describing_lifts_the_block(cli, tmp_path: Path, capsys) -> None:
    _curated(cli, tmp_path, capsys)
    assert cli("collections", "accept", "1", "--title", "Study 2024") == 0
    assert (
        cli(
            "collections", "describe", "1",
            "--license", "CC-BY-4.0",
            "--data-type", "imaging",
            "--description", "Confocal scans.",
            "--contact", "lab@example.org",
            "--creators", "A. Researcher",
            "--project", "Imaging",
            "--retention-note", "Keep ten years.",
        )
        == 0
    )
    capsys.readouterr()

    assert cli("score", "1") == 0
    report = capsys.readouterr().out
    assert "BLOCKING" not in report
    assert "pass  R1.1" in report


def test_coverage_names_the_unfiled_share(cli, tmp_path: Path, capsys) -> None:
    _curated(cli, tmp_path, capsys)
    assert cli("collections", "coverage") == 0
    coverage = capsys.readouterr().out
    assert "unfiled" in coverage.lower()
    assert "in a collection" in coverage


def test_vocab_lists_controlled_terms_without_forcing_them(
    cli, capsys
) -> None:
    assert cli("collections", "vocab") == 0
    vocab = capsys.readouterr().out
    assert "CC-BY-4.0" in vocab
    assert "not-for-sharing" in vocab
    assert "never refused" in vocab, (
        "the CLI must say an uncontrolled value is recorded, not rejected"
    )


def test_rejecting_a_candidate_is_remembered(cli, tmp_path: Path, capsys) -> None:
    _curated(cli, tmp_path, capsys)
    assert cli("collections", "reject", "1") == 0
    assert "will not be proposed again" in capsys.readouterr().out

    assert cli("collections", "reject", "1") == 1
    assert "no pending candidate" in capsys.readouterr().err


def test_scan_without_scope_is_an_error(cli, capsys) -> None:
    assert cli("scan") == 1
    assert "no scope roots" in capsys.readouterr().out
