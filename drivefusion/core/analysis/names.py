"""A name for every drive, the same everywhere it is shown.

A drive is called by what a person would call it: its nickname, if one was
given (``drivefusion drives edit --nickname``), else the label of its volume
(what Windows shows in Explorer), else its model, else its serial, else ``drive N``. Drives
bought together often share a model and sometimes a label, so names that
would be the same are told apart: by the letter each is mounted at, where
those differ, else by the end of its serial number, else by its catalog
number. Every name is unique.
"""

from __future__ import annotations

from collections import defaultdict


def _base(drive, labels: list[str]) -> str:
    return (drive["nickname"] or (labels[0] if labels else None) or drive["model"]
            or drive["serial"] or f"drive {drive['id']}")


def drive_names(catalog) -> dict[int, str]:
    """{drive id: its display name}, every name distinct."""
    drives = list(catalog.conn.execute("SELECT * FROM drive ORDER BY id"))
    labels: dict[int, list[str]] = defaultdict(list)
    letters: dict[int, list[str]] = defaultdict(list)
    for v in catalog.conn.execute(
            "SELECT drive_id, label, last_letter FROM volume WHERE drive_id IS NOT NULL "
            "ORDER BY COALESCE(free_bytes, -1) DESC, id"):
        if v["label"]:
            labels[v["drive_id"]].append(v["label"])
        if v["last_letter"]:
            letters[v["drive_id"]].append(v["last_letter"].rstrip(":\\") + ":")
    base = {d["id"]: _base(d, labels[d["id"]]) for d in drives}
    clash = defaultdict(list)
    for d in drives:
        clash[base[d["id"]].lower()].append(d)

    names: dict[int, str] = {}
    for group in clash.values():
        if len(group) == 1:
            names[group[0]["id"]] = base[group[0]["id"]]
            continue
        firsts = [letters[d["id"]][0] if letters[d["id"]] else None for d in group]
        by_letter = None not in firsts and len(set(firsts)) == len(firsts)
        for d, letter in zip(group, firsts):
            serial = (d["serial"] or "").strip()
            if by_letter:
                tag = letter
            elif len(serial) >= 4 and sum(
                    (o["serial"] or "").strip()[-4:] == serial[-4:] for o in group) == 1:
                tag = f"…{serial[-4:]}"
            else:
                tag = f"#{d['id']}"
            names[d["id"]] = f"{base[d['id']]} ({tag})"
    return names
