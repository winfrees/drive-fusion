"""Compare what Drive Fusion concluded with what the oracle says it must.

Every test returns a verdict, how many items it examined, and the first
discrepancies, each annotated with the scenario that planted it. Two rules
keep the verdicts honest:

* **A test that examined nothing is N/A, never PASS.** A dataset whose drives
  cannot hold hardlinks has not shown that hardlinks are counted correctly.
* **Identity is compared as a partition of paths, never by id.** Catalog ids
  are internal; "these three paths are one content" is the claim a user acts
  on, so that is what is checked.

Tests that compare redundancy numbers also check identity first, and name a
split or merged identity as the cause when that is why a count is wrong, so
one root cause does not read as five unrelated failures.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from vv import oracle
from vv.requirements import BY_ID

PASS, FAIL, NA, ERROR, DEFERRED = "PASS", "FAIL", "N/A", "ERROR", "DEFERRED"
MAX_DETAILS = 15


@dataclass
class Result:
    id: str
    title: str
    requirements: list[str]
    phase: str
    verdict: str = PASS
    checked: int = 0
    details: list[str] = field(default_factory=list)
    note: str = ""

    def fail(self, message: str) -> None:
        self.verdict = FAIL
        if len(self.details) < MAX_DETAILS:
            self.details.append(message)
        elif len(self.details) == MAX_DETAILS:
            self.details.append("… further discrepancies omitted")

    def finish(self, empty_reason: str = "") -> "Result":
        if self.verdict == PASS and self.checked == 0:
            self.verdict = NA
            self.note = self.note or empty_reason or "nothing in this dataset to check"
        return self


def _when(text: str | None):
    """An ISO timestamp, or None when absent or unreadable."""
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _fmt(key) -> str:
    return f"{key[0]}:{key[1]}"


class Observed:
    """One phase of observations, indexed for comparison."""

    def __init__(self, phase: dict) -> None:
        self.raw = phase
        self.live: dict[tuple, tuple] = {}
        self.vanished: set[tuple] = set()
        for drive, path, size, cid, vanished, state in phase["files"]:
            if vanished:
                self.vanished.add((drive, path))
            else:
                self.live[(drive, path)] = (size, cid, state)
        self.dirs = {(d, p) for d, p, v in phase["dirs"] if not v}
        self.vanished_dirs = {(d, p) for d, p, v in phase["dirs"] if v}
        self.members: dict[int, set] = defaultdict(set)
        for key, (_s, cid, _st) in self.live.items():
            if cid is not None:
                self.members[cid].add(key)
        self.groups = {g["content_id"]: g for g in phase["groups_all"]}

    def cid(self, key):
        entry = self.live.get(key)
        return entry[1] if entry else None

    def path_set(self, cid) -> frozenset:
        return frozenset(self.members.get(cid, ()))


class Evaluator:
    def __init__(self, manifest: dict, observed: dict) -> None:
        self.manifest = manifest
        self.observed = observed
        self.min_copies = observed.get("min_copies", 2)
        self.exp = oracle.expectations(manifest, min_copies=self.min_copies)
        self.results: list[Result] = []
        self.scenario_of: dict[tuple, str] = {}
        kinds = {s["id"]: s["kind"] for s in manifest["scenarios"]}
        for s in manifest["scenarios"]:
            for drive, path in s["members"]:
                self.scenario_of.setdefault((drive, path), s["kind"])
        self.kinds = kinds

    def tag(self, key) -> str:
        kind = self.scenario_of.get(tuple(key))
        return f"{_fmt(key)} [{kind}]" if kind else _fmt(key)

    def new(self, test_id: str, title: str, reqs: list[str], phase: str) -> Result:
        result = Result(test_id, title, reqs, phase)
        self.results.append(result)
        return result

    # -- driver -------------------------------------------------------------

    def run(self) -> list[Result]:
        errors = self.observed.get("errors", {})
        phases = self.observed.get("phases", {})
        self.no_touch()
        for name in ("baseline", "rescan"):
            if name in errors or name not in phases:
                self.phase_error(name, errors.get(name, "phase did not run"))
                continue
            obs, exp = Observed(phases[name]), self.exp[name]
            if name == "baseline":
                self.scope_tests(obs, exp, phases[name], name)
                self.catalog_tests(obs, exp, phases[name])
            else:
                self.scope_tests(obs, exp, phases[name], name, scope_only=True)
                self.rescan_tests(obs, exp)
                self.membership_test(phases[name], exp)
                self.scorecard_tests(phases[name], exp)
                self.plan_tests(phases[name], exp)
            self.last_scanned_test(phases, name)
            self.dirs_test(obs, exp, name)
            self.identity_tests(obs, exp, name)
            self.redundancy_tests(obs, exp, phases[name], name)
        if "baseline" in phases and "baseline" not in errors:
            self.health_tests(phases["baseline"])
            self.curation_tests(phases["baseline"])
            self.version_tests(phases["baseline"])
        if "verify" in errors or "verify" not in phases:
            self.phase_error("verify", errors.get("verify", "phase did not run"))
        else:
            self.fixity_tests(phases["verify"])
        if "export" in errors:
            self.phase_error("export", errors["export"])
        elif "export" in phases:
            self.export_tests(phases["export"], self.observed.get("export_tree", {}))
            self.manifest_tests(phases["export"], self.observed.get("export_tree", {}),
                                phases.get("rescan", {}))
            self.package_tests(phases["export"], self.observed.get("export_tree", {}),
                               phases.get("rescan", {}))
        self.deferred()
        return self.results

    def phase_error(self, phase: str, message: str) -> None:
        r = self.new(f"VV-PHASE-{phase.upper()}", f"{phase} phase completed", [], phase)
        r.verdict = ERROR
        r.details.append(message.strip().splitlines()[-1] if message else "unknown error")
        r.note = "every test of this phase is unverified"

    # -- R-RO ----------------------------------------------------------------

    def no_touch(self) -> None:
        for phase, diffs in self.observed.get("no_touch", {}).items():
            r = self.new("VV-RO-01", "Scanned trees are unchanged by the tool", ["R-RO-01"], phase)
            r.checked = self.observed.get("snapshot_entries", {}).get(phase, 0)
            for d in diffs:
                r.fail(d)
            r.finish()

    # -- R-SC ----------------------------------------------------------------

    def scope_tests(self, obs: Observed, exp: dict, raw: dict, phase: str,
                    scope_only: bool = False) -> None:
        if not scope_only:
            r = self.new("VV-SC-01", "Nothing outside scope is catalogued", ["R-SC-01"], phase)
            r.checked = len(self.manifest["unscoped"])
            for key in list(obs.live) + list(obs.dirs):
                if key[0] == "?":
                    r.fail(f"catalogued outside every scope root: {key[1]}")
            r.finish()

        r = self.new("VV-SC-02", "Default exclusions are not catalogued and are counted",
                     ["R-SC-02"], phase)
        roots = self.manifest["excluded"]["roots"]
        r.checked = len(roots)
        for e in roots:
            key = (e["drive"], e["path"])
            leaked = [k for k in list(obs.live) + list(obs.dirs)
                      if k[0] == key[0] and (k[1] == key[1] or k[1].startswith(key[1] + "/"))]
            for k in leaked[:3]:
                r.fail(f"excluded entry catalogued: {_fmt(k)}")
        for drive, counts in exp["scan"].items():
            got = raw.get("scan", {}).get(drive, {}).get("excluded")
            if got != counts["excluded"]:
                r.fail(f"{drive}: scan reported {got} excluded entries, expected {counts['excluded']}")
        r.finish()

        r = self.new("VV-SC-03", "Links are reported and never traversed", ["R-SC-03"], phase)
        links = self.manifest["links"]
        r.checked = len(links)
        for link in links:
            for k in list(obs.live) + list(obs.dirs):
                if k[0] == link["drive"] and (k[1] == link["path"] or k[1].startswith(link["path"] + "/")):
                    r.fail(f"catalogued through link {link['path']}: {_fmt(k)}")
                    break
        for drive, counts in exp["scan"].items():
            if not counts["links"]:
                continue
            got = raw.get("scan", {}).get(drive, {}).get("links")
            if got != counts["links"]:
                r.fail(f"{drive}: scan reported {got} links not traversed, expected {counts['links']}")
        r.finish("no drive in this dataset supports symlinks")

    # -- R-CAT ---------------------------------------------------------------

    def _file_set(self, r: Result, obs: Observed, exp: dict) -> None:
        r.checked = len(exp["files"])
        for key, e in sorted(exp["files"].items()):
            if key not in obs.live:
                r.fail(f"missing: {self.tag(key)}")
            elif obs.live[key][0] != e["size"]:
                r.fail(f"size {obs.live[key][0]} != {e['size']}: {self.tag(key)}")
        for key in sorted(set(obs.live) - set(exp["files"])):
            r.fail(f"unexpected file catalogued: {_fmt(key)}")

    def catalog_tests(self, obs: Observed, exp: dict, raw: dict) -> None:
        phase = "baseline"
        r = self.new("VV-CAT-01", "Every in-scope file is catalogued with path and size",
                     ["R-CAT-01"], phase)
        self._file_set(r, obs, exp)
        r.finish()

        r = self.new("VV-CAT-03", "Empty files are catalogued and never grouped as duplicates",
                     ["R-CAT-03"], phase)
        for key, e in exp["files"].items():
            if e["size"] == 0:
                r.checked += 1
                if key not in obs.live:
                    r.fail(f"missing empty file: {_fmt(key)}")
                elif obs.live[key][0] != 0:
                    r.fail(f"empty file catalogued with size {obs.live[key][0]}: {_fmt(key)}")
                elif obs.live[key][1] is not None and len(obs.members[obs.live[key][1]]) > 1:
                    r.fail(f"empty file grouped with {len(obs.members[obs.live[key][1]]) - 1} "
                           f"other path(s) as one content: {_fmt(key)}")
        r.finish()

        for test_id, title, req, kinds in (
            ("VV-CAT-04", "Unusual names are stored exactly as observed", "R-CAT-04",
             ("odd-names", "case-variant")),
            ("VV-CAT-05", "Paths beyond 260 characters are catalogued", "R-CAT-05", ("deep-path",)),
        ):
            r = self.new(test_id, title, [req], phase)
            for s in self.manifest["scenarios"]:
                if s["kind"] not in kinds:
                    continue
                for drive, path in s["members"]:
                    if (drive, path) not in exp["files"]:
                        continue
                    if req == "R-CAT-05" and len(self.root(drive)) + len(path) + 1 <= 260:
                        continue
                    r.checked += 1
                    if (drive, path) not in obs.live:
                        r.fail(f"missing: {drive}:{path!r}")
            r.finish()

        r = self.new("VV-CAT-06", "Unreadable files are recorded, not fatal, and not identified",
                     ["R-CAT-06"], phase)
        for key, e in exp["files"].items():
            if e["unreadable"]:
                r.checked += 1
                if key not in obs.live:
                    r.fail(f"unreadable file missing from catalog: {_fmt(key)}")
                elif obs.live[key][1] is not None:
                    r.fail(f"unreadable file was given a content identity: {_fmt(key)}")
        r.finish("no drive in this dataset can hold an unreadable file "
                 "(Windows, or running as administrator/root)")

        r = self.new("VV-CAT-07", "Each physical device is catalogued as a distinct device",
                     ["R-CAT-07"], phase)
        volumes = raw.get("volumes", {})
        r.checked = len(self.manifest["drives"])
        devices = defaultdict(set)
        truth = {d["id"]: d.get("device", d["id"]) for d in self.manifest["drives"]}
        for drive in self.manifest["drives"]:
            v = volumes.get(drive["id"])
            if not v:
                r.fail(f"{drive['id']} was not registered as a volume")
                continue
            devices[v["device"]].add(drive["id"])
        for device, drives in devices.items():
            if len({truth[d] for d in drives}) > 1:
                r.fail(f"drives {', '.join(sorted(drives))} are on different physical "
                       f"devices but resolved to one, {device}")
        for true_device in set(truth.values()):
            seen = {v["device"] for d, v in volumes.items() if truth.get(d) == true_device}
            if len(seen) > 1:
                r.fail(f"the partitions of one device resolved to {len(seen)} devices")
        r.finish()

    def last_scanned_test(self, phases: dict, phase: str) -> None:
        r = self.new("VV-CAT-08", "\"Last scanned\" is when the last scan finished",
                     ["R-CAT-08"], phase)
        volumes = phases[phase].get("volumes", {})
        before = phases.get("baseline", {}).get("volumes", {}) if phase == "rescan" else {}
        started = _when(self.observed.get("started_at"))
        for drive in self.manifest["drives"]:
            v = volumes.get(drive["id"], {})
            if "last_scanned_at" not in v:
                continue
            r.checked += 1
            when = _when(v["last_scanned_at"])
            earlier = _when(before.get(drive["id"], {}).get("last_scanned_at"))
            if when is None:
                r.fail(f"{drive['id']}: scanned in this phase, but shown as never scanned")
            elif started is not None and when < started:
                r.fail(f"{drive['id']}: last scanned {v['last_scanned_at']}, before this run "
                       f"began ({self.observed['started_at']})")
            elif earlier is not None and when < earlier:
                r.fail(f"{drive['id']}: the rescan moved \"last scanned\" backwards")
        r.finish("this version doesn't report when a volume was last scanned")

    def root(self, drive: str) -> str:
        return next(d["root"] for d in self.manifest["drives"] if d["id"] == drive)

    def dirs_test(self, obs: Observed, exp: dict, phase: str) -> None:
        reqs = ["R-CAT-02"] + (["R-INC-02"] if phase == "rescan" else [])
        r = self.new("VV-CAT-02", "Every directory is catalogued, including empty ones", reqs, phase)
        r.checked = len(exp["dirs"])
        for key in sorted(exp["dirs"] - obs.dirs):
            r.fail(f"directory missing: {self.tag(key)}")
        for key in sorted(obs.dirs - exp["dirs"]):
            r.fail(f"unexpected live directory: {self.tag(key)}")
        if phase == "rescan":
            for key in sorted(exp["vanished_dirs"] - obs.vanished_dirs):
                r.fail(f"deleted directory not tombstoned: {self.tag(key)}")
        r.finish()

    # -- R-INC -----------------------------------------------------------------

    def rescan_tests(self, obs: Observed, exp: dict) -> None:
        phase = "rescan"
        ops = self.manifest["mutations"]
        base_files = self.exp["baseline"]["files"]

        r = self.new("VV-INC-01", "A rescan reflects additions and modifications",
                     ["R-INC-01", "R-CAT-01"], phase)
        self._file_set(r, obs, exp)
        for op in ops:
            if op["op"] != "modify":
                continue
            key = (op["drive"], op["path"])
            old = base_files[key]["content"]
            new_cid = obs.cid(key)
            old_cids = {obs.cid(k) for k, e in exp["files"].items()
                        if e["content"] == old and k != key} - {None}
            if new_cid is not None and new_cid in old_cids:
                r.fail(f"modified file kept the identity of its old content: {_fmt(key)}")
        r.finish()

        r = self.new("VV-INC-02", "Deleted files are tombstoned with metadata retained",
                     ["R-INC-02"], phase)
        r.checked = len(exp["vanished"])
        for key in sorted(exp["vanished"]):
            if key in obs.live:
                r.fail(f"deleted file still live: {self.tag(key)}")
            elif key not in obs.vanished:
                r.fail(f"deleted file removed from the catalog instead of tombstoned: {self.tag(key)}")
        for key in sorted(obs.vanished - exp["vanished"]):
            r.fail(f"tombstoned but still on disk: {_fmt(key)}")
        r.finish()

        r = self.new("VV-INC-03", "Moved or renamed content is recognized at its new path",
                     ["R-INC-03"], phase)
        for op in ops:
            if op["op"] != "move":
                continue
            r.checked += 1
            src, dst = (op["drive"], op["path"]), (op["drive"], op["to"])
            content = exp["files"][dst]["content"]
            siblings = [k for k, e in exp["files"].items() if e["content"] == content and k != dst
                        and k in exp["identified"]]
            if dst not in obs.live:
                r.fail(f"new location not catalogued: {_fmt(dst)}")
                continue
            if src not in obs.vanished:
                r.fail(f"old location not tombstoned: {_fmt(src)}")
            if dst not in exp["identified"]:
                continue  # nothing the spec says to hash; VV-ID-03 covers that side
            sib_cids = {obs.cid(k) for k in siblings}
            if obs.cid(dst) is None or (siblings and sib_cids != {obs.cid(dst)}):
                r.fail(f"{_fmt(dst)} not recognized as the content still held at "
                       f"{', '.join(_fmt(k) for k in siblings[:3])}")
        r.finish()

    # -- R-ID ------------------------------------------------------------------

    def identity_tests(self, obs: Observed, exp: dict, phase: str) -> None:
        r = self.new("VV-ID-01", "Byte-identical files share one content identity", ["R-ID-01"], phase)
        for content, g in sorted(exp["groups"].items()):
            if len(g["paths"]) < 2:
                continue
            r.checked += 1
            seen = {obs.cid(k) for k in g["paths"]}
            if len(seen) != 1 or None in seen:
                by_cid = defaultdict(list)
                for k in g["paths"]:
                    by_cid[obs.cid(k)].append(k)
                split = "; ".join(f"id {c}: " + ", ".join(self.tag(k) for k in ks[:3])
                                  for c, ks in by_cid.items())
                r.fail(f"{len(g['paths'])} identical copies split into {len(by_cid)} identities — {split}")
        r.finish()

        r = self.new("VV-ID-02", "Files whose bytes differ never share an identity", ["R-ID-02"], phase)
        r.checked = len(obs.members)
        for cid, keys in sorted(obs.members.items()):
            truths = defaultdict(list)
            for k in keys:
                if k in exp["files"]:
                    truths[exp["files"][k]["content"]].append(k)
            if len(truths) > 1:
                r.fail(f"id {cid} merges {len(truths)} different contents: "
                       + "; ".join(self.tag(ks[0]) for ks in truths.values()))
        hidden = sum(1 for s in self.manifest["scenarios"] if s["kind"] == "same-name-hidden-diff")
        r.note = f"includes {hidden} tier-2 case(s) that differ only outside the quick-hash samples"
        r.finish()

        r = self.new("VV-ID-03", "Only shared-size files are hashed; the rest are reported unhashed",
                     ["R-ID-03"], phase)
        r.checked = len(exp["files"])
        got = {k for k, v in obs.live.items() if v[1] is not None}
        for key in sorted(exp["identified"] - got):
            r.fail(f"expected a content identity, has none: {self.tag(key)}")
        for key in sorted((got - exp["identified"]) & set(exp["files"])):
            r.fail(f"identified although its size is unique (spec: never opened): {self.tag(key)}")
        r.finish()

    # -- R-RED -------------------------------------------------------------------

    def _observed_group(self, obs: Observed, paths) -> tuple[dict | None, str]:
        cids = {obs.cid(k) for k in paths}
        if None in cids or len(cids) != 1:
            return None, f"identity split across {len(cids)} ids"
        cid = cids.pop()
        if obs.path_set(cid) != frozenset(paths):
            return None, "identity merged with other content"
        g = obs.groups.get(cid)
        return (g, "") if g else (None, f"id {cid} missing from the tool's analysis")

    def redundancy_tests(self, obs: Observed, exp: dict, raw: dict, phase: str) -> None:
        r = self.new("VV-RED-01", "Copies are counted over distinct drives, not paths",
                     ["R-RED-01"], phase)
        r2 = self.new("VV-RED-02", "Hardlinks are one physical copy", ["R-RED-02"], phase)
        for content, g in sorted(exp["groups"].items()):
            r.checked += 1
            hardlinked = g["physical"] < len(g["paths"])
            r2.checked += hardlinked
            og, why = self._observed_group(obs, g["paths"])
            label = self.tag(g["paths"][0])
            if og is None:
                r.fail(f"{label}: true copies on {g['drives']} drive(s) — {why} (see VV-ID-01/02)")
                if hardlinked:
                    r2.fail(f"{label}: {why}")
                continue
            if og["drives"] != g["drives"]:
                r.fail(f"{label}: reported on {og['drives']} drive(s), truly on {g['drives']}")
            if hardlinked and og["physical_copies"] != g["physical"]:
                r2.fail(f"{label}: {og['physical_copies']} physical copies reported, "
                        f"{g['physical']} real ({len(g['paths'])} paths)")
        r.finish()
        r2.finish("no drive in this dataset supports hardlinks")

        def as_paths(groups: list[dict]) -> dict[frozenset, dict]:
            # A single-copy group has no content id; the adapter names its file.
            return {frozenset(map(tuple, g["members"])) if g.get("members") is not None
                    else obs.path_set(g["content_id"]): g for g in groups}

        true_sets = {frozenset(g["paths"]) for g in exp["groups"].values()}

        def spurious(ps: frozenset, claim: str) -> str:
            """Say *why* a reported group is not a real one, when it is a fragment."""
            first = sorted(ps)[0]
            if ps in true_sets:
                return f"{claim}: {self.tag(first)}"
            truth = exp["groups"].get(exp["files"].get(first, {}).get("content"), {})
            return (f"{claim}: {self.tag(first)} — the tool's group has {len(ps)} path(s) of "
                    f"a content truly at {len(truth.get('paths', ()))} (identity split or "
                    "merged; see VV-ID-01/02)")

        r = self.new("VV-RED-03", f"Under-protected set is exactly the content on fewer than "
                     f"{self.min_copies} drives", ["R-RED-03"], phase)
        expected = {frozenset(exp["groups"][c]["paths"]): c for c in exp["under_protected"]}
        expected.update({frozenset([k]): k for k in exp["under_protected_singles"]})
        got = as_paths(raw["under_protected"])
        r.checked = len(exp["groups"]) + len(exp["singles"])
        for ps in sorted(set(expected) - set(got), key=sorted):
            r.fail(f"not reported as under-protected: {self.tag(sorted(ps)[0])} "
                   f"({len(ps)} path(s))")
        for ps in sorted(set(got) - set(expected), key=sorted):
            if ps:
                r.fail(spurious(ps, f"reported under-protected on {got[ps]['drives']} drive(s)"))
        r.finish()

        r = self.new("VV-RED-04", "Reclamation is exactly same-drive duplication", ["R-RED-04"], phase)
        expected_r = {frozenset(exp["groups"][c]["paths"]): b for c, b in exp["reclamation"].items()}
        got_r = {ps: g["reclaimable_bytes"] for ps, g in as_paths(raw["reclamation"]).items()}
        r.checked = len(exp["groups"])
        for ps, b in sorted(expected_r.items(), key=lambda x: sorted(x[0])):
            if ps not in got_r:
                r.fail(f"same-drive duplicate not reported: {self.tag(sorted(ps)[0])} ({b:,} bytes)")
            elif got_r[ps] != b:
                r.fail(f"{self.tag(sorted(ps)[0])}: {got_r[ps]:,} reclaimable bytes reported, {b:,} true")
        for ps in sorted(set(got_r) - set(expected_r), key=sorted):
            if ps:
                r.fail(spurious(ps, "reported as same-drive waste"))
        r.finish()

        r = self.new("VV-RED-06", "Volumes count by physical device; an unidentified "
                     "drive adds no proven copy and no waste", ["R-RED-06"], phase)
        reclaimed = {frozenset(map(tuple, ps)) for ps in
                     (exp["groups"][c]["paths"] for c in exp["reclamation"])}
        got_reclaim = set(as_paths(raw["reclamation"]))
        for content, g in sorted(exp["groups"].items()):
            if not g["identity_case"]:
                continue
            r.checked += 1
            og, why = self._observed_group(obs, g["paths"])
            label = self.tag(g["paths"][0])
            if og is None:
                r.fail(f"{label}: {why} (see VV-ID-01/02)")
                continue
            if og["drives"] != g["drives"]:
                r.fail(f"{label}: reported on {og['drives']} drive(s); "
                       f"{g['drives']} proven (by physical device, identified drives only)")
            if og.get("drives_possible") is not None and og["drives_possible"] != g["drives_possible"]:
                r.fail(f"{label}: {og['drives_possible']} possible drive(s) reported, "
                       f"{g['drives_possible']} true")
            ps = frozenset(g["paths"])
            if (ps in got_reclaim) != (ps in reclaimed):
                r.fail(f"{label}: {'reported' if ps in got_reclaim else 'not reported'} as "
                       f"same-drive waste; truly {'is' if ps in reclaimed else 'is not'}")
        r.finish("needs three or more simulated drives (two partitions of one device), "
                 "or four for an unidentified drive")

        r = self.new("VV-RED-05", "Headline numbers match the ground truth", ["R-RED-05"], phase)
        for name, value in exp["summary"].items():
            r.checked += 1
            got_v = raw["summary"].get(name)
            if got_v != value:
                r.fail(f"{name}: reported {got_v:,}, true {value:,}" if isinstance(got_v, int)
                       else f"{name}: reported {got_v!r}, true {value!r}")
        r.finish()

    # -- R-FIX -------------------------------------------------------------------

    def fixity_tests(self, raw: dict) -> None:
        phase = "verify"
        exp = self.exp["verify"]
        incidents = {(i["drive"], i["path"]): i for i in raw["incidents"]}

        r = self.new("VV-FIX-01", "Silent corruption is detected", ["R-FIX-01"], phase)
        r.checked = len(exp["corrupted"])
        for key in sorted(exp["corrupted"]):
            inc = incidents.get(key)
            if inc is None:
                r.fail(f"corruption not reported: {_fmt(key)}")
            elif inc["result"] != "mismatch":
                r.fail(f"reported as {inc['result']!r}, expected 'mismatch': {_fmt(key)}")
        r.finish("no verifiable duplicate content to corrupt in this dataset")

        r = self.new("VV-FIX-02", "Verification has no false positives and covers both tiers",
                     ["R-FIX-02"], phase)
        r.checked = raw["checked"]
        for key, inc in sorted(incidents.items()):
            if key in exp["edited"]:
                continue        # VV-FIX-04's business
            if key not in exp["corrupted"] and inc["result"] == "mismatch":
                r.fail(f"intact file reported as corrupted: {_fmt(key)}")
        if raw["checked"] < exp["min_checked"]:
            r.fail(f"checked {raw['checked']} file(s); at least {exp['min_checked']} are "
                   "verifiable (every identified file at or below the small-file threshold, "
                   "plus the corrupted large files)")
        r.finish()

        r = self.new("VV-FIX-04", "An edit since the last scan is modified, not corrupt",
                     ["R-FIX-04"], phase)
        reported_modified = raw.get("modified")
        modified = (None if reported_modified is None
                    else {tuple(k) for k in reported_modified})
        r.checked = len(exp["edited"])
        for key in sorted(exp["edited"]):
            inc = incidents.get(key)
            if inc is not None:
                r.fail(f"edited file reported as {inc['result']!r}: {_fmt(key)}")
            elif modified is not None and key not in modified:
                r.fail(f"edited file not reported as modified: {_fmt(key)}")
        r.finish("no stable duplicate content left to edit in this dataset")

        r = self.new("VV-FIX-03", "Each incident states how many drives hold the content",
                     ["R-FIX-03"], phase)
        for key, drives in sorted(exp["holding_drives"].items()):
            inc = incidents.get(key)
            if inc is None:
                continue
            r.checked += 1
            if inc["other_copies"] != drives:
                r.fail(f"{_fmt(key)}: incident says {inc['other_copies']} drive(s) hold it, "
                       f"truly {drives}")
        r.finish("no corruption was reported to check")

    # -- R-HL --------------------------------------------------------------------

    #: smartctl options that change a drive, as the bench understands them.
    SMARTCTL_STATE_CHANGING = ("-t", "--test", "-s", "--smart", "-o", "--offlineauto",
                               "-S", "--saveauto", "--set", "-X", "--abort", "-C", "--captive")

    def health_tests(self, raw: dict) -> None:
        import datetime as dt

        phase = "baseline"
        health = raw.get("health")
        titles = {
            "VV-HL-01": ("No SMART is unknown, never a number", ["R-HL-01"]),
            "VV-HL-02": ("Copies that can fail together are worth less", ["R-HL-02"]),
            "VV-HL-03": ("Failure rates and expected loss equal independent arithmetic",
                         ["R-HL-03"]),
            "VV-HL-04": ("smartctl is only ever given read-only options", ["R-HL-04"]),
        }
        if health is None or self.manifest["mode"] != "simulated":
            for test_id, (title, reqs) in titles.items():
                r = self.new(test_id, title, reqs, phase)
                r.finish("this version has no drive health or durability (before M5.5)"
                         if health is None else
                         "simulated mode only: real drives carry no planted metadata, and "
                         "their bus and model aren't known to the oracle")
            return
        planted = {d["id"]: d for d in self.manifest["drives"] if d.get("health")}
        today = dt.date.fromisoformat(health["today"])
        standards = oracle.load_standards(self.observed["tool"]["source"])

        r = self.new("VV-HL-01", *titles["VV-HL-01"], phase)
        from vv.smart_mock import FAILING
        for drive_id, drive in sorted(planted.items()):
            got = health["drives"].get(drive_id)
            if got is None:
                continue
            r.checked += 1
            kind, row = drive["health"]["smart"], got["health"]
            if row is None:
                r.fail(f"{drive_id}: no health reading recorded")
            elif kind == "hidden":
                numbers = {k: v for k, v in row.items()
                           if k != "confidence" and v is not None}
                if row["confidence"] != "unknown" or numbers:
                    r.fail(f"{drive_id}: no SMART data, recorded as {row['confidence']} "
                           f"with {numbers}")
                if got["confidence"] == "measured":
                    r.fail(f"{drive_id}: failure rate labelled measured without SMART")
            else:
                want = FAILING if kind == "failing" else dict.fromkeys(FAILING, 0)
                want = {**want, "power_on_hours": drive["health"]["power_on_hours"]}
                wrong = {k: (row[k], v) for k, v in want.items() if row[k] != v}
                if row["confidence"] != "measured" or wrong:
                    r.fail(f"{drive_id}: {kind} SMART recorded as {row['confidence']}, "
                           f"(recorded, reported) differ for {wrong}")
        r.finish("no simulated drive with SMART planted (needs simulated mode)")

        exp = oracle.durability(self.manifest, self.exp["baseline"], standards, today)
        device, _unknown = oracle._drive_identity(self.manifest)
        observed_sets = {frozenset(device[n] for n in s["drives"]): s for s in health["sets"]}

        r = self.new("VV-HL-02", *titles["VV-HL-02"], phase)
        pairs = {}
        for scenario in self.manifest["scenarios"]:
            if scenario["kind"] in ("batch-copy", "cross-batch-copy"):
                pairs[scenario["kind"]] = frozenset(device[m[0]] for m in scenario["members"])
        for devs, want in sorted(exp["sets"].items(), key=lambda x: sorted(x[0])):
            if len(devs) < 2 or devs not in observed_sets:
                continue
            r.checked += 1
            got = observed_sets[devs]["correlation"]
            if abs(got - want["correlation"]) > 1e-9 * want["correlation"]:
                r.fail(f"drives {sorted(devs)}: correlation {got:g}, expected "
                       f"{want['correlation']:g}")
        batch = pairs.get("batch-copy")
        if batch is not None and batch in observed_sets:
            # The same two drives, as if bought from different batches: only
            # the batch differs, so only the batch can explain the difference.
            r.checked += 1
            heads = {}
            for d in self.manifest["drives"]:
                heads.setdefault(device[d["id"]], d)
            apart = [dict(heads[x]) for x in sorted(batch)]
            apart[1]["manufacturer"] = f"{apart[1]['manufacturer']} (another batch)"
            unbatched = oracle.set_loss(apart, standards, today)["p_loss"]
            got = observed_sets[batch]["p_loss"]
            rates = [oracle.drive_rate(d, standards, today)["afr"] for d in apart]
            capped = got >= min(rates) - 1e-15
            if not (got > unbatched or (capped and got >= unbatched)):
                r.fail(f"two copies on one purchase batch (drives {sorted(batch)}): P(loss) "
                       f"{got:.3e}, no worse than {unbatched:.3e} for the same drives "
                       "from different batches")
        r.finish("needs two identified simulated devices")

        r = self.new("VV-HL-03", *titles["VV-HL-03"], phase)
        for drive_id, got in sorted(health["drives"].items()):
            drive = next(d for d in self.manifest["drives"] if d["id"] == drive_id)
            want = oracle.drive_rate(drive, standards, today)
            r.checked += 1
            if abs(got["afr"] - want["afr"]) > 1e-9 or got["confidence"] != want["confidence"]:
                r.fail(f"{drive_id}: AFR {got['afr']:.6f} ({got['confidence']}), expected "
                       f"{want['afr']:.6f} ({want['confidence']})")
        for devs, want in sorted(exp["sets"].items(), key=lambda x: sorted(x[0])):
            r.checked += 1
            got = observed_sets.get(devs)
            if got is None:
                r.fail(f"drives {sorted(devs) or ['unverified']}: no P(loss) reported "
                       f"for {want['bytes']:,} bytes")
            elif abs(got["p_loss"] - want["p_loss"]) > 1e-12 or got["bytes"] != want["bytes"]:
                r.fail(f"drives {sorted(devs) or ['unverified']}: P(loss) {got['p_loss']:.3e} "
                       f"over {got['bytes']:,} bytes, expected {want['p_loss']:.3e} over "
                       f"{want['bytes']:,}")
        total = exp["expected_bytes_per_year"]
        r.checked += 1
        if abs(health["expected_bytes_per_year"] - total) > 1e-9 * max(1.0, total):
            r.fail(f"expected loss {health['expected_bytes_per_year']:,.3f} bytes a year, "
                   f"independently {total:,.3f}")
        r.finish()

        r = self.new("VV-HL-04", *titles["VV-HL-04"], phase)
        for argv in health["smart_argvs"]:
            r.checked += 1
            bad = [a for a in argv[1:] if a.split("=", 1)[0] in self.SMARTCTL_STATE_CHANGING]
            if bad:
                r.fail(f"smartctl would be run with {bad}: {' '.join(argv)}")
        r.finish("no SMART read was made (needs simulated drives with SMART planted)")

    # -- R-COL ---------------------------------------------------------------------

    def curation_tests(self, raw: dict) -> None:
        import datetime as dt

        phase = "baseline"
        cur = raw.get("curation")
        titles = {
            "VV-COL-01": ("Inference only proposes", ["R-COL-01"]),
            "VV-COL-02": ("Every proposal's evidence is true", ["R-COL-02"]),
            "VV-COL-03": ("Proposals never nest; no scan root is proposed", ["R-COL-03"]),
            "VV-COL-04": ("A planted project series is proposed folder by folder", ["R-COL-04"]),
        }
        if cur is None:
            for test_id, (title, reqs) in titles.items():
                self.new(test_id, title, reqs, phase).finish(
                    "this version has no collection inference (before M6.2)")
            return
        proposals = cur["proposals"]

        r = self.new("VV-COL-01", *titles["VV-COL-01"], phase)
        r.checked = 1
        if cur["collections"] or cur["rules"]:
            r.fail(f"after inference: {cur['collections']} collection(s) and {cur['rules']} "
                   "rule(s), none created by a person")
        r.finish()

        def month(ns: int) -> str:
            return dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).strftime("%Y-%m")

        r = self.new("VV-COL-02", *titles["VV-COL-02"], phase)
        sizes = self.manifest["contents"]
        for p in proposals:
            r.checked += 1
            prefix = f"{p['path']}/" if p["path"] else ""
            under = [f for f in self.manifest["files"]
                     if f["drive"] == p["drive"] and f["path"].startswith(prefix)]
            files = len(under)
            size = sum(sizes[f["content"]]["size"] for f in under)
            months = sorted(month(f["mtime_ns"]) for f in under if f.get("mtime_ns") is not None)
            span = [months[0], months[-1]] if months else None
            label = f"{p['drive']}:{p['path']}"
            if (p["files"], p["bytes"]) != (files, size):
                r.fail(f"{label}: evidence says {p['files']} file(s), {p['bytes']:,} bytes; "
                       f"truly {files}, {size:,}")
            if p["date_span"] != span:
                r.fail(f"{label}: evidence dates {p['date_span']}, truly {span}")
            if not p["has_evidence"]:
                r.fail(f"{label}: proposed without reasons or sample paths")
        r.finish("nothing was proposed")

        r = self.new("VV-COL-03", *titles["VV-COL-03"], phase)
        keys = {(p["drive"], p["path"]) for p in proposals}
        for drive, path in sorted(keys):
            r.checked += 1
            if path == "":
                r.fail(f"{drive}: the whole drive was proposed as a collection")
            parts = path.split("/")
            for i in range(1, len(parts)):
                if (drive, "/".join(parts[:i])) in keys:
                    r.fail(f"{drive}:{path} proposed inside {drive}:{'/'.join(parts[:i])}")
        r.finish("nothing was proposed")

        r = self.new("VV-COL-04", *titles["VV-COL-04"], phase)
        for scenario in self.manifest["scenarios"]:
            if scenario["kind"] != "project-series":
                continue
            for drive, folder in scenario["folders"]:
                r.checked += 1
                if (drive, folder) in keys:
                    continue
                nearby = sorted(p for d, p in keys if d == drive and
                                (folder.startswith(p + "/") or p.startswith(folder + "/")))
                r.fail(f"{drive}:{folder} not proposed"
                       + (f" (proposed instead: {nearby})" if nearby else ""))
        r.finish("no project series planted (dataset from before M6.2)")

    def membership_test(self, raw: dict, exp: dict) -> None:
        r = self.new("VV-COL-05", "A confirmed collection's members follow its folders",
                     ["R-COL-05"], "rescan")
        found = raw.get("collections")
        if found is None:
            r.finish("this version has no collection membership (before M6.3)")
            return
        for coll in found:
            prefixes = [(d, f"{p}/" if p else "") for d, p in coll["folders"]]
            under = [k for k in exp["files"]
                     if any(k[0] == d and k[1].startswith(p) for d, p in prefixes)]
            files, size = len(under), sum(exp["files"][k]["size"] for k in under)
            r.checked += 1
            if (coll["files"], coll["bytes"]) != (files, size):
                r.fail(f"{coll['title']}: {coll['files']} member file(s), {coll['bytes']:,} "
                       f"bytes; truly {files}, {size:,} under {coll['folders']}")
        r.finish("no collection was confirmed (nothing was proposed)")

        r = self.new("VV-COL-06", "Coverage counts every distinct byte once, filed or not",
                     ["R-COL-06"], "rescan")
        cov = raw.get("coverage")
        if cov is None:
            r.finish("this version reports no coverage (before M6.4)")
            return
        prefixes = [(d, f"{p}/" if p else "") for c in found for d, p in c["folders"]]

        def filed(key) -> bool:
            return any(key[0] == d and key[1].startswith(p) for d, p in prefixes)

        want = sum(g["size"] for g in exp["groups"].values()
                   if any(filed(k) for k in g["paths"]))
        want += sum(size for k, size in exp["singles"].items() if filed(k))
        total = exp["summary"]["unique_bytes"]
        r.checked = 3
        if cov["total_bytes"] != total:
            r.fail(f"coverage is over {cov['total_bytes']:,} bytes; all distinct bytes are "
                   f"{total:,}")
        if cov["filed_bytes"] != want:
            r.fail(f"{cov['filed_bytes']:,} bytes reported in collections; {want:,} truly have "
                   "a copy in one")
        if cov["filed_bytes"] + cov["unfiled_bytes"] != cov["total_bytes"]:
            r.fail("filed plus unfiled is not the whole")
        r.finish()

    def version_tests(self, raw: dict) -> None:
        import re

        r = self.new("VV-VER-01", "A version chain is one set, its latest preferred",
                     ["R-VER-01"], "baseline")
        sets = raw.get("versions")
        if sets is None:
            r.finish("this version has no version sets (before M6.5)")
            return
        by_path = {}
        for found in sets:
            for m in found["members"]:
                by_path[tuple(m["path"])] = found
        for scenario in self.manifest["scenarios"]:
            if scenario["kind"] != "version-chain":
                continue
            r.checked += 1
            chain = {tuple(m) for m in scenario["members"]}
            holders = {id(by_path[k]) for k in chain if k in by_path}
            missing = sorted(k for k in chain if k not in by_path)
            if missing:
                r.fail(f"not in any version set: {[_fmt(k) for k in missing]}")
                continue
            if len(holders) > 1:
                r.fail(f"one chain split over {len(holders)} sets: {sorted(map(_fmt, chain))}")
                continue
            found = by_path[next(iter(chain))]
            paths = {tuple(m["path"]) for m in found["members"]}
            if paths != chain:
                r.fail(f"{found['label']}: set holds {sorted(map(_fmt, paths))}, the chain is "
                       f"{sorted(map(_fmt, chain))}")
            newest = max(chain, key=lambda k: int(re.findall(r"_v(\d+)", k[1])[-1]))
            preferred = [tuple(m["path"]) for m in found["members"] if m["preferred"]]
            if preferred != [newest]:
                r.fail(f"{found['label']}: preferred {[_fmt(k) for k in preferred]}, the "
                       f"latest version is {_fmt(newest)}")
        r.finish("no version chain planted")

    def scorecard_tests(self, raw: dict, exp: dict) -> None:
        from vv.oracle import _drive_identity

        phase = "rescan"
        titles = {
            "VV-FAIR-01": ("Every criterion gets one result; every gap a remedy", ["R-FAIR-01"]),
            "VV-FAIR-02": ("FAIR results follow what is recorded", ["R-FAIR-02"]),
            "VV-FAIR-03": ("NDSA storage results follow copies and zones", ["R-FAIR-03"]),
        }
        cards = raw.get("scorecards")
        if cards is None:
            for test_id, (title, reqs) in titles.items():
                self.new(test_id, title, reqs, phase).finish(
                    "this version has no scorecards (before M6.7)")
            return

        r = self.new("VV-FAIR-01", *titles["VV-FAIR-01"], phase)
        for card in cards:
            for rubric_id in ("fair", "ndsa"):
                got = card[rubric_id]
                r.checked += 1
                if len(got["order"]) != len(set(got["order"])):
                    r.fail(f"{card['title']} {rubric_id}: a criterion scored twice")
                for criterion, (status, has_remedy) in got["results"].items():
                    if status not in ("pass", "judgement") and not has_remedy:
                        r.fail(f"{card['title']} {got['rubric']} {criterion}: {status} "
                               "without a remedy")
                if not got["rubric"].startswith(rubric_id + "-"):
                    r.fail(f"{card['title']}: scorecard names rubric {got['rubric']!r}")
        r.finish("no collection was confirmed")

        device, unknown = _drive_identity(self.manifest)
        zone = {}
        for d in self.manifest["drives"]:
            if d["id"] not in unknown:
                zone.setdefault(device[d["id"]], (d.get("health") or {}).get("disaster_zone"))
        plans = {(d["drive"], d["folder"]): d["fields"]
                 for sc in self.manifest["scenarios"] for d in sc.get("describe", [])}

        def items_under(folders):
            prefixes = [(dr, f"{p}/" if p else "") for dr, p in folders]
            inside = lambda k: any(k[0] == dr and k[1].startswith(p) for dr, p in prefixes)  # noqa: E731
            out = []
            for g in exp["groups"].values():
                if any(inside(k) for k in g["paths"]):
                    out.append({device[k[0]] for k in g["paths"] if k[0] not in unknown})
            for k in exp["singles"]:
                if inside(k):
                    out.append({device[k[0]]} if k[0] not in unknown else set())
            return out

        def expect(test, label):
            return "pass" if test else label

        r2 = self.new("VV-FAIR-02", *titles["VV-FAIR-02"], phase)
        r3 = self.new("VV-FAIR-03", *titles["VV-FAIR-03"], phase)
        for card in cards:
            fields = {}
            for folder in card["folders"]:
                fields.update(plans.get(tuple(folder), {}))
            filled = 1 + sum(1 for k in ("description", "creators_json", "contact", "license",
                                         "data_type", "project") if fields.get(k))
            items = items_under(card["folders"])
            fair = {c: v[0] for c, v in card["fair"]["results"].items()}
            ndsa = {c: v[0] for c, v in card["ndsa"]["results"].items()}
            completeness = "pass" if filled == 7 else "partial" if filled >= 4 else "fail"
            located = sum(1 for holders in items if holders)
            want2 = {
                "F2": completeness, "R1": completeness,
                "R1.1": expect(fields.get("license"), "fail"),
                "I3": "fail",
                "A1": "pass" if located == len(items) else "partial" if located else "fail",
            }
            for criterion, want in want2.items():
                r2.checked += 1
                if fair.get(criterion) != want:
                    r2.fail(f"{card['title']} {criterion}: {fair.get(criterion)}, expected "
                            f"{want} ({filled} of 7 fields, {located} of {len(items)} located)")

            def share(test):
                n = sum(1 for holders in items if test(holders))
                return "pass" if items and n == len(items) else "partial" if n else "fail"

            zones = lambda h: len({zone[x] for x in h if zone.get(x)})  # noqa: E731
            want3 = {
                "storage-1a": share(lambda h: len(h) >= 2),
                "storage-3a": share(lambda h: zones(h) >= 2),
                "storage-2a": share(lambda h: len(h) >= 3 and zones(h) >= 2),
            }
            for criterion, want in want3.items():
                r3.checked += 1
                if ndsa.get(criterion) != want:
                    r3.fail(f"{card['title']} {criterion}: {ndsa.get(criterion)}, expected "
                            f"{want} over {len(items)} item(s)")
        r2.finish("no collection was confirmed")
        r3.finish("no collection was confirmed")

    # -- R-PLAN ----------------------------------------------------------------------

    PLAN_KINDS = {"copy", "organize", "describe", "verify", "review-candidate", "acquire"}
    RESERVE_SHARE, RESERVE_FLOOR, DEFAULT_CLUSTER = 0.10, 1 << 30, 4096

    def plan_tests(self, raw: dict, exp: dict) -> None:
        from vv.oracle import _drive_identity

        phase = "rescan"
        titles = {
            "VV-PLAN-01": ("A plan only adds copies", ["R-PLAN-01"]),
            "VV-PLAN-02": ("No destination is planned over its room", ["R-PLAN-02"]),
            "VV-PLAN-03": ("Every unit meets its target or an acquire covers it", ["R-PLAN-03"]),
            "VV-PLAN-04": ("A copy sharing a batch or zone says so", ["R-PLAN-04"]),
        }
        plan = raw.get("plan")
        if plan is None:
            for test_id, (title, reqs) in titles.items():
                self.new(test_id, title, reqs, phase).finish("this version has no planner "
                                                             "(before M7)")
            return
        device, unknown = _drive_identity(self.manifest)
        drives = {d["id"]: d for d in self.manifest["drives"]}
        units = {u["key"]: u for u in plan["units"]}
        copies = [st for st in plan["steps"] if st["kind"] == "copy"]

        # Each live file belongs to the unit with the deepest folder over it
        # (a "loose" unit only holds the files directly in its folder).
        def owner(key):
            drive, path = key
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            best = None
            for u in plan["units"]:
                d, p = u["folder"]
                if d != drive:
                    continue
                inside = folder == p if u["kind"] == "loose" else (
                    p == "" or folder == p or folder.startswith(p + "/"))
                if inside and (best is None or len(p) > len(best["folder"][1])
                               or (len(p) == len(best["folder"][1]) and u["kind"] == "loose")):
                    best = u
            return best

        members: dict[str, list] = defaultdict(list)
        for key, entry in exp["files"].items():
            u = owner(key)
            if u is not None:
                members[u["key"]].append(key)
        content_devices = {c: {device[k[0]] for k in g["paths"] if k[0] not in unknown}
                           for c, g in exp["groups"].items()}

        def item_devices(key):
            content = exp["files"][key]["content"]
            if key in exp["identified"] and content in content_devices:
                return content_devices[content]
            return {device[key[0]]} if key[0] not in unknown else set()

        added: dict[str, set] = defaultdict(set)
        for st in copies:
            if st["dst"]:
                added[st["unit"]].add(device[st["dst"]])

        r = self.new("VV-PLAN-01", *titles["VV-PLAN-01"], phase)
        r.checked = 1 + len(plan["steps"])
        if not plan["removal_refused"]:
            r.fail("the plan store accepted a step that removes a copy")
        for st in plan["steps"]:
            if st["kind"] not in self.PLAN_KINDS:
                r.fail(f"step {st['seq']} is a {st['kind']!r}")
        for st in copies:
            unit = units.get(st["unit"])
            if not st["dst"] or unit is None:
                r.fail(f"copy step {st['seq']} has no destination or unit")
                continue
            if device[st["dst"]] == device[unit["folder"][0]]:
                r.fail(f"copy step {st['seq']} copies {_fmt(tuple(unit['folder']))} onto its "
                       "own drive")
        r.finish()

        r = self.new("VV-PLAN-02", *titles["VV-PLAN-02"], phase)
        used: dict[str, int] = defaultdict(int)
        for st in copies:
            dst = drives.get(st["dst"])
            if dst is None or st["unit"] not in units:
                continue
            cluster = dst.get("cluster_bytes") or self.DEFAULT_CLUSTER
            fp = sum(-(-exp["files"][k]["size"] // cluster) * cluster
                     for k in members[st["unit"]] if exp["files"][k]["size"])
            used[st["dst"]] += fp
            r.checked += 1
            if (st["footprint"] or 0) < fp:
                r.fail(f"copy step {st['seq']}: footprint {st['footprint']:,} bytes on "
                       f"{st['dst']}, independently {fp:,} at {cluster:,}-byte clusters")
        for drive_id, fp in sorted(used.items()):
            d = drives[drive_id]
            if not d.get("capacity_bytes"):
                r.fail(f"{drive_id}: planned as a destination with no known size")
                continue
            free = d["capacity_bytes"] - d.get("baseline_bytes", 0)
            room = free - max(int(d["capacity_bytes"] * self.RESERVE_SHARE), self.RESERVE_FLOOR)
            if fp > room:
                r.fail(f"{drive_id}: {fp:,} bytes planned onto {room:,} of room "
                       f"({free:,} free less the reserve)")
        r.finish("no copy step planned (drives of unknown size take nothing)")

        r = self.new("VV-PLAN-03", *titles["VV-PLAN-03"], phase)
        owed = 0
        for key, unit in sorted(units.items()):
            items = members.get(key, [])
            if not items:
                continue
            r.checked += 1
            after = []
            for k in items:
                held = item_devices(k) | added[key]
                after.append(len(held) + (0 if item_devices(k) else 1))
            deficit = max(0, unit["target"] - min(after))
            if deficit > unit["short"]:
                r.fail(f"{_fmt(tuple(unit['folder']))}: {min(after)} of {unit['target']} copies "
                       f"after the plan, but the plan says {unit['short']} short")
            owed += deficit * sum(exp["files"][k]["size"] for k in items)
        acquire = sum(st["bytes"] or 0 for st in plan["steps"] if st["kind"] == "acquire")
        if owed and acquire < owed:
            r.fail(f"{owed:,} bytes of copies are short of target; the acquire step covers "
                   f"{acquire:,}")
        r.finish("no unit planned")

        r = self.new("VV-PLAN-04", *titles["VV-PLAN-04"], phase)
        traits: dict[str, tuple] = {}
        for d in self.manifest["drives"]:
            if d["id"] in unknown:
                continue
            health = d.get("health") or {}
            date = health.get("purchase_date") or ""
            batch = ((d.get("manufacturer") or "").strip().lower(),
                     (d.get("model") or "").strip().lower(), date[:7]) if len(date) >= 7 else None
            if device[d["id"]] not in traits or (batch and not traits[device[d["id"]]][0]):
                traits[device[d["id"]]] = (batch, health.get("disaster_zone"))
        for key, new in sorted(added.items()):
            unit = units[key]
            items = members.get(key, [])
            before = set.intersection(*(item_devices(k) for k in items)) if items else set()
            after = before | new
            limit = max(1, unit["target"] - 1)
            for st in (st for st in copies if st["unit"] == key and st["dst"]):
                dev = device[st["dst"]]
                r.checked += 1
                batch, zone = traits.get(dev, (None, None))
                crowded = []
                for label, value, index in (("batch", batch, 0), ("zone", zone, 1)):
                    if value and sum(1 for x in after
                                     if traits.get(x, (None, None))[index] == value) > limit:
                        crowded.append(label)
                said = st.get("shares", [])
                where = _fmt(tuple(unit["folder"]))
                for label in crowded:
                    if label not in said:
                        r.fail(f"{where}: the copy on {st['dst']} puts more than {limit} of its "
                               f"copies in one {label} and doesn't say so")
                for label in said:
                    if label not in crowded:
                        r.fail(f"{where}: the copy on {st['dst']} says it shares a {label}, but "
                               "it keeps the copies apart")
        r.finish("no copy step planned")

    # -- R-EXP ------------------------------------------------------------------------

    def export_tests(self, bundles: list | None, tree: dict) -> None:
        r = self.new("VV-EXP-01", "Exports stay in their bundles, every file listed",
                     ["R-EXP-01"], "export")
        if bundles is None:
            r.finish("this version writes no exports (before M8)")
            return
        names = {b["path"] for b in bundles}
        for rel, entry in sorted(tree.items()):
            r.checked += 1
            top = rel.split("/", 1)[0]
            if top not in names or "/" not in rel:
                r.fail(f"{rel}: written outside any bundle the tool reported")
            if entry.get("link"):
                r.fail(f"{rel}: a link in the export root")
        for name in sorted(names):
            r.checked += 1
            record = tree.get(f"{name}/_EXPORT.json")
            if record is None:
                r.fail(f"{name}: closed without an _EXPORT.json")
                continue
            listing = record.get("listing")
            if listing is None:
                r.fail(f"{name}/_EXPORT.json: not a readable record")
                continue
            listed = {f["path"]: f for f in listing}
            present = {rel.split("/", 1)[1]: e for rel, e in tree.items()
                       if rel.startswith(name + "/") and not e.get("link")
                       and rel != f"{name}/_EXPORT.json"}
            for path in sorted(set(present) - set(listed)):
                r.fail(f"{name}/{path}: written but not listed in _EXPORT.json")
            for path in sorted(set(listed) - set(present)):
                r.fail(f"{name}/{path}: listed in _EXPORT.json but not there")
            for path in sorted(set(listed) & set(present)):
                if (listed[path]["sha256"], listed[path]["bytes"]) != \
                        (present[path]["sha256"], present[path]["bytes"]):
                    r.fail(f"{name}/{path}: _EXPORT.json says {listed[path]['bytes']:,} bytes, "
                           f"{listed[path]['sha256'][:12]}…; the file is "
                           f"{present[path]['bytes']:,}, {present[path]['sha256'][:12]}…")
        r.finish("no export was made")

    def manifest_tests(self, bundles: list | None, tree: dict, rescan: dict) -> None:
        from vv.oracle import _drive_identity

        r = self.new("VV-EXP-02", "Each drive's manifest lists exactly its files and copies",
                     ["R-EXP-02"], "export")
        found = [e["json"] for rel, e in sorted(tree.items())
                 if rel.endswith("/_DRIVE-FUSION.json")]
        if bundles is None or not any(b["kind"] == "manifests" for b in bundles):
            r.finish("this version writes no per-drive manifests (before M8.2)")
            return
        exp = self.exp["rescan"]
        device, unknown = _drive_identity(self.manifest)
        roots = {d["id"]: d["root"].replace("\\", "/").rstrip("/") for d in self.manifest["drives"]}
        by_volume = {info["volume_id"]: d for d, info in rescan.get("volumes", {}).items()}

        def key_of(path: str):
            path = path.replace("\\", "/")
            for d, root in roots.items():
                if path.startswith(root + "/"):
                    return (d, path[len(root) + 1:])
            return None

        # The oracle's grouping: one manifest per identified device, one per
        # unidentified drive (its volume has no drive).
        def group_of(drive_id: str):
            return ("device", device[drive_id]) if drive_id not in unknown else ("volume", drive_id)

        want: dict = defaultdict(set)
        for k in exp["files"]:
            want[group_of(k[0])].add(k)
        members = {k: g["paths"] for g in exp["groups"].values() for k in g["paths"]}
        seen_groups = set()
        for m in found:
            r.checked += 1
            if m is None:
                r.fail("a _DRIVE-FUSION.json that is not JSON")
                continue
            drives_in = {by_volume.get(v["id"]) for v in m["volumes"]} - {None}
            if not drives_in:
                r.fail(f"{m['label']}: no volume the bench registered")
                continue
            group = group_of(sorted(drives_in)[0])
            seen_groups.add(group)
            listed = {}
            for f in m["files"]:
                k = key_of(f["path"])
                if k is None:
                    r.fail(f"{m['label']}: {f['path']} is under no drive root")
                    continue
                listed[k] = f
            for k in sorted(set(listed) - want[group]):
                r.fail(f"{m['label']}: lists {_fmt(k)}, which is not a live file there")
            for k in sorted(want[group] - set(listed)):
                r.fail(f"{m['label']}: leaves out {_fmt(k)}")
            for k in sorted(set(listed) & want[group]):
                f = listed[k]
                if f["bytes"] != exp["files"][k]["size"]:
                    r.fail(f"{m['label']}: {_fmt(k)} is {f['bytes']:,} bytes in the manifest, "
                           f"{exp['files'][k]['size']:,} truly")
                if k not in exp["identified"]:
                    continue
                others = {p for p in members.get(k, ()) if group_of(p[0]) != group}
                said = {key_of(s["path"]) for s in f["copies_elsewhere"]}
                if said != others:
                    r.fail(f"{m['label']}: {_fmt(k)} names copies at "
                           f"{sorted(map(_fmt, said - {None}))}; truly "
                           f"{sorted(map(_fmt, others))}")
        for group in sorted(set(want) - seen_groups, key=str):
            r.fail(f"no manifest for {group[0]} {group[1]}, which holds "
                   f"{len(want[group])} live file(s)")
        r.finish("no manifest was written")

    def package_tests(self, bundles: list | None, tree: dict, rescan: dict) -> None:
        r = self.new("VV-EXP-03", "Each collection is a valid bag whose crate lists its members",
                     ["R-EXP-03"], "export")
        if bundles is None or not any(b["kind"] == "collections" for b in bundles):
            r.finish("this version writes no collection packages (before M8.3)")
            return
        exp = self.exp["rescan"]
        roots = {d["id"]: d["root"].replace("\\", "/").rstrip("/") for d in self.manifest["drives"]}

        def key_of(uri: str):
            from urllib.parse import unquote, urlparse

            path = unquote(urlparse(uri).path)
            if len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            for d, root in roots.items():
                if path.startswith(root + "/"):
                    return (d, path[len(root) + 1:])
            return None

        bags = sorted(rel[:-len("/bagit.txt")] for rel in tree if rel.endswith("/bagit.txt"))
        wanted = {c["title"]: c for c in rescan.get("collections") or []}
        seen = set()
        for bag in bags:
            r.checked += 1
            if not tree[f"{bag}/bagit.txt"].get("text", "").startswith("BagIt-Version: 1.0"):
                r.fail(f"{bag}: bagit.txt does not declare BagIt 1.0")
            payload = {rel[len(bag) + 1:]: e for rel, e in tree.items()
                       if rel.startswith(f"{bag}/data/")}
            lines = [line.split("  ", 1) for line in
                     tree.get(f"{bag}/manifest-sha256.txt", {}).get("text", "").splitlines()]
            listed = {p: h for h, p in lines}
            if set(listed) != set(payload):
                r.fail(f"{bag}: manifest lists {sorted(listed)}, the payload is {sorted(payload)}")
            for p in sorted(set(listed) & set(payload)):
                if listed[p] != payload[p]["sha256"]:
                    r.fail(f"{bag}/{p}: manifest checksum does not match the file")
            info = dict(line.split(": ", 1) for line in
                        tree.get(f"{bag}/bag-info.txt", {}).get("text", "").splitlines()
                        if ": " in line)
            oxum = f"{sum(e['bytes'] for e in payload.values())}.{len(payload)}"
            if info.get("Payload-Oxum") != oxum:
                r.fail(f"{bag}: Payload-Oxum {info.get('Payload-Oxum')}, truly {oxum}")
            tags = {p: h for h, p in (line.split("  ", 1) for line in tree.get(
                f"{bag}/tagmanifest-sha256.txt", {}).get("text", "").splitlines())}
            for name in ("bagit.txt", "bag-info.txt", "manifest-sha256.txt"):
                entry = tree.get(f"{bag}/{name}")
                if name not in tags or entry is None or tags[name] != entry["sha256"]:
                    r.fail(f"{bag}: the tag manifest does not cover {name} correctly")
            crate = tree.get(f"{bag}/data/ro-crate-metadata.json", {}).get("json")
            if not crate:
                r.fail(f"{bag}: no readable RO-Crate")
                continue
            graph = {e["@id"]: e for e in crate.get("@graph", [])}
            root = graph.get("./", {})
            title = root.get("name")
            coll = wanted.get(title)
            if coll is None:
                r.fail(f"{bag}: a crate for {title!r}, which is not a confirmed collection")
                continue
            seen.add(title)
            prefixes = [(d, f"{p}/" if p else "") for d, p in coll["folders"]]
            truth = {k for k in exp["files"]
                     if any(k[0] == d and k[1].startswith(p) for d, p in prefixes)}
            parts = {}
            for ref in root.get("hasPart", []):
                entity = graph.get(ref["@id"], {})
                parts[key_of(ref["@id"])] = int(entity.get("contentSize", -1))
            if set(parts) != truth:
                extra, missing = set(parts) - truth, truth - set(parts)
                r.fail(f"{title}: crate lists {len(parts)} file(s), the collection holds "
                       f"{len(truth)}" + (f"; extra {sorted(map(_fmt, extra - {None}))[:3]}"
                                          if extra else "")
                       + (f"; missing {sorted(map(_fmt, missing))[:3]}" if missing else ""))
            for k in sorted(set(parts) & truth):
                if parts[k] != exp["files"][k]["size"]:
                    r.fail(f"{title}: {_fmt(k)} is {parts[k]:,} bytes in the crate, "
                           f"{exp['files'][k]['size']:,} truly")
        for title in sorted(set(wanted) - seen):
            r.fail(f"collection {title!r} has no package")
        r.finish("no collection was confirmed")

    def deferred(self) -> None:
        for req in BY_ID.values():
            if req.status != "planned":
                continue
            r = self.new(f"VV-{req.id[2:]}", req.text.split(",")[0], [req.id], "—")
            r.verdict = DEFERRED
            planted = [s for s in self.manifest["scenarios"] if req.id in s["requirements"]]
            r.checked = len(planted)
            r.note = (f"planned for {req.milestone}; {len(planted)} scenario(s) are in the "
                      "dataset so a later version can be measured against them")


def evaluate(manifest: dict, observed: dict) -> list[dict]:
    return [asdict(r) for r in Evaluator(manifest, observed).run()]
