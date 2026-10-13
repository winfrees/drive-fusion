# Release notes: signing, antivirus, and what to tell a user

Drive Fusion opens raw volume handles and reads every file on a drive. That is
also, almost exactly, the behaviour signature of ransomware reconnaissance. The
difference is visible in the code and invisible to a heuristic, so the release
has to carry the evidence instead.

This file is the checklist for making a build that people can actually run. It
is not implemented by code — there is no release automation here yet — so it is
written down rather than assumed.

## Authenticode signing

Sign both executables and the helper:

- `DriveFusion.exe` — the windowed front end
- `drivefusion.exe` — the console front end
- `dfscan-helper.exe` — the elevated enumeration helper

The helper matters most. It requests elevation, and Windows shows the publisher
name in the UAC prompt: a signed binary shows the organisation, an unsigned one
shows "Unknown publisher" in a dialog that is asking for administrator rights.
Asking a user to grant admin to an unknown publisher is asking them to do the
exact thing security training tells them not to, and they are right to refuse.

An EV certificate additionally seeds SmartScreen reputation immediately rather
than after some number of installs. For a tool distributed to a handful of
people, a standard certificate means early users see a SmartScreen warning;
plan for that or budget for EV.

Sign after PyInstaller, not before: the build rewrites the executable headers.

## Why antivirus flags this, and what to publish

The behaviour that trips heuristics:

- `CreateFileW` on `\\.\PhysicalDrive*` and `\\?\Volume{...}` — raw volume
  handles, used to read the USN journal and directory records
- reading every file on a volume in quick succession, which is what the hashing
  pass does
- a child process requesting elevation

All three are necessary and all three are read-only. What to publish alongside
a release so an administrator can verify that rather than take it on trust:

1. **SHA-256 of every binary**, in a signed release note.
2. **The read-only argument**, stated concretely rather than as a claim: four
   independent mechanisms (`docs/PLAN.md` §3.2) — a file gateway with no writer,
   Win32 handles that request read access only, a CI lint that fails the build
   on any mutating call, and a regression test that hashes a fixture tree before
   and after a full scan→analysis→plan→export cycle and asserts byte and
   metadata identity, access times included.
3. **The lint rules by number**, because they are checkable: DF001–DF010, with
   DF008 covering process creation and DF009/DF010 confining the one package
   allowed to write files.
4. **A VirusTotal link** for each release artifact. Expect a few engines to flag
   it; say so in advance rather than letting a user discover it.

## Exclusions, and how to ask for them

Do not tell users to disable their antivirus. Ask for the narrowest exclusion
that works, and explain the cost:

- The **install folder**, so the signed binaries are not rescanned on every
  launch.
- The **catalog folder** (`%LOCALAPPDATA%\DriveFusion`), because real-time
  scanning of a SQLite file being written in WAL mode is a large and pointless
  slowdown.

Do not ask for an exclusion on the user's media. Those are the files the tool
reads; an exclusion there would weaken scanning of the data the user cares
most about, to speed up a tool that is already read-only.

## Bundling smartctl

`smartmontools` ships as a separate executable. Bundle it rather than requiring
a separate install: on Windows it is far more reliable than WMI, particularly
over USB bridges, and a drive whose health cannot be read is scored *worse*
than one verified clean, so a missing `smartctl` quietly degrades every risk
figure in the product.

Two things the installer must get right:

- **Ship the licence.** smartmontools is GPL-2.0-or-later. Bundling it means
  shipping its licence text and its source offer. A preservation tool that
  violates a licence is not in a position to score anyone's metadata.
- **Record the version** in the catalog alongside each reading, so a health
  figure can be traced to the binary that produced it.

Only read-only arguments are ever passed, enforced by the allowlist in
`drivefusion/core/health/smart.py` and by lint rule DF008. `smartctl -t long`
starts a multi-hour self-test on a user's drive; that is not a filesystem write,
so none of the other read-only mechanisms would catch it.

## What a release note should say about exports

An export bundle is the artifact most likely to outlive this tool, and the one
most likely to be mistaken for something it is not. Two sentences worth
repeating wherever it is described:

- The bundle is a **description**, not a copy. The files it describes stay on
  the user's drives.
- It is **not a compliance artifact** and not a Data Management and Sharing
  Plan. It records what was found and what the tool's own versioned rubrics
  make of it.

Both are already written into `README.txt` and `bag-info.txt` inside every
bundle (`drivefusion/core/export/writer.py`), so a release note only has to
agree with them.

## Checklist

- [ ] Version bumped in `drivefusion/__init__.py`
- [ ] `python -m pytest` green, with and without the optional `ortools`
- [ ] `python tools/ro_lint.py` clean
- [ ] Built on `windows-latest`, one-folder, 64-bit
- [ ] All three executables signed, signatures verified on a clean machine
- [ ] UAC prompt checked: publisher name shows, not "Unknown publisher"
- [ ] SHA-256 published for every artifact
- [ ] VirusTotal scanned, results linked, expected detections noted
- [ ] `smartctl` bundled with its licence and source offer
- [ ] Exclusion guidance limited to the install and catalog folders
- [ ] A bundle produced by the release build validates with
      `drivefusion export verify`
