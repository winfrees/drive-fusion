"""Framing between the enumeration helper and the parent process.

The helper forwards **raw IOCTL output buffers** rather than re-serialising
records. At ten million records any per-record encoding would dominate the
cost, and forwarding the bytes untouched means the parent parses them with the
same tested functions in ``records.py`` that a same-process read would use —
there is no second decoder to keep in step.

stdout carries only frames. Structured status goes on stderr as JSON lines, so
a diagnostic can never be mistaken for payload.
"""

from __future__ import annotations

import json
import struct
from typing import BinaryIO, Iterator

#: Frames are length-prefixed; a zero length terminates the stream.
_LENGTH = struct.Struct("<I")

#: Refuse absurd frames rather than trying to allocate them. Buffers are ~1 MB.
MAX_FRAME_BYTES = 64 * 1024 * 1024

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    """Raised when the helper's output cannot be read as a frame stream."""


def write_frame(stream: BinaryIO, payload: bytes) -> None:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(payload)} bytes exceeds the limit")
    stream.write(_LENGTH.pack(len(payload)))
    stream.write(payload)


def write_end(stream: BinaryIO) -> None:
    stream.write(_LENGTH.pack(0))
    stream.flush()


def read_frames(stream: BinaryIO) -> Iterator[bytes]:
    """Yield payloads until the terminator.

    A truncated stream raises rather than ending quietly: the helper dying
    mid-enumeration must not look like a volume that simply ended.
    """
    while True:
        header = _read_exactly(stream, _LENGTH.size, allow_eof=True)
        if header is None:
            raise ProtocolError(
                "helper output ended without a terminator; the process "
                "probably died mid-enumeration"
            )
        (length,) = _LENGTH.unpack(header)
        if length == 0:
            return
        if length > MAX_FRAME_BYTES:
            raise ProtocolError(f"frame claims {length} bytes; refusing")
        payload = _read_exactly(stream, length)
        if payload is None:
            raise ProtocolError(f"frame truncated; expected {length} bytes")
        yield payload


def _read_exactly(stream: BinaryIO, count: int, *, allow_eof: bool = False):
    chunks = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks and allow_eof:
                return None
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_status(**fields) -> bytes:
    fields.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(fields, separators=(",", ":")) + "\n").encode("utf-8")


def decode_status(line: bytes | str) -> dict:
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"status": "unparseable", "raw": text[:500]}
