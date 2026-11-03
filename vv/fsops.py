"""Every write the test bench makes, plus the snapshot that proves the tool made none.

Kept in one module so the bench's own blast radius is reviewable: it writes only
beneath drive roots and the unscoped root that a manifest names, and it refuses
to start in a directory that already holds anything. Physical drives are real
devices with real data on them, so that refusal is the line between a test
dataset and someone's archive.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_module
from pathlib import Path

from vv import content as content_mod

WINDOWS = os.name == "nt"


class DatasetError(RuntimeError):
    """A dataset cannot be built, used, or restored as asked."""


def os_path(root: str, rel: str = "") -> str:
    """An absolute OS path for a manifest path, long-path safe on Windows.

    Manifests store paths with ``/`` relative to a drive root. Deep-path
    scenarios exceed 260 characters, which Windows only accepts with the
    ``\\\\?\\`` prefix.
    """
    full = os.path.abspath(os.path.join(root, *rel.split("/"))) if rel else os.path.abspath(root)
    if WINDOWS and not full.startswith("\\\\?\\"):
        full = "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full
    return full


def ensure_empty_target(path: str) -> None:
    """Refuse to generate into anything that already has entries in it."""
    p = Path(path)
    if p.exists():
        if not p.is_dir():
            raise DatasetError(f"{path} exists and is not a directory")
        if any(p.iterdir()):
            raise DatasetError(
                f"{path} is not empty. The generator only writes into new or empty "
                "folders, so it can never overwrite real data. Use a dedicated "
                "folder such as E:\\DriveFusionVV."
            )


def make_dirs_tracked(path: str, created: list[str]) -> None:
    """``makedirs``, recording each folder that did not exist before."""
    missing = []
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    os.makedirs(path, exist_ok=True)
    created.extend(reversed(missing))


def remove_created(created: list[str]) -> None:
    """Undo ``make_dirs_tracked``: only folders this run made, deepest first."""
    for path in reversed(created):
        shutil.rmtree(path, ignore_errors=True)


def _existing(path: str) -> str:
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    return probe


def check_free_space(needs: dict[str, int]) -> None:
    """Refuse a dataset that would crowd the disk it is written to.

    ``needs`` maps each location to the bytes it will consume. Locations on
    the same filesystem are summed — simulated drives all share the host disk —
    and each filesystem must keep a reserve free afterwards: a tenth of its
    size, capped at 1 GiB, so a small thumb drive is not held to a large disk's
    margin.
    """
    by_device: dict[int, list] = {}
    for path, n in needs.items():
        probe = _existing(path)
        entry = by_device.setdefault(os.stat(probe).st_dev, [probe, 0])
        entry[1] += n
    for probe, n in by_device.values():
        usage = shutil.disk_usage(probe)
        reserve = min(2**30, usage.total // 10)
        if n + reserve > usage.free:
            raise DatasetError(
                f"not enough space at {probe}: this dataset needs ~{n / 1e6:,.0f} MB and "
                f"{reserve / 1e6:,.0f} MB is kept free, but only {usage.free / 1e6:,.0f} MB "
                "is available. Use a smaller --drive-size, fewer --drives or "
                "--files-per-drive, or another disk.")


def volume_space(path: str) -> dict:
    """The size, free space and cluster size of the filesystem holding ``path``,
    as the operating system reports them: the bench's own measurement, never the
    tool's (R-PLAN-02 on physical drives). The cluster size is None where it
    can't be read."""
    probe = _existing(path)
    usage = shutil.disk_usage(probe)
    cluster = None
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(1024)
        kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel32.GetVolumePathNameW.restype = wintypes.BOOL
        kernel32.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(wintypes.DWORD)] * 4
        kernel32.GetDiskFreeSpaceW.restype = wintypes.BOOL
        if kernel32.GetVolumePathNameW(os.path.abspath(probe), buffer, len(buffer)):
            sectors, sector_bytes, free_clusters, clusters = (wintypes.DWORD() for _ in range(4))
            if kernel32.GetDiskFreeSpaceW(buffer.value, ctypes.byref(sectors), ctypes.byref(sector_bytes),
                                          ctypes.byref(free_clusters), ctypes.byref(clusters)):
                cluster = sectors.value * sector_bytes.value or None
    else:
        cluster = os.statvfs(probe).f_bsize or None
    return {"capacity_bytes": usage.total, "free_bytes": usage.free, "cluster_bytes": cluster}


# -- capability probing ---------------------------------------------------

def probe_capabilities(root: str) -> dict:
    """What this filesystem lets a dataset contain.

    FAT32 and exFAT have no hardlinks or symlinks, Windows symlinks need a
    privilege, and an administrator or root can read a file with no permission
    bits. Each scenario that depends on one of these is placed only where the
    probe succeeded, and reports N/A elsewhere rather than passing vacuously.
    """
    caps = {"hardlinks": False, "symlinks": False, "unreadable": False}
    probe = os.path.join(root, ".vv-probe")
    os.makedirs(probe, exist_ok=False)
    try:
        target = os.path.join(probe, "a")
        with open(target, "wb") as handle:
            handle.write(b"probe")
        try:
            os.link(target, os.path.join(probe, "b"))
            caps["hardlinks"] = os.stat(target).st_nlink == 2
        except (OSError, NotImplementedError, AttributeError):
            pass
        try:
            os.symlink(target, os.path.join(probe, "c"))
            caps["symlinks"] = True
        except (OSError, NotImplementedError, AttributeError):
            pass
        if not WINDOWS:
            try:
                os.chmod(target, 0)
                try:
                    with open(target, "rb"):
                        pass
                except PermissionError:
                    caps["unreadable"] = True
            finally:
                os.chmod(target, 0o644)
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return caps


# -- materializing a manifest ----------------------------------------------

def _write(path: str, data: bytes, mtime_ns: int | None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def _roots(manifest: dict) -> dict[str, str]:
    return {d["id"]: d["root"] for d in manifest["drives"]}


def materialize(manifest: dict, *, progress=None) -> None:
    """Write a manifest's baseline state to disk."""
    seed, contents = manifest["seed"], manifest["contents"]
    roots = _roots(manifest)

    def body(key: str) -> bytes:
        return content_mod.render(seed, key, contents)

    for d in manifest["dirs"]:
        os.makedirs(os.path.join(os_path(roots[d["drive"]], d["path"])), exist_ok=True)

    first_of_link_group: dict[str, str] = {}
    written = 0
    for f in manifest["files"]:
        target = os_path(roots[f["drive"]], f["path"])
        group = f.get("link_group")
        if group and group in first_of_link_group:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.link(first_of_link_group[group], target)
            continue
        _write(target, body(f["content"]), f["mtime_ns"])
        if group:
            first_of_link_group[group] = target
        written += 1
        if progress and written % 200 == 0:
            progress(written)

    for f in manifest["excluded"]["files"]:
        _write(os_path(roots[f["drive"]], f["path"]), body(f["content"]), None)
    for d in manifest["excluded"]["dirs"]:
        os.makedirs(os_path(roots[d["drive"]], d["path"]), exist_ok=True)

    for f in manifest["unscoped"]:
        _write(os_path(manifest["unscoped_root"], f["path"]), body(f["content"]), None)

    for link in manifest["links"]:
        root = roots[link["drive"]]
        os.symlink(os_path(root, link["target"]), os_path(root, link["path"]),
                   target_is_directory=link["kind"] == "dir")

    # Last, so the generator's own writes above were not refused.
    for f in manifest["files"]:
        if f.get("unreadable"):
            os.chmod(os_path(roots[f["drive"]], f["path"]), 0)


def wipe(manifest: dict) -> None:
    """Remove a dataset's drive roots and unscoped root, after checking them.

    Only paths the manifest could have put there may exist. Anything else
    means the folder holds something this bench did not create, and it is
    left alone with an explanation rather than deleted.
    """
    roots = _roots(manifest)
    known: dict[str, set[str]] = {d: set() for d in roots}
    for f in manifest["files"] + manifest["excluded"]["files"] + manifest["links"]:
        known[f["drive"]].add(f["path"])
    for d in manifest["dirs"] + manifest["excluded"]["dirs"] + manifest["excluded"]["roots"]:
        known[d["drive"]].add(d["path"])
    for op in manifest["mutations"]:
        known[op["drive"]].add(op["path"])
        if "to" in op:
            known[op["drive"]].add(op["to"])
    for paths in known.values():
        for path in list(paths):
            parts = path.split("/")
            paths.update("/".join(parts[:i]) for i in range(1, len(parts)))

    for drive, root in roots.items():
        if not os.path.isdir(root):
            continue
        top = os_path(root)

        def unlistable(exc: OSError) -> None:
            # An entry we cannot see is an entry we cannot vouch for.
            raise DatasetError(f"cannot inspect {exc.filename}: {exc.strerror}; "
                               "refusing to delete the drive root")

        for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=unlistable):
            for name in dirnames + filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), top).replace(os.sep, "/")
                if rel in known[drive]:
                    continue
                raise DatasetError(
                    f"{os.path.join(root, rel)} was not created by this dataset; "
                    "refusing to delete the drive root. Remove it by hand if intended."
                )

    for root in [*roots.values(), manifest["unscoped_root"]]:
        if os.path.isdir(root):
            _make_writable(root)
            shutil.rmtree(os_path(root))


def _make_writable(root: str) -> None:
    for dirpath, _dirs, filenames in os.walk(os_path(root)):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                try:
                    os.chmod(full, stat_module.S_IRUSR | stat_module.S_IWUSR)
                except OSError:
                    pass


# -- between-scan changes ------------------------------------------------------

def apply_mutations(manifest: dict) -> None:
    """Change the drives the way a user would between two scans."""
    seed, contents, roots = manifest["seed"], manifest["contents"], _roots(manifest)
    for op in manifest["mutations"]:
        root = roots[op["drive"]]
        path = os_path(root, op["path"])
        kind = op["op"]
        if kind in ("add", "modify"):
            _write(path, content_mod.render(seed, op["content"], contents), op["mtime_ns"])
        elif kind == "mkdir":
            os.makedirs(path, exist_ok=False)
        elif kind == "delete":
            os.remove(path)
        elif kind == "rmtree":
            shutil.rmtree(path)
        elif kind == "move":
            target = os_path(root, op["to"])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(path, target)
        else:
            raise DatasetError(f"unknown mutation {kind!r}")


def apply_corruptions(manifest: dict) -> None:
    """Silent corruption: flip bytes, keep size, restore the modification time.

    This is bit rot as a catalog sees it — nothing about the file's metadata
    says it changed, so only re-reading the bytes can tell.
    """
    roots = _roots(manifest)
    for item in manifest["corruptions"]:
        path = os_path(roots[item["drive"]], item["path"])
        before = os.stat(path)
        with open(path, "r+b") as handle:
            handle.seek(item["offset"])
            original = handle.read(1)
            handle.seek(item["offset"])
            handle.write(bytes([original[0] ^ 0xFF]))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise DatasetError(f"could not corrupt {path} silently: metadata changed")


def apply_late_edits(manifest: dict) -> None:
    """An ordinary edit after the rescan: bytes change, and so does the date.

    Two seconds later, so a FAT-family filesystem stores the new time exactly.
    """
    roots = _roots(manifest)
    for item in manifest.get("late_edits", []):
        path = os_path(roots[item["drive"]], item["path"])
        before = os.stat(path)
        with open(path, "r+b") as handle:
            handle.seek(item["offset"])
            original = handle.read(1)
            handle.seek(item["offset"])
            handle.write(bytes([original[0] ^ 0xFF]))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        if os.stat(path).st_mtime_ns == before.st_mtime_ns:
            raise DatasetError(f"could not edit {path}: its date did not change")


# -- the no-touch snapshot ---------------------------------------------------

def _hash_file(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        try:
            fd = os.open(path, flags | noatime)
        except OSError:
            fd = os.open(path, flags)
    except PermissionError:
        return "<unreadable>"
    digest = hashlib.sha256()
    try:
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def snapshot(roots: list[str]) -> dict[str, list]:
    """Path -> [kind, size, mtime_ns, mode, sha256] for everything under roots.

    Deliberately independent of ``drivefusion.core.fsio``: the instrument
    checking the gateway must not be the gateway. Access times are not
    compared — scanning may legitimately update them on filesystems mounted
    without noatime — but every byte and every other piece of metadata is.
    """
    out: dict[str, list] = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            directory = stack.pop()
            with os.scandir(os_path(directory)) as it:
                entries = sorted(it, key=lambda e: e.name)
            for entry in entries:
                full = os.path.join(directory, entry.name)
                # Not entry.stat(): on Windows that is the copy of the metadata
                # held in the *parent's* directory index, which NTFS refreshes
                # lazily. A folder that just had files written into it reports
                # a stale mtime there that later corrects itself, which reads
                # as the tool having touched it. lstat reads the entry itself.
                st = os.lstat(os_path(full))
                if entry.is_symlink():
                    record = ["link", 0, st.st_mtime_ns, st.st_mode, os.readlink(os_path(full))]
                elif entry.is_dir(follow_symlinks=False):
                    record = ["dir", 0, st.st_mtime_ns, st.st_mode, ""]
                    stack.append(full)
                else:
                    record = ["file", st.st_size, st.st_mtime_ns, st.st_mode,
                              _hash_file(os_path(full))]
                out[full] = record
    if not out:
        # An empty snapshot equals every other empty snapshot, which would make
        # the no-touch verdict vacuous. That is a broken instrument, not a pass.
        raise DatasetError(f"snapshot of {roots} found nothing")
    return out


def snapshot_diff(before: dict, after: dict, limit: int = 20) -> list[str]:
    problems = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            problems.append(f"deleted: {path}")
        elif path not in before:
            problems.append(f"created: {path}")
        elif before[path] != after[path]:
            fields = ("kind", "size", "mtime_ns", "mode", "sha256")
            changed = [f"{n}" for n, b, a in zip(fields, before[path], after[path]) if b != a]
            problems.append(f"modified ({', '.join(changed)}): {path}")
        if len(problems) >= limit:
            break
    return problems
