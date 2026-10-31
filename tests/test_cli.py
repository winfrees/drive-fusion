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
    for verb, milestone in (("export", "M8"),):
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
    # but that makes it a known single copy, reported as under-protected
    # rather than as an unknown (docs/REVIEW.md F-04).
    assert "no content identity yet" not in report
    assert "solo.bin" in report
    assert "under-protected:   2 items" in report
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


def test_scan_without_scope_is_an_error(cli, capsys) -> None:
    assert cli("scan") == 1
    assert "no scope roots" in capsys.readouterr().out


# -- refusals (docs/REVIEW.md F-01, F-02, F-03) --------------------------------

def _another_volume(monkeypatch) -> None:
    """Make every path report a volume the catalog has never seen."""
    from drivefusion.core import discovery

    monkeypatch.setattr(
        discovery,
        "identify_volume",
        lambda path: discovery.VolumeInfo(volume_guid="vol:impostor", label="B"),
    )


def test_scan_hash_and_verify_refuse_a_different_volume_at_the_root(
    cli, tmp_path: Path, capsys, monkeypatch
) -> None:
    tree = tmp_path / "media"
    tree.mkdir()
    payload = b"bound" * 100
    (tree / "a.bin").write_bytes(payload)
    (tree / "b.bin").write_bytes(payload)
    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    capsys.readouterr()

    _another_volume(monkeypatch)
    (tree / "c.bin").write_bytes(b"new")   # would be catalogued if scanned

    assert cli("scan") == 1
    err = capsys.readouterr().err
    assert "not scanning" in err and "not the catalogued volume" in err

    assert cli("hash") == 1
    err = capsys.readouterr().err
    assert "skipping files under" in err and "nothing to hash" in err

    assert cli("verify") == 1
    assert "nothing to verify" in capsys.readouterr().err

    monkeypatch.undo()
    assert cli("status") == 0
    status = capsys.readouterr().out
    assert "files:       2" in status, "the refused scan recorded nothing"
    assert "identified:  0 files" in status, "the refused hash read nothing"


def test_scan_of_an_unplugged_root_reports_and_keeps_its_files(
    cli, tmp_path: Path, capsys
) -> None:
    tree = tmp_path / "media"
    tree.mkdir()
    (tree / "a.bin").write_bytes(b"a")
    assert cli("scope", "add", str(tree)) == 0
    assert cli("scan") == 0
    tree.rename(tmp_path / "unplugged")
    capsys.readouterr()

    assert cli("scan") == 1
    assert "is not there" in capsys.readouterr().err
    assert cli("find", "a.bin") == 0, "still catalogued, not vanished"


def test_a_catalog_inside_a_scope_root_is_refused(
    tmp_path: Path, capsys
) -> None:
    tree = tmp_path / "media"
    tree.mkdir()
    inside = tree / "catalog.db"
    assert main(["--catalog", str(inside), "scope", "add", str(tree)]) == 1
    assert "contains the catalog" in capsys.readouterr().err
    assert main(["--catalog", str(inside), "scope", "list"]) == 0
    assert "no scope roots" in capsys.readouterr().out


def test_scope_add_refuses_a_root_on_the_catalogs_data_drive(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Refused before anything is recorded, not even the volume (ROADMAP D8)."""
    from drivefusion.core import discovery

    monkeypatch.setattr(discovery, "system_volume_guid", lambda: "vol:some-other-drive")
    tree = tmp_path / "media"
    tree.mkdir()
    catalog = tmp_path / "catalog" / "catalog.db"
    assert main(["--catalog", str(catalog), "scope", "add", str(tree)]) == 1
    assert "same drive as the catalog" in capsys.readouterr().err

    from drivefusion.core.store.catalog import Catalog

    with Catalog(catalog) as opened:
        assert opened.volumes() == [] and opened.scope_roots() == []


def test_drives_list_confirm_and_assign(cli, sample_tree: Path, capsys, tmp_path: Path) -> None:
    """REVIEW F-05: a volume with no identified drive, settled by the user."""
    from drivefusion.core.store.catalog import Catalog

    assert cli("scope", "add", str(sample_tree)) == 0
    catalog_path = tmp_path / "catalog" / "catalog.db"
    with Catalog(catalog_path) as catalog:
        catalog.conn.execute("UPDATE volume SET drive_id = NULL")   # a bridge with no serial
        volume = catalog.unverified_volumes()[0]["id"]
        known = catalog.upsert_drive(serial="KNOWN-1", model="Archive")
    capsys.readouterr()

    assert cli("drives", "list") == 0
    out = capsys.readouterr().out
    assert "drive UNVERIFIED" in out and "drives confirm VOLUME" in out

    assert cli("drives", "assign", str(volume), "999") == 1
    assert "no drive 999" in capsys.readouterr().err

    assert cli("drives", "assign", str(volume), str(known)) == 0
    capsys.readouterr()
    assert cli("drives", "list") == 0
    out = capsys.readouterr().out
    assert "UNVERIFIED" not in out and "identified by serial number" in out

    # Now identified by hardware (through the drive it was assigned to): settled.
    assert cli("drives", "confirm", str(volume)) == 1
    assert "can't be overruled" in capsys.readouterr().err


def test_drives_without_an_action_shows_help(cli, capsys) -> None:
    with pytest.raises(SystemExit):
        cli("drives")
    assert "confirm" in capsys.readouterr().out


def test_scope_list_shows_when_the_root_was_last_scanned(cli, sample_tree: Path, capsys) -> None:
    """REVIEW F-12: straight after a scan, not "9 months ago"."""
    assert cli("scope", "add", str(sample_tree)) == 0
    capsys.readouterr()
    assert cli("scope", "list") == 0
    assert "last scanned: never scanned" in capsys.readouterr().out
    assert cli("scan") == 0
    capsys.readouterr()
    assert cli("scope", "list") == 0
    out = capsys.readouterr().out
    assert "never scanned" not in out and "last scanned:" in out


# -- the first-run disclaimer (docs/PLAN.md §1, REVIEW SR-25) ------------------

@pytest.fixture
def no_scripted_acceptance(monkeypatch):
    monkeypatch.delenv("DRIVEFUSION_ACCEPT_DISCLAIMER", raising=False)


def _roots(catalog: Path) -> list:
    from drivefusion.core.store.catalog import Catalog

    with Catalog(catalog) as opened:
        return opened.scope_roots()


def test_nothing_is_read_before_the_disclaimer_is_accepted(
    tmp_path: Path, sample_tree: Path, capsys, no_scripted_acceptance
) -> None:
    catalog = tmp_path / "cat" / "catalog.db"
    for argv in (["scope", "add", str(sample_tree)], ["scan"], ["hash"], ["verify"]):
        assert main(["--catalog", str(catalog), *argv]) == 2, argv
        err = " ".join(capsys.readouterr().err.split())     # the text is wrapped
        assert "not been accepted" in err and "regulated data" in err
    assert _roots(catalog) == [], "a refused scope add recorded something"
    # Reading the catalog itself is not gated.
    assert main(["--catalog", str(catalog), "status"]) == 0


def test_the_flag_accepts_once_and_is_recorded(
    tmp_path: Path, sample_tree: Path, capsys, no_scripted_acceptance
) -> None:
    import json

    from drivefusion.core import disclaimer
    from drivefusion.core.store import audit
    from drivefusion.core.store.catalog import Catalog

    catalog = tmp_path / "cat" / "catalog.db"
    assert main(["--catalog", str(catalog), "--accept-disclaimer", "scope", "add",
                 str(sample_tree)]) == 0
    assert main(["--catalog", str(catalog), "scan"]) == 0, "accepted once is accepted"
    with Catalog(catalog) as opened:
        first = list(reversed(audit.entries(opened.conn)))[0]
        assert first["action"] == "disclaimer.accepted", "accepted before anything was read"
        detail = json.loads(first["detail_json"])
        assert detail == {"how": "flag", "text_sha256": disclaimer.text_sha256()}


def test_the_prompt_accepts_or_refuses(
    tmp_path: Path, sample_tree: Path, capsys, monkeypatch, no_scripted_acceptance
) -> None:
    catalog = tmp_path / "cat" / "catalog.db"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    monkeypatch.setattr("builtins.input", lambda _prompt: "no thanks")
    assert main(["--catalog", str(catalog), "scope", "add", str(sample_tree)]) == 2
    assert _roots(catalog) == []

    monkeypatch.setattr("builtins.input", lambda _prompt: "accept")
    assert main(["--catalog", str(catalog), "scope", "add", str(sample_tree)]) == 0
    assert "regulated data" in " ".join(capsys.readouterr().out.split())
    assert len(_roots(catalog)) == 1


def test_new_wording_must_be_accepted_again(
    tmp_path: Path, sample_tree: Path, monkeypatch, no_scripted_acceptance
) -> None:
    from drivefusion.core import disclaimer

    catalog = tmp_path / "cat" / "catalog.db"
    assert main(["--catalog", str(catalog), "--accept-disclaimer", "scope", "add",
                 str(sample_tree)]) == 0
    monkeypatch.setattr(disclaimer, "TEXT", disclaimer.TEXT + " Revised.")
    assert main(["--catalog", str(catalog), "scan"]) == 2


# -- drive metadata (ROADMAP M5.2) ------------------------------------------------

def test_drives_edit_and_show(cli, sample_tree: Path, capsys) -> None:
    assert cli("--accept-disclaimer", "scope", "add", str(sample_tree)) == 0
    capsys.readouterr()
    assert cli("drives", "edit", "1", "--purchase-date", "2021-03-14", "--price", "129.99",
               "--role", "archive", "--location", "Shelf B") == 0
    assert "recorded purchase-date, price, role, location" in capsys.readouterr().out

    assert cli("drives", "edit", "1", "--price", "cheap") == 1
    assert "not a price" in capsys.readouterr().err
    assert cli("drives", "edit", "1", "--location", "x", "--clear", "location") == 2
    assert cli("drives", "edit", "1") == 2
    capsys.readouterr()

    assert cli("drives", "show", "1") == 0
    out = capsys.readouterr().out
    assert "purchased:       2021-03-14" in out and "price:           129.99" in out
    assert "role:            archive" in out
    assert "failure rate:" in out and "a year (estimated; base" in out
    assert "no SMART reading" in out
    assert cli("drives", "show", "99") == 1


def test_failure_rates_are_imported_from_a_file_listed_and_forgotten(
        cli, tmp_path: Path, capsys) -> None:
    stats = tmp_path / "data_Q3_2025.csv"
    rows = ["date,serial_number,model,capacity_bytes,failure"]
    rows += [f"2025-07-01,S{i},ST4000DM000,4000787030016,{int(i < 2)}" for i in range(40_000)]
    stats.write_text("\n".join(rows) + "\n")

    assert cli("standards", "import-afr", str(stats)) == 0
    assert "imported backblaze-2025Q3: 1 models" in capsys.readouterr().out
    assert cli("standards", "list") == 0
    out = capsys.readouterr().out
    assert out.index("backblaze-2025Q3: fleet, imported") < out.index("provisional-1: provisional, shipped")

    assert cli("standards", "forget", "provisional-1") == 1
    assert cli("standards", "forget", "backblaze-2025Q3") == 0
    capsys.readouterr()
    assert cli("standards", "list") == 0
    assert "backblaze-2025Q3" not in capsys.readouterr().out


def test_an_import_that_is_not_drive_stats_says_so(cli, tmp_path: Path, capsys) -> None:
    other = tmp_path / "data_Q3_2025.csv"
    other.write_text("name,size\nx,1\n")
    assert cli("standards", "import-afr", str(other)) == 1
    assert "no model and failure columns" in capsys.readouterr().err
    bare = tmp_path / "stats.csv"
    bare.write_text("x")
    assert cli("standards", "import-afr", str(bare)) == 2


def test_the_durability_report_gives_expected_loss(cli, sample_tree: Path, capsys) -> None:
    assert cli("--accept-disclaimer", "scope", "add", str(sample_tree)) == 0
    assert cli("scan") == 0 and cli("hash") == 0
    capsys.readouterr()
    assert cli("report", "durability") == 0
    out = capsys.readouterr().out
    assert "expected loss:" in out and "a year (" in out and "P(loss)" in out
    assert "no SMART readings" in out
    assert cli("report", "redundancy") == 0
    assert "most bytes at risk first" in capsys.readouterr().out


def test_an_invalid_role_is_refused_by_the_parser(cli, capsys) -> None:
    with pytest.raises(SystemExit):
        cli("drives", "edit", "1", "--role", "backup")


# -- drive health (ROADMAP M5.3) ---------------------------------------------------

def test_health_is_shelved(cli, capsys) -> None:
    with pytest.raises(SystemExit):
        cli("health", "read")
    assert "invalid choice: 'health'" in capsys.readouterr().err


@pytest.fixture
def health_env(cli, sample_tree: Path, monkeypatch, capsys):
    """A catalogued drive, smartctl 'bundled', and every device 'connected'."""
    from drivefusion.core.enum.helper import client
    from drivefusion.core.health import smart

    assert cli("--accept-disclaimer", "scope", "add", str(sample_tree)) == 0
    capsys.readouterr()
    fixture = (Path(__file__).parent / "fixtures" / "smart" / "ata_hdd_healthy.json").read_text()
    runs = []
    monkeypatch.setattr(smart, "ENABLED", True)      # shelved by default
    monkeypatch.setattr(client, "is_elevated", lambda: True)
    monkeypatch.setattr(smart, "bundled_smartctl", lambda: Path("smartctl.exe"))
    monkeypatch.setattr(smart, "device_for_drive", lambda c, d: ("/dev/pd1", ""))
    monkeypatch.setattr(smart, "run", lambda *a, **k: runs.append(a) or fixture)
    return runs


def test_health_needs_admin_and_says_so(cli, health_env, monkeypatch, capsys) -> None:
    from drivefusion.core.enum.helper import client

    monkeypatch.setattr(client, "is_elevated", lambda: False)
    assert cli("health", "read") == 1
    err = capsys.readouterr().err
    assert "warning" in err and "administrator" in err and "nothing was read" in err
    assert health_env == [], "smartctl ran without admin rights"


def test_health_read_asks_first(cli, health_env, monkeypatch, capsys) -> None:
    assert cli("health", "read") == 2, "no terminal and no --yes: refused"
    assert "pass --yes" in capsys.readouterr().err
    assert health_env == []

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert cli("health", "read") == 0
    out = capsys.readouterr().out
    assert "read-only" in out and "--json=c -i -H -A -l selftest" in out and "not read" in out
    assert health_env == []

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert cli("health", "read") == 0
    assert "drive 1: OK" in capsys.readouterr().out and len(health_env) == 1


def test_health_read_yes_then_show(cli, health_env, capsys) -> None:
    assert cli("health", "read", "--yes") == 0
    capsys.readouterr()
    assert cli("health", "show") == 0
    out = capsys.readouterr().out
    assert "drive 1: OK (measured" in out and "power-on hours:  12000" in out


def test_health_read_without_a_bundled_smartctl(cli, health_env, monkeypatch, capsys) -> None:
    from drivefusion.core.health import smart

    monkeypatch.setattr(smart, "bundled_smartctl", lambda: None)
    assert cli("health", "read", "--yes") == 1
    assert "no bundled smartctl" in capsys.readouterr().err
