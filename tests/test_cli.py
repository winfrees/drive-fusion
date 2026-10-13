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
    """Empty since M8: every verb the CLI advertises is implemented.

    The mechanism is still asserted, because a later milestone that announces
    a verb before building it should say so rather than printing top-level help
    and exiting zero.
    """
    from drivefusion.cli.main import PLANNED_VERBS

    for verb, (_help, milestone) in PLANNED_VERBS.items():
        assert cli(verb) == 2
        assert milestone in capsys.readouterr().err


def test_every_advertised_verb_actually_runs(cli, capsys) -> None:
    """A verb in the help that falls through to top-level help is worse than
    one that says "not yet": it looks like the user got it wrong."""
    from drivefusion.cli.main import (
        DISPATCH,
        PLANNED_VERBS,
        SUBCOMMAND_VERBS,
        build_parser,
    )

    parser = build_parser()
    verbs: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if choices and hasattr(choices, "keys"):
            verbs.update(choices.keys())

    dispatched = {verb for verb, _action in DISPATCH}
    unreachable = verbs - dispatched - set(PLANNED_VERBS) - set(SUBCOMMAND_VERBS)
    assert not unreachable, f"verbs with no handler: {sorted(unreachable)}"

    # Every subcommand verb prints its own help and exits zero.
    for verb in SUBCOMMAND_VERBS:
        assert cli(verb) == 0, f"`{verb}` with no action did not print help"
        assert verb in capsys.readouterr().out


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


# -- plan ---------------------------------------------------------------------

def _plannable(tmp_path: Path, catalog_path: Path) -> Path:
    """A tree on one drive, with two more drives available to copy to."""
    from drivefusion.core.scan import scan_root
    from drivefusion.core.store.catalog import Catalog

    tree = tmp_path / "media" / "project"
    tree.mkdir(parents=True)
    for index in range(40):
        (tree / f"f{index:03}.dat").write_bytes(b"x" * 5000)

    with Catalog(catalog_path) as catalog:
        source = catalog.upsert_drive(serial="CLI-SRC", nickname="SOURCE")
        catalog.update_drive(source, disaster_zone="office")
        volume_id = catalog.upsert_volume(
            volume_guid="cli:src",
            drive_id=source,
            fs_type="ntfs",
            cluster_bytes=4096,
            capacity_bytes=500 * 1024 ** 3,
            free_bytes=400 * 1024 ** 3,
        )
        root_id = catalog.add_scope_root(volume_id, str(tmp_path / "media"))
        scan_root(
            catalog,
            root_path=str(tmp_path / "media"),
            root_id=root_id,
            volume_id=volume_id,
        )
        for name, fs, cluster, zone in (
            ("ARCHIVE", "exfat", 131072, "office"),
            ("OFFSITE", "ntfs", 4096, "parents"),
        ):
            drive_id = catalog.upsert_drive(serial=f"CLI-{name}", nickname=name)
            catalog.update_drive(drive_id, disaster_zone=zone)
            catalog.upsert_volume(
                volume_guid=f"cli:{name}",
                drive_id=drive_id,
                fs_type=fs,
                cluster_bytes=cluster,
                capacity_bytes=500 * 1024 ** 3,
                free_bytes=500 * 1024 ** 3,
            )
    return tmp_path / "media"


def test_plan_build_shows_a_checklist_with_reasons(cli, tmp_path: Path, capsys) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat") == 0
    out = capsys.readouterr().out

    assert "[copy]" in out
    assert "[verify]" in out
    assert "reduces expected loss" in out
    assert "data to copy" in out
    # The space a copy needs on exFAT is not its size, and the plan must say so
    # rather than leaving an unexplained discrepancy.
    assert "clusters" in out


def test_plan_build_says_nothing_will_be_carried_out(
    cli, tmp_path: Path, capsys
) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat", "--save") == 0
    out = capsys.readouterr().out
    assert "draft" in out
    assert "will not carry it out" in out


def test_plan_output_never_tells_the_user_to_delete_anything(
    cli, tmp_path: Path, capsys
) -> None:
    """The CLI's own rendering, not just the plan object it rendered from."""
    from drivefusion.core import wording

    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat") == 0
    wording.assert_safe(capsys.readouterr().out, what="plan output")


def test_plan_build_honours_the_level_and_exclusions(
    cli, tmp_path: Path, capsys
) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat", "--level", "2") == 0
    assert "3 copies" in capsys.readouterr().out

    # Excluding every destination leaves nowhere to copy to, which is an
    # acquire finding and not an error.
    assert cli(
        "plan", "build", "--no-cp-sat",
        "--exclude-drive", "2", "--exclude-drive", "3",
    ) == 0
    out = capsys.readouterr().out
    assert "[acquire]" in out
    assert "will not propose deleting" in out
    assert "[copy]" not in out


def test_plan_build_emits_json_on_request(cli, tmp_path: Path, capsys) -> None:
    import json

    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat", "--json") == 0
    parsed = json.loads(capsys.readouterr().out)

    assert parsed["steps"]
    assert parsed["policy"]["min_copies"] == 2
    assert "no executor" in parsed["note"]


def test_plan_list_and_show_round_trip(cli, tmp_path: Path, capsys) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("plan", "list") == 1, "no plans saved yet"
    assert "No saved plans" in capsys.readouterr().out

    assert cli("plan", "build", "--no-cp-sat", "--save", "--name", "Spring tidy") == 0
    capsys.readouterr()

    assert cli("plan", "list") == 0
    listing = capsys.readouterr().out
    assert "Spring tidy" in listing
    assert "draft" in listing

    assert cli("plan", "show", "1") == 0
    shown = capsys.readouterr().out
    assert "Spring tidy" in shown
    assert "[copy]" in shown
    assert "2 copies on distinct drives" in shown


def test_plan_show_reports_a_missing_plan_without_a_traceback(
    cli, tmp_path: Path, capsys
) -> None:
    assert cli("plan", "show", "404") == 1
    assert "404" in capsys.readouterr().err


def test_plan_with_no_action_prints_its_own_help(cli, capsys) -> None:
    assert cli("plan") == 0
    out = capsys.readouterr().out
    assert "build" in out and "list" in out and "show" in out


def test_no_cp_sat_actually_skips_the_optimiser(
    cli, tmp_path: Path, capsys, monkeypatch
) -> None:
    """The flag's effect is invisible in the output when CP-SAT fails to beat
    the baseline, which it often does — so the wiring is asserted directly
    rather than inferred from a plan that would look the same either way.
    """
    from drivefusion.core.plan import solver as solver_module

    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    calls = []
    real = solver_module._solve_cp_sat

    def recording(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(solver_module, "_solve_cp_sat", recording)

    assert cli("plan", "build", "--no-cp-sat") == 0
    capsys.readouterr()
    assert not calls, "--no-cp-sat still ran the optimiser"

    assert cli("plan", "build") == 0
    capsys.readouterr()
    assert calls, "the optimiser was never attempted without the flag"


# -- export -------------------------------------------------------------------

def test_export_bundle_writes_and_verifies(cli, tmp_path: Path, capsys) -> None:
    media = _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    destination = tmp_path / "exports"
    assert cli("export", "bundle", str(destination)) == 0
    out = capsys.readouterr().out

    assert "Wrote" in out
    assert "Verified against its own manifest" in out
    assert "not a copy of it" in out

    bundles = list(destination.iterdir())
    assert len(bundles) == 1
    assert (bundles[0] / "data" / "report.html").is_file()
    assert (bundles[0] / "manifest-sha256.txt").is_file()
    # And the archive is untouched.
    assert sorted(p.name for p in (media / "project").iterdir())[:1] == ["f000.dat"]


def test_export_refuses_a_destination_inside_the_archive(
    cli, tmp_path: Path, capsys
) -> None:
    media = _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("export", "bundle", str(media / "exports")) == 2
    error = capsys.readouterr().err
    assert "inside the scanned tree" in error
    assert "outside your archive" in error
    assert not list(media.glob("exports")), "a folder was created anyway"


def test_export_output_never_tells_the_user_to_delete_anything(
    cli, tmp_path: Path, capsys
) -> None:
    from drivefusion.core import wording

    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("export", "bundle", str(tmp_path / "exports")) == 0
    wording.assert_safe(capsys.readouterr().out, what="export output")


def test_export_list_shows_what_was_written(cli, tmp_path: Path, capsys) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("export", "list") == 1
    assert "No exports recorded" in capsys.readouterr().out

    assert cli("export", "bundle", str(tmp_path / "exports")) == 0
    capsys.readouterr()

    assert cli("export", "list") == 0
    listing = capsys.readouterr().out
    assert "file" in listing
    assert str(tmp_path / "exports") in listing


def test_export_verify_catches_a_tampered_bundle(
    cli, tmp_path: Path, capsys
) -> None:
    """A manifest nobody can fail is a manifest nobody has checked."""
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    destination = tmp_path / "exports"
    assert cli("export", "bundle", str(destination)) == 0
    capsys.readouterr()
    root = next(destination.iterdir())

    assert cli("export", "verify", str(root)) == 0
    assert "validates against its own manifest" in capsys.readouterr().out

    report = root / "data" / "report.html"
    report.write_text(report.read_text() + " ")

    assert cli("export", "verify", str(root)) == 1
    error = capsys.readouterr().err
    assert "does NOT validate" in error
    assert "report.html" in error


def test_export_verify_reports_a_missing_folder_without_a_traceback(
    cli, tmp_path: Path, capsys
) -> None:
    assert cli("export", "verify", str(tmp_path / "nope")) == 1
    assert "not a directory" in capsys.readouterr().err


def test_export_templates_explains_the_placeholders_and_the_undo_rule(
    cli, capsys
) -> None:
    assert cli("export", "templates") == 0
    out = capsys.readouterr().out

    assert "{stem}" in out and "{collection}" in out
    assert "{stem} is required" in out
    assert "inverse" in out
    assert "refused rather" in out


def test_export_with_a_plan_marks_it_exported(cli, tmp_path: Path, capsys) -> None:
    from drivefusion.core.plan import document
    from drivefusion.core.store.catalog import Catalog

    catalog_path = tmp_path / "catalog" / "catalog.db"
    _plannable(tmp_path, catalog_path)
    capsys.readouterr()

    assert cli("plan", "build", "--no-cp-sat", "--save") == 0
    capsys.readouterr()

    assert cli("export", "bundle", str(tmp_path / "exports"), "--plan", "1") == 0
    capsys.readouterr()

    with Catalog(catalog_path) as catalog:
        assert document.load(catalog, 1)["plan"]["status"] == "exported"


def test_export_reports_a_missing_plan_rather_than_exporting_without_one(
    cli, tmp_path: Path, capsys
) -> None:
    _plannable(tmp_path, tmp_path / "catalog" / "catalog.db")
    capsys.readouterr()

    assert cli("export", "bundle", str(tmp_path / "exports"), "--plan", "99") == 1
    assert "99" in capsys.readouterr().err
    assert not (tmp_path / "exports").exists(), "a bundle was written anyway"


def test_export_with_no_action_prints_its_own_help(cli, capsys) -> None:
    assert cli("export") == 0
    out = capsys.readouterr().out
    assert "bundle" in out and "verify" in out and "templates" in out


def test_save_survives_a_closed_stdout(cli, tmp_path: Path, capsys) -> None:
    """Piping into `head` closes stdout partway through the listing.

    That raises BrokenPipeError, which the CLI handles as the normal thing it
    is — and with the save happening after the printing, the plan the user
    explicitly asked to keep was silently dropped while the command still
    exited zero. Found by piping the real command into `head`, not by reading
    the code.
    """
    import io
    import sys

    from drivefusion.core.plan import document
    from drivefusion.core.store.catalog import Catalog

    catalog_path = tmp_path / "catalog" / "catalog.db"
    _plannable(tmp_path, catalog_path)
    capsys.readouterr()

    class _ClosedAfterFirstWrite(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self._writes = 0

        def write(self, text: str) -> int:
            self._writes += 1
            if self._writes > 1:
                raise BrokenPipeError(32, "Broken pipe")
            return super().write(text)

    original = sys.stdout
    sys.stdout = _ClosedAfterFirstWrite()
    try:
        exit_code = cli("plan", "build", "--no-cp-sat", "--save")
    finally:
        sys.stdout = original

    assert exit_code == 0, "a closed pipe is not an error"
    with Catalog(catalog_path) as catalog:
        assert document.plans(catalog), "the plan was not saved"
