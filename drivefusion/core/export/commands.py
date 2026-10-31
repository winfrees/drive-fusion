"""Copy-only command previews and the generated-text denylist (PLAN §3.2, ROADMAP M8.4).

A plan's copy steps can be exported as command previews for a person to
review and run: ``robocopy`` for Windows, ``rsync`` elsewhere. They only copy:
no ``/MIR``, ``/PURGE``, ``/MOVE``, ``--delete`` or ``--remove-source-files``,
and Drive Fusion never runs them.

Every preview passes :func:`check` before it is written, an independent
gate on the text itself rather than on how it was built:

* a line is a comment (``REM`` or ``#``), blank, or a command whose program is
  ``robocopy`` or ``rsync``;
* every path is quoted, and a path containing a quote is refused, so no path
  can end its quoting and start a command of its own;
* every unquoted token must be an allowed option, and none may be on the
  denylist (``del``, ``rm``, ``rmdir``, ``erase``, ``format``, ``diskpart``,
  ``unlink``, ``/MIR``, ``/PURGE``, ``--delete``…), matched whole and without
  regard to case.

A destination's drive letter or mount point isn't known to the catalog, so
each destination starts with a placeholder naming the drive, ``<ARCHIVE-2>``,
for the person to replace.
"""

from __future__ import annotations

import re

from drivefusion.core.export.writer import ExportError

DENYLIST = ("del", "rm", "rmdir", "rd", "erase", "format", "diskpart", "unlink", "mv",
            "move", "/mir", "/purge", "/move", "/mov", "--delete", "--remove-source-files",
            "shred", "mkfs", "dd")
ROBOCOPY_OPTIONS = {"/e", "/copy:dat", "/dcopy:dat", "/r:1", "/w:1", "/xj", "/np", "/lev:1"}
RSYNC_OPTIONS = {"-a", "--itemize-changes", "--exclude=*/", "--protect-args"}
PROGRAMS = {"robocopy": ROBOCOPY_OPTIONS, "rsync": RSYNC_OPTIONS}
_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _quote(path: str) -> str:
    if '"' in path or "'" in path or "\n" in path:
        raise ExportError(f"{path!r}: a path with a quote or a line break can't be put in a "
                          "command preview safely")
    return f'"{path}"'


def check(relative: str, data: bytes) -> None:
    """Refuse a preview that holds anything but copy-only commands."""
    text = data.decode("utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.upper().startswith("REM "):
            continue
        tokens = _TOKEN.findall(stripped)
        program = tokens[0].lower()
        where = f"{relative}, line {number}"
        if program not in PROGRAMS:
            raise ExportError(f"{where}: {tokens[0]!r} is not a copy-only program")
        for token in tokens[1:]:
            if token[0] in "\"'":
                if token[-1] != token[0] or len(token) < 2:
                    raise ExportError(f"{where}: unbalanced quoting")
                continue
            low = token.lower()
            if low in DENYLIST or any(low.startswith(bad) for bad in DENYLIST if
                                      bad.startswith(("-", "/"))):
                raise ExportError(f"{where}: {token!r} is on the denylist; Drive Fusion "
                                  "never writes a destructive command")
            if low not in PROGRAMS[program]:
                raise ExportError(f"{where}: {token!r} is not an allowed {program} option")


def _norm(path: str) -> str:
    return re.sub(r"[\\/]+", "/", path)


def robocopy(src: str, dst: str, *, recursive: bool) -> str:
    flags = ["/E"] if recursive else ["/LEV:1"]
    win = [_quote(_norm(p).replace("/", "\\")) for p in (src, dst)]
    return " ".join(["robocopy", *win,
                     *flags, "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1", "/XJ"])


def rsync(src: str, dst: str, *, recursive: bool) -> str:
    flags = ["-a", "--protect-args"] + ([] if recursive else ["--exclude=*/"])
    return " ".join(["rsync", *flags, _quote(_norm(src).rstrip("/") + "/"),
                     _quote(_norm(dst).rstrip("/") + "/")])


def previews(catalog, plan_id: int) -> dict[str, str]:
    """``copy-commands.cmd`` and ``copy-commands.sh`` for a stored plan."""
    from drivefusion.core.planner import document

    doc = document.to_json(catalog, plan_id)
    head = (f"Copy-only commands for plan {doc['id']} ({doc['name']}), written by Drive Fusion "
            "for you to review and run yourself. Nothing here deletes, moves or mirrors. "
            "Replace each <DRIVE> placeholder with that drive's letter or mount point first.")
    cmd = [f"REM {head}"]
    sh = [f"# {head}"]
    for st in doc["steps"]:
        if st["kind"] != "copy" or not st["src_path"]:
            continue
        loose = st["unit"].startswith("l")
        dst = f"<{st['dst_drive']}>/{st['dst_path']}"
        note = f"step {st['seq']}: {st['src_drive'] or 'unconfirmed drive'} -> {st['dst_drive']}"
        cmd += [f"REM {note}", robocopy(st["src_path"], dst, recursive=not loose)]
        sh += [f"# {note}", rsync(st["src_path"], dst, recursive=not loose)]
    return {"copy-commands.cmd": "\r\n".join(cmd) + "\r\n", "copy-commands.sh": "\n".join(sh) + "\n"}
