"""Coverage and the unfiled bucket (ROADMAP M6.4)."""

from __future__ import annotations

import datetime as dt
import shutil

import pytest

from drivefusion.cli.main import main
from drivefusion.core.analysis import redundancy_summary
from drivefusion.core.curate import confirm, coverage, infer
from drivefusion.core.health import durability
from drivefusion.core.store import curation
from tests.conftest import _write_dated


def _accept(catalog, name: str) -> int:
    infer.infer(catalog)
    for row in curation.candidates(catalog):
        if name in row["evidence_json"]:
            return confirm.accept(catalog, row["id"])
    raise AssertionError(name)


def test_nothing_curated_is_all_unfiled_and_says_so(archive) -> None:
    catalog, _ = archive
    found = coverage.coverage(catalog)
    assert found.share == 0.0 and found.filed.items == 0
    summary = redundancy_summary(catalog)
    assert (found.total.items, found.total.bytes) == (summary["distinct_content"],
                                                      summary["unique_bytes"])
    assert found.unfiled.bytes == found.total.bytes


def test_a_collection_moves_its_bytes_out_of_unfiled(archive) -> None:
    catalog, _ = archive
    june = _accept(catalog, "2021-06 Lake Survey")
    found = coverage.coverage(catalog)
    june_bytes = curation.member_stats(catalog, june)["bytes"]
    assert found.filed.bytes == june_bytes == found.collections[june].bytes
    assert found.filed.bytes + found.unfiled.bytes == found.total.bytes
    assert found.share == pytest.approx(june_bytes / found.total.bytes)
    assert found.filed.expected_loss + found.unfiled.expected_loss == \
        pytest.approx(durability.exposure(catalog).expected_bytes_per_year)
    assert found.unfiled.under_protected_bytes == found.unfiled.bytes, "one drive: all exposed"


def test_content_with_a_copy_in_a_collection_counts_as_filed_once(archive, tmp_path) -> None:
    catalog, path = archive
    june = _accept(catalog, "2021-06 Lake Survey")
    root = tmp_path / "archive"
    shutil.copy2(root / "Projects/Surveys/2021-06 Lake Survey/station_00.csv",
                 root / "Misc/copy_of_station.csv")
    assert main(["--catalog", str(path), "scan"]) == 0
    assert main(["--catalog", str(path), "hash"]) == 0
    found = coverage.coverage(catalog)
    summary = redundancy_summary(catalog)
    assert found.total.items == summary["distinct_content"]
    assert found.filed.items == found.collections[june].items == 8, "the copy adds no item"
    assert found.filed.bytes == curation.member_stats(catalog, june)["bytes"]


def test_the_cli_reports_coverage_and_the_unfiled_bucket(archive, capsys) -> None:
    catalog, path = archive
    _accept(catalog, "2021-03 Lake Survey")
    assert main(["--catalog", str(path), "collections", "coverage"]) == 0
    out = capsys.readouterr().out
    assert "of distinct bytes are in confirmed collections" in out
    assert "unfiled" in out and "expected loss" in out and "never mean to curate" in out
    assert main(["--catalog", str(path), "report", "summary"]) == 0
    assert "collections:" in capsys.readouterr().out
