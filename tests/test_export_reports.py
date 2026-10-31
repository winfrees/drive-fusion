"""HTML and CSV reports (ROADMAP M8.5)."""

from __future__ import annotations

import csv
import re

from drivefusion.core.analysis import redundancy_summary
from drivefusion.core.export import reports, writer
from drivefusion.core.planner import document
from tests.test_export_manifests import fleet  # noqa: F401  (the fixture)


def test_reports_match_the_analysis_and_stand_alone(fleet) -> None:  # noqa: F811
    document.save(fleet.c, document.make(fleet.c, use_cpsat=False))
    bundle = writer.open_bundle(fleet.c, fleet.out, "reports")
    counts = reports.write(bundle, fleet.c)
    bundle.close(summary=counts)
    names = {p.name for p in bundle.path.iterdir()}
    assert set(reports.TITLES) | {"report.html", writer.MANIFEST} == names
    summary = dict(list(csv.reader((bundle.path / "summary.csv").open(encoding="utf-8", newline="")))[1:])
    for key, value in redundancy_summary(fleet.c).items():
        assert summary[key] == str(value), key
    under = list(csv.DictReader((bundle.path / "under-protected.csv").open(encoding="utf-8", newline="")))
    assert len(under) == int(summary["under_protected_items"])
    assert counts["plan.csv"] >= 1 and counts["scorecards.csv"] > 30
    page = (bundle.path / "report.html").read_text(encoding="utf-8")
    assert "<script" not in page.lower()
    assert not re.search(r"""(href|src)\s*=\s*["']?https?:""", page, re.I)
    assert "consistent with" in page and "Under-protected items" in page


def test_html_escapes_what_it_shows(fleet) -> None:  # noqa: F811
    fleet.c.conn.execute("UPDATE collection SET title = '<b>x</b>'")
    found = reports.tables(fleet.c)
    page = reports.page(found, generated="now")
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
