"""Naming-template mappings (PLAN §3.3, §10, ROADMAP M8.4).

A reorganisation is proposed, never performed: per collection, a template
renders every member's new path, and the export holds the mapping
(``mapping.csv``, old → new) and its inverse (``inverse.csv``, new → old)
for a person to review, diff, archive and carry out with their own tools.

Template fields: ``{collection}`` (the title, made safe for a folder name),
``{pid}``, ``{yyyy}``, ``{mm}``, ``{dd}`` (the file's modified date, or
``undated``), ``{name}``, ``{stem}``, ``{ext}`` (without the dot) and
``{path}`` (the file's path below the collection folder). The default is
``{collection}/{yyyy}/{name}``. New paths are relative to wherever the person
puts them. Two files that would land on one path keep distinct ones: the
later gets `` (2)``, `` (3)``… before its extension, so the mapping is always
one to one and its inverse always restores every path.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import string
from pathlib import PurePosixPath

from drivefusion.core.export.manifests import _Paths, _folder_name
from drivefusion.core.export.writer import ExportError
from drivefusion.core.store import curation

DEFAULT_TEMPLATE = "{collection}/{yyyy}/{name}"
FIELDS = {"collection", "pid", "yyyy", "mm", "dd", "name", "stem", "ext", "path"}


def check_template(template: str) -> None:
    used = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    unknown = used - FIELDS
    if unknown:
        raise ExportError(f"unknown template field(s) {sorted(unknown)}; use {sorted(FIELDS)}")
    if template.startswith("/") or ".." in template.split("/"):
        raise ExportError("a template renders a relative path, without '..'")


def _safe(part: str) -> str:
    return "".join("_" if c in '<>:"\\|?*' or ord(c) < 32 else c for c in part).strip() or "_"


def mapping(catalog, collection_id: int, template: str = DEFAULT_TEMPLATE) -> list[tuple]:
    """``[(old_path, new_path)]`` for every live member, one to one."""
    check_template(template)
    coll = curation.collection(catalog, collection_id)
    paths = _Paths(catalog)
    rows = []
    for rule in curation.rules(catalog, collection_id):
        base = paths(rule["dir_id"])
        rows += [(base, r) for r in catalog.conn.execute(
            curation._MEMBER_DIRS.replace("collection_id = ?", "id = ?") +
            " SELECT f.dir_id, f.name, f.mtime_ns FROM file f WHERE f.vanished_at IS NULL "
            "AND f.dir_id IN (SELECT id FROM tree) ORDER BY f.dir_id, f.name", (rule["id"],))]
    taken: set[str] = set()
    seen: set[str] = set()
    out = []
    for base, r in rows:
        old = f"{paths(r['dir_id'])}/{r['name']}"
        if old in seen:
            continue
        seen.add(old)
        below = old[len(base):].lstrip("/") if old.startswith(base) else r["name"]
        when = (dt.datetime.fromtimestamp(r["mtime_ns"] / 1e9, dt.timezone.utc)
                if r["mtime_ns"] else None)
        name = PurePosixPath(r["name"])
        new = template.format(
            collection=_folder_name(coll["title"]), pid=coll["local_pid"],
            yyyy=f"{when:%Y}" if when else "undated", mm=f"{when:%m}" if when else "undated",
            dd=f"{when:%d}" if when else "undated", name=r["name"], stem=name.stem,
            ext=name.suffix.lstrip("."), path=below)
        new = "/".join(_safe(p) for p in new.split("/") if p not in ("", "."))
        if new.lower() in taken:
            head, _, leaf = new.rpartition("/")
            stem, dot, ext = leaf.rpartition(".") if "." in leaf.lstrip(".") else (leaf, "", "")
            n = 2
            while True:
                candidate = f"{head + '/' if head else ''}{stem} ({n}){dot}{ext}"
                if candidate.lower() not in taken:
                    new = candidate
                    break
                n += 1
        taken.add(new.lower())
        out.append((old, new))
    return out


def _csv(rows, header) -> str:
    buffer = io.StringIO()
    out = csv.writer(buffer, lineterminator="\n")
    out.writerow(header)
    out.writerows(rows)
    return buffer.getvalue()


def write(bundle, catalog, *, template: str = DEFAULT_TEMPLATE, plan_id: int | None = None) -> dict:
    from drivefusion.core.export import commands
    from drivefusion.core.store import plans

    bundle.checks.append(lambda rel, data: commands.check(rel, data)
                         if rel.endswith(("copy-commands.cmd", "copy-commands.sh")) else None)
    made = 0
    for coll in curation.collections(catalog):
        pairs = mapping(catalog, coll["id"], template)
        folder = coll["local_pid"]
        bundle.write(f"{folder}/mapping.csv", _csv(pairs, ["old_path", "new_path"]))
        bundle.write(f"{folder}/inverse.csv", _csv([(n, o) for o, n in pairs],
                                                   ["new_path", "old_path"]))
        bundle.write(f"{folder}/README.md", (
            f"# {coll['title']}: proposed names\n\n"
            f"Template: `{template}`. `mapping.csv` gives each file's proposed path; "
            "`inverse.csv` undoes it. Drive Fusion renamed and moved nothing: this is a "
            "proposal for you to review and carry out with your own tools, if you choose.\n"))
        made += len(pairs)
    current = plans.current(catalog) if plan_id is None else plans.plan(catalog, plan_id)
    previews = 0
    if current is not None:
        for name, text in commands.previews(catalog, current["id"]).items():
            bundle.write(f"plan-{current['id']}/{name}", text)
            previews += 1
    return {"template": template, "mapped": made, "collections":
            len(curation.collections(catalog)), "command_previews": previews,
            "plan_id": current["id"] if current is not None else None}
