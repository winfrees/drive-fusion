"""A campaign's summary and dashboard, built from the runs on disk.

``write(campaign)`` reads campaign.json and each runs/NNN/results.json and
writes:

* ``summary.json``: per run its seed, verdict and counts; per requirement its
  verdict in each run and overall; per suite how many requirements passed in
  every run, failed in any, or could not be judged;
* ``dashboard.html``: one self-contained page (no scripts, no network) with
  the overall verdict, a card per suite, a requirement × run matrix, every
  failure's details, and links to each run's report;
* ``junit.xml``: every run's tests, one ``<testsuite>`` per run, for CI.

It can be rebuilt at any time from the folder (``python -m vv dashboard
CAMPAIGN``), so a campaign copied to another machine stays readable.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

from vv import suites as suites_mod
from vv.evaluate import DEFERRED, ERROR, FAIL, NA, PASS
from vv.requirements import BY_ID

ORDER = (ERROR, FAIL, PASS, DEFERRED, NA)
COLOURS = {PASS: "#1a7f37", FAIL: "#cf222e", ERROR: "#8250df", NA: "#8c959f",
           DEFERRED: "#bf8700", "—": "#d0d7de"}


def _worst(verdicts) -> str:
    verdicts = [v for v in verdicts if v != "—"]
    for v in (ERROR, FAIL):
        if v in verdicts:
            return v
    if PASS in verdicts:
        return PASS
    return DEFERRED if DEFERRED in verdicts else NA


def load(campaign: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
    runs = []
    for entry in meta.get("runs", []):
        path = campaign / "runs" / f"{entry['run']:03d}" / "results.json"
        results = (json.loads(path.read_text(encoding="utf-8"))["results"]
                   if path.is_file() else [])
        runs.append({**entry, "results": results})
    return meta, runs


def summarise(meta: dict, runs: list[dict]) -> dict:
    chosen = meta["config"]["suites"]
    wanted = sorted(suites_mod.requirements(chosen))
    per_req: dict[str, dict] = {}
    for req in wanted:
        cells = []
        for run in runs:
            rs = [r for r in run["results"] if req in r["requirements"]]
            cells.append(_worst(r["verdict"] for r in rs) if rs else
                         (ERROR if run["verdict"] == ERROR else "—"))
        per_req[req] = {"suite": suites_mod.suite_of(req), "runs": cells,
                        "overall": _worst(cells) if any(c != "—" for c in cells) else NA,
                        "text": BY_ID[req].text if req in BY_ID else ""}
    per_suite = {}
    for name in chosen:
        reqs = [r for r, v in per_req.items() if v["suite"] == name]
        per_suite[name] = {
            "title": suites_mod.SUITES[name].title,
            "requirements": len(reqs),
            "passed": sum(per_req[r]["overall"] == PASS for r in reqs),
            "failed": sum(per_req[r]["overall"] in (FAIL, ERROR) for r in reqs),
            "not_judged": sum(per_req[r]["overall"] in (NA, DEFERRED) for r in reqs),
        }
    run_rows = []
    for run in runs:
        counts = defaultdict(int)
        for r in run["results"]:
            counts[r["verdict"]] += 1
        run_rows.append({"run": run["run"], "seed": run.get("seed"), "verdict": run["verdict"],
                         "counts": dict(counts), "error": run.get("error"),
                         "started_at": run.get("started_at"),
                         "finished_at": run.get("finished_at")})
    finished = [r for r in runs if r["verdict"] in (PASS, FAIL)]
    verdict = (ERROR if any(r["verdict"] == ERROR for r in runs) else
               FAIL if any(r["verdict"] == FAIL for r in runs) else
               PASS if finished else NA)
    return {"format": "drivefusion-vv-summary/1", "campaign": meta["id"], "verdict": verdict,
            "suites": per_suite, "requirements": per_req, "runs": run_rows,
            "tool": next((r.get("tool") for r in runs if r.get("tool")), {}),
            "config": meta["config"]}


def _cell(v: str, href: str | None = None, title: str = "") -> str:
    text = {"—": "·", NA: "n/a", DEFERRED: "later"}.get(v, v)
    body = f'<span class=v style="background:{COLOURS.get(v, "#888")}">{html.escape(text)}</span>'
    if href:
        body = f'<a href="{html.escape(href)}" title="{html.escape(title)}">{body}</a>'
    return f"<td>{body}</td>"


def page(meta: dict, runs: list[dict], summary: dict) -> str:
    cfg = summary["config"]
    e = html.escape
    drives = (", ".join(cfg["drive_paths"]) if cfg["drive_paths"]
              else f"{cfg['drives']} simulated" + (f" × {cfg['drive_size']}" if cfg.get("drive_size") else ""))
    head = (f"<h1>Drive Fusion V&amp;V campaign <code>{e(summary['campaign'])}</code></h1>"
            f"<p class=big>{_cell(summary['verdict'])[4:-5]} "
            f"{len(runs)} run(s) of {cfg['iterations']} · drives: {e(drives)} · "
            f"suites: {e(', '.join(cfg['suites']))} · tool "
            f"{e(str(summary['tool'].get('version', '?')))} · bench {e(meta['bench'])} · "
            f"{e(meta.get('started_at', ''))} → {e(meta.get('finished_at', 'running'))}</p>")
    cards = "".join(
        f"<div class=card><b>{e(s['title'])}</b><br>{s['passed']}/{s['requirements']} passed"
        + (f" · <span style='color:{COLOURS[FAIL]}'>{s['failed']} failed</span>" if s['failed'] else "")
        + (f" · {s['not_judged']} not judged" if s['not_judged'] else "") + "</div>"
        for s in summary["suites"].values())
    header = "".join(f"<th><a href='runs/{r['run']:03d}/report.md'>{r['run']}</a></th>" for r in runs)
    rows = []
    for req, info in summary["requirements"].items():
        cells = "".join(_cell(v, f"runs/{runs[i]['run']:03d}/report.md", f"seed {runs[i].get('seed')}")
                        for i, v in enumerate(info["runs"]))
        rows.append(f"<tr><th class=req title='{e(info['text'])}'>{e(req)}</th>"
                    f"<td class=s>{e(info['suite'] or '')}</td>{_cell(info['overall'])}{cells}</tr>")
    matrix = (f"<table><thead><tr><th>Requirement</th><th>Suite</th><th>All runs</th>{header}"
              f"</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    run_rows = "".join(
        f"<tr><td><a href='runs/{r['run']:03d}/report.md'>{r['run']}</a></td><td><code>{e(str(r['seed']))}</code></td>"
        f"{_cell(r['verdict'])}<td>{e(', '.join(f'{n} {v}' for v, n in sorted(r['counts'].items())))}</td>"
        f"<td>{e(r.get('error') or '')}</td><td><a href='runs/{r['run']:03d}/log.txt'>log</a></td></tr>"
        for r in summary["runs"])
    failures = []
    for run in runs:
        for r in run["results"]:
            if r["verdict"] in (FAIL, ERROR):
                details = "".join(f"<li>{e(d)}</li>" for d in r["details"])
                failures.append(f"<details><summary>run {run['run']} · {e(r['id'])}@{e(r['phase'])} · "
                                f"{e(r['title'])}</summary><ul>{details}</ul></details>")
        if run.get("error"):
            failures.append(f"<details><summary>run {run['run']} · the bench stopped</summary>"
                            f"<pre>{e(run['error'])}</pre></details>")
    style = ("body{font:14px system-ui,sans-serif;margin:1.5em;max-width:90em}"
             "table{border-collapse:collapse;margin:.5em 0 1.5em}td,th{border:1px solid #d0d7de;"
             "padding:.15em .4em;text-align:left}th.req{font-family:monospace}td.s{color:#57606a}"
             ".v{color:#fff;border-radius:3px;padding:0 .35em;font-size:12px}a{color:inherit}"
             ".card{display:inline-block;border:1px solid #d0d7de;border-radius:6px;padding:.5em .8em;"
             "margin:.2em}.big{font-size:16px}details{margin:.2em 0}")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<title>V&amp;V {e(summary['campaign'])}</title><style>{style}</style></head><body>"
            f"{head}<h2>Suites</h2>{cards}<h2>Requirements × runs</h2>{matrix}"
            f"<h2>Runs</h2><table><tr><th>Run</th><th>Seed</th><th>Verdict</th><th>Tests</th>"
            f"<th>Error</th><th></th></tr>{run_rows}</table>"
            f"<h2>Discrepancies</h2>{''.join(failures) or '<p>None.</p>'}"
            "<p>Each run's <code>report.md</code> traces every requirement to its tests; "
            "<code>summary.json</code> and <code>junit.xml</code> hold the same for tools.</p>"
            "</body></html>\n")


def junit(runs: list[dict]) -> str:
    from vv.report import junit as run_junit

    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<testsuites>"]
    for run in runs:
        body = run_junit(run["results"]).split("\n", 1)[1].rstrip()
        parts.append(body.replace('<testsuite name="drivefusion-vv"',
                                  f'<testsuite name="drivefusion-vv run {run["run"]} seed {run.get("seed")}"', 1))
    parts.append("</testsuites>")
    return "\n".join(parts) + "\n"


def write(campaign: Path) -> Path:
    campaign = Path(campaign)
    meta, runs = load(campaign)
    summary = summarise(meta, runs)
    (campaign / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (campaign / "junit.xml").write_text(junit(runs), encoding="utf-8")
    path = campaign / "dashboard.html"
    path.write_text(page(meta, runs, summary), encoding="utf-8")
    return path
