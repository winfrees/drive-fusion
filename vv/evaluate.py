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
            self.dirs_test(obs, exp, name)
            self.identity_tests(obs, exp, name)
            self.redundancy_tests(obs, exp, phases[name], name)
        if "verify" in errors or "verify" not in phases:
            self.phase_error("verify", errors.get("verify", "phase did not run"))
        else:
            self.fixity_tests(phases["verify"])
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

        r = self.new("VV-CAT-07", "Each drive is catalogued as a distinct device", ["R-CAT-07"], phase)
        volumes = raw.get("volumes", {})
        r.checked = len(self.manifest["drives"])
        devices = defaultdict(list)
        for drive in self.manifest["drives"]:
            v = volumes.get(drive["id"])
            if not v:
                r.fail(f"{drive['id']} was not registered as a volume")
                continue
            devices[v["device"]].append(drive["id"])
        for device, drives in devices.items():
            if len(drives) > 1:
                r.fail(f"drives {', '.join(drives)} resolved to one device {device}")
        r.finish()

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
            return {obs.path_set(g["content_id"]): g for g in groups}

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
        got = as_paths(raw["under_protected"])
        r.checked = len(exp["groups"])
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
            if key not in exp["corrupted"] and inc["result"] == "mismatch":
                r.fail(f"intact file reported as corrupted: {_fmt(key)}")
        if raw["checked"] < exp["min_checked"]:
            r.fail(f"checked {raw['checked']} file(s); at least {exp['min_checked']} are "
                   "verifiable (every identified file at or below the small-file threshold, "
                   "plus the corrupted large files)")
        r.finish()

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
