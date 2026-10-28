"""Mock file contents: plausible bytes for many file types, reproducible from a seed.

A dataset never stores its file bodies. Each distinct piece of content is a
*recipe* in the manifest — a type and a size, or a base recipe plus edits — and
its bytes are a pure function of ``(dataset seed, content key, recipe)``. That
gives the two properties V&V needs at once:

* **Different runs, different data.** The seed is drawn fresh for every
  dataset, so no two runs exercise the tool with the same bytes, names, sizes,
  or layout.
* **Any failure is reproducible.** The seed is in the manifest; regenerating
  with it rebuilds the identical dataset, and ``reset`` restores a used one.

Bodies carry real magic numbers (JPEG, PNG, PDF, ZIP/Office, MP4, HDF5, …) so a
dataset looks like an archive drive to anything that sniffs formats, but the
tool under test is only ever asked about bytes, never about formats.
"""

from __future__ import annotations

import functools
import json
import random

#: ext -> (category, magic header, trailer). Text types have no magic.
BINARY_TYPES: dict[str, tuple[str, bytes, bytes]] = {
    "jpg": ("image", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01", b"\xff\xd9"),
    "png": ("image", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", b"IEND\xaeB`\x82"),
    "gif": ("image", b"GIF89a", b"\x3b"),
    "tif": ("image", b"II*\x00\x08\x00\x00\x00", b""),
    "cr2": ("raw-photo", b"II*\x00\x10\x00\x00\x00CR\x02\x00", b""),
    "nef": ("raw-photo", b"MM\x00*\x00\x00\x00\x08", b""),
    "pdf": ("document", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", b"\n%%EOF\n"),
    "docx": ("document", b"PK\x03\x04\x14\x00\x06\x00", b"PK\x05\x06"),
    "xlsx": ("spreadsheet", b"PK\x03\x04\x14\x00\x06\x00", b"PK\x05\x06"),
    "pptx": ("presentation", b"PK\x03\x04\x14\x00\x06\x00", b"PK\x05\x06"),
    "zip": ("archive", b"PK\x03\x04\x0a\x00\x00\x00", b"PK\x05\x06"),
    "7z": ("archive", b"7z\xbc\xaf\x27\x1c\x00\x04", b""),
    "mp4": ("video", b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00", b""),
    "mov": ("video", b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00", b""),
    "wav": ("audio", b"RIFF\x24\x08\x00\x00WAVEfmt ", b""),
    "mp3": ("audio", b"ID3\x04\x00\x00\x00\x00\x00\x00", b""),
    "h5": ("scientific", b"\x89HDF\r\n\x1a\n", b""),
    "npy": ("scientific", b"\x93NUMPY\x01\x00v\x00", b""),
    "fits": ("scientific", b"SIMPLE  =                    T", b"END"),
    "sqlite": ("database", b"SQLite format 3\x00", b""),
    "bin": ("binary", b"", b""),
    "dat": ("binary", b"", b""),
}

TEXT_TYPES: dict[str, str] = {
    "txt": "text", "md": "text", "log": "text", "csv": "tabular",
    "json": "structured", "xml": "structured", "html": "web", "py": "code",
    "r": "code", "ipynb": "code",
}

ALL_TYPES = tuple(BINARY_TYPES) + tuple(TEXT_TYPES)

#: Typical size bands per category, in bytes. Large media is drawn separately.
SIZE_BANDS: dict[str, tuple[int, int]] = {
    "text": (200, 40_000), "tabular": (1_000, 180_000),
    "structured": (300, 60_000), "web": (800, 50_000), "code": (300, 30_000),
    "image": (20_000, 900_000), "raw-photo": (300_000, 1_500_000),
    "document": (15_000, 700_000), "spreadsheet": (8_000, 300_000),
    "presentation": (60_000, 1_200_000), "archive": (10_000, 1_000_000),
    "video": (400_000, 2_500_000), "audio": (100_000, 1_500_000),
    "scientific": (20_000, 1_500_000), "database": (8_192, 400_000),
    "binary": (64, 500_000),
}

WORDS = (
    "sample assay batch run control calibration subject trial cohort plate "
    "spectrum image stack frame channel gain offset exposure notes summary "
    "draft final review revised method result figure table appendix data "
    "analysis model fit residual baseline drift session instrument export "
    "archive project grant protocol inventory budget meeting minutes"
).split()


def category(ext: str) -> str:
    if ext in BINARY_TYPES:
        return BINARY_TYPES[ext][0]
    return TEXT_TYPES.get(ext, "binary")


def _text_body(rng: random.Random, ext: str, size: int) -> bytes:
    """ASCII-only, so truncating to an exact size never splits a character."""
    parts: list[str] = []
    length = 0

    def add(line: str) -> None:
        nonlocal length
        parts.append(line)
        length += len(line) + 1

    if ext == "csv":
        add("id,timestamp,channel,value,flag")
        while length < size:
            add(f"{rng.randrange(10**6)},{rng.randrange(1_600_000_000, 1_800_000_000)},"
                f"{rng.choice(WORDS)},{rng.uniform(-1e3, 1e3):.4f},{rng.randrange(2)}")
    elif ext in ("json", "ipynb"):
        add("[")
        while length < size:
            record = {"id": rng.randrange(10**6), "label": rng.choice(WORDS),
                      "value": round(rng.random(), 6)}
            add(json.dumps(record) + ",")
    elif ext in ("xml", "html"):
        add("<?xml version=\"1.0\"?>" if ext == "xml" else "<!doctype html>")
        while length < size:
            add(f"<{rng.choice(WORDS)} n=\"{rng.randrange(999)}\">"
                f"{' '.join(rng.choices(WORDS, k=6))}</item>")
    elif ext in ("py", "r"):
        while length < size:
            add(f"{rng.choice(WORDS)}_{rng.randrange(99)} = {rng.randrange(10**4)}"
                f"  # {' '.join(rng.choices(WORDS, k=4))}")
    else:
        while length < size:
            add(" ".join(rng.choices(WORDS, k=rng.randrange(4, 14))).capitalize() + ".")
    return "\n".join(parts).encode("ascii")[:size].ljust(size, b"\n")


def _fresh(seed: int, key: str, ext: str, size: int) -> bytes:
    rng = random.Random(f"{seed}/{key}")
    if ext in TEXT_TYPES:
        return _text_body(rng, ext, size)
    _cat, magic, trailer = BINARY_TYPES.get(ext, ("binary", b"", b""))
    if size <= len(magic) + len(trailer):
        return (magic + trailer)[:size]
    return magic + rng.randbytes(size - len(magic) - len(trailer)) + trailer


def render(seed: int, key: str, contents: dict) -> bytes:
    """The exact bytes of one content recipe."""
    return _render_cached(seed, key, json.dumps(contents[key], sort_keys=True),
                          _BaseLookup(contents))


class _BaseLookup:
    """Hashable wrapper so the cache key does not include the whole table."""

    __slots__ = ("contents",)

    def __init__(self, contents: dict) -> None:
        self.contents = contents

    def __hash__(self) -> int:
        return id(self.contents)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _BaseLookup) and other.contents is self.contents


@functools.lru_cache(maxsize=16)  # bodies reach 6 MB; copies are written close together
def _render_cached(seed: int, key: str, recipe_json: str, lookup: _BaseLookup) -> bytes:
    recipe = json.loads(recipe_json)
    if recipe.get("base") is None:
        data = _fresh(seed, key, recipe["type"], recipe["size"])
    else:
        data = bytearray(render(seed, recipe["base"], lookup.contents))
        for edit in recipe.get("edits", ()):
            if "flip" in edit:
                # Inverting bytes guarantees a difference; random "new" bytes
                # could coincide with the old ones and silently test nothing.
                offset, length = edit["flip"]
                for i in range(offset, offset + length):
                    data[i] ^= 0xFF
            elif "append" in edit:
                data.extend(random.Random(f"{seed}/{key}/append").randbytes(edit["append"]))
        data = bytes(data)
    if len(data) != recipe["size"]:
        raise AssertionError(f"content {key}: rendered {len(data)} bytes, "
                             f"recipe says {recipe['size']}")
    return data
