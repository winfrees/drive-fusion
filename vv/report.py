"""Write a run's verdicts as Markdown for people, JSON for diffing, JUnit for CI."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from vv.evaluate import DEFERRED, ERROR, FAIL, NA, PASS
from vv.requirements import REQUIREMENTS

ICON = {PASS: "✅ PASS", FAIL: "❌ FAIL", ERROR: "⛔ ERROR", NA: "➖ N/A", DEFERRED: "⏳ DEFERRED"}


def overall(results: list[dict]) -> str:
    verdicts = {r["verdict"] for r in results}
    return FAIL if verdicts & {FAIL, ERROR} else PASS


def requirement_verdict(results: list[dict]) -> str:
    verdicts = [r["verdict"] for r in results]
    for v in (ERROR, FAIL):
        if v in verdicts:
            return v
    if PASS in verdicts:
        return PASS
    return DEFERRED if DEFERRED in verdicts else NA


def markdown(manifest: dict, observed: dict, results: list[dict]) -> str:
    counts = Counter(r["verdict"] for r in results)
    by_req = defaultdict(list)
    for r in results:
        for req in r["requirements"]:
            by_req[req].append(r)
    tool = observed.get("tool", {})
    kinds = Counter(s["kind"] for s in manifest["scenarios"])

    lines = [
        "# Drive Fusion V&V report",
        "",
        f"**Verdict: {ICON[overall(results)]}** — "
        + ", ".join(f"{counts[v]} {v}" for v in (PASS, FAIL, ERROR, NA, DEFERRED) if counts[v]),
        "",
        "| | |", "|---|---|",
        f"| Tool | drivefusion {tool.get('version', '?')} (`{tool.get('source', '?')}`) |",
        f"| Adapter | {observed.get('adapter', '?')} |",
        f"| Dataset seed | `{manifest['seed']}` |",
        f"| Drive mode | {manifest['mode']} |",
        f"| Drives | " + ", ".join(f"{d['id']} {d['fs']}" for d in manifest["drives"]) + " |",
        f"| Files / dirs / distinct contents | {len(manifest['files']):,} / {len(manifest['dirs']):,}"
        f" / {len(manifest['contents']):,} |",
        f"| Copy policy | {observed.get('min_copies', 2)} copies on distinct drives |",
        f"| Run | {observed.get('started_at', '?')} → {observed.get('finished_at', '?')} |",
        "",
        "## Requirements traceability",
        "",
        "| Requirement | PLAN | Verdict | Tests |",
        "|---|---|---|---|",
    ]
    for req in REQUIREMENTS:
        rs = by_req.get(req.id, [])
        tests = ", ".join(sorted({f"{r['id']}@{r['phase']}" for r in rs})) or "—"
        lines.append(f"| **{req.id}** {req.text} | {req.section} | "
                     f"{ICON[requirement_verdict(rs)] if rs else ICON[NA]} | {tests} |")

    lines += ["", "## Test results", "",
              "| Test | Phase | Verdict | Checked | Title |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['phase']} | {ICON[r['verdict']]} | {r['checked']:,} | "
                     f"{r['title']} |")

    problems = [r for r in results if r["verdict"] in (FAIL, ERROR)]
    if problems:
        lines += ["", "## Discrepancies", ""]
        for r in problems:
            lines += [f"### {r['id']} @ {r['phase']} — {r['title']}", ""]
            lines += [f"- {d}" for d in r["details"]]
            if r["note"]:
                lines += ["", f"_{r['note']}_"]
            lines.append("")

    notes = [r for r in results if r["verdict"] in (NA, DEFERRED) or r["note"]]
    if notes:
        lines += ["", "## Notes", ""]
        lines += [f"- **{r['id']}@{r['phase']}** ({r['verdict']}): {r['note']}" for r in notes if r["note"]]

    lines += ["", "## Scenarios planted in this dataset", "",
              "| Scenario | Instances | Requirements |", "|---|---|---|"]
    reqs_of = {s["kind"]: s["requirements"] for s in manifest["scenarios"]}
    for kind, n in sorted(kinds.items()):
        lines.append(f"| {kind} | {n} | {', '.join(reqs_of[kind])} |")
    lines += ["", f"Reproduce this dataset: `python -m vv generate --seed {manifest['seed']} "
              f"{_params_cli(manifest)}`", ""]
    return "\n".join(lines)


def _params_cli(manifest: dict) -> str:
    p = manifest["params"]
    parts = [f"--files-per-drive {p['files_per_drive']}", f"--large-files {p['large_files']}"]
    if manifest["mode"] == "physical":
        parts += [f"--drive-path {d['id']}=<path>" for d in manifest["drives"]]
    else:
        parts.insert(0, f"--drives {len(manifest['drives'])}")
    return " ".join(parts) + " --out <new folder>"


def junit(results: list[dict]) -> str:
    failures = sum(r["verdict"] == FAIL for r in results)
    errors = sum(r["verdict"] == ERROR for r in results)
    skipped = sum(r["verdict"] in (NA, DEFERRED) for r in results)
    out = [f'<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuite name="drivefusion-vv" tests="{len(results)}" failures="{failures}" '
           f'errors="{errors}" skipped="{skipped}">']
    for r in results:
        name = escape(f"{r['id']}@{r['phase']} {r['title']}", {'"': "&quot;"})
        out.append(f'  <testcase classname="vv.{escape(",".join(r["requirements"]) or "phase")}" name="{name}">')
        body = escape("\n".join(r["details"]))
        if r["verdict"] == FAIL:
            out.append(f'    <failure message="{len(r["details"])} discrepancies">{body}</failure>')
        elif r["verdict"] == ERROR:
            out.append(f'    <error message="phase error">{body}</error>')
        elif r["verdict"] in (NA, DEFERRED):
            out.append(f'    <skipped message="{escape(r["note"], {chr(34): "&quot;"})}"/>')
        out.append("  </testcase>")
    out.append("</testsuite>")
    return "\n".join(out) + "\n"


def write(run_dir: Path, manifest: dict, observed: dict, results: list[dict]) -> Path:
    (run_dir / "results.json").write_text(
        json.dumps({"verdict": overall(results), "results": results}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    (run_dir / "junit.xml").write_text(junit(results), encoding="utf-8")
    path = run_dir / "report.md"
    path.write_text(markdown(manifest, observed, results), encoding="utf-8")
    return path
