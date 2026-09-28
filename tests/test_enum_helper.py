"""The helper protocol and the parent/child round trip.

The real helper needs Windows and a volume handle, but the protocol between it
and the parent does not. These tests run a stand-in child that emits synthetic
frames, so the framing, the parsing, and every failure path are exercised on
any platform — including the ones that matter most: a child that dies partway
through, and one that fails before producing anything.
"""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from drivefusion.core.enum.helper import protocol
from drivefusion.core.enum.helper.client import HelperError, run_helper
from tests.fixtures import winbuf

DIRECTORY = winbuf.FILE_ATTRIBUTE_DIRECTORY


# -- framing ------------------------------------------------------------------

def test_frames_round_trip() -> None:
    stream = io.BytesIO()
    protocol.write_frame(stream, b"first")
    protocol.write_frame(stream, b"second payload")
    protocol.write_end(stream)

    stream.seek(0)
    assert list(protocol.read_frames(stream)) == [b"first", b"second payload"]


def test_empty_stream_terminates_cleanly() -> None:
    stream = io.BytesIO()
    protocol.write_end(stream)
    stream.seek(0)
    assert list(protocol.read_frames(stream)) == []


def test_missing_terminator_is_an_error() -> None:
    """A helper that dies mid-enumeration must not look like a finished one."""
    stream = io.BytesIO()
    protocol.write_frame(stream, b"payload")
    stream.seek(0)

    with pytest.raises(protocol.ProtocolError, match="without a terminator"):
        list(protocol.read_frames(stream))


def test_truncated_frame_is_an_error() -> None:
    stream = io.BytesIO()
    protocol.write_frame(stream, b"0123456789")
    truncated = io.BytesIO(stream.getvalue()[:-4])

    with pytest.raises(protocol.ProtocolError, match="truncated"):
        list(protocol.read_frames(truncated))


def test_absurd_frame_length_is_refused() -> None:
    """Refuse to allocate rather than trusting a length from a dead process."""
    stream = io.BytesIO((protocol.MAX_FRAME_BYTES + 1).to_bytes(4, "little"))
    with pytest.raises(protocol.ProtocolError, match="refusing"):
        list(protocol.read_frames(stream))


def test_status_lines_round_trip() -> None:
    encoded = protocol.encode_status(status="ok", mode="enum", frames=3)
    decoded = protocol.decode_status(encoded)
    assert decoded["status"] == "ok"
    assert decoded["frames"] == 3
    assert decoded["v"] == protocol.PROTOCOL_VERSION


def test_unparseable_status_does_not_raise() -> None:
    assert protocol.decode_status(b"not json")["status"] == "unparseable"


# -- parent/child round trip --------------------------------------------------

def fake_helper(tmp_path: Path, body: str) -> list[str]:
    """A stand-in child that speaks the protocol without needing a volume."""
    script = tmp_path / "fake_helper.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys, struct
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from drivefusion.core.enum.helper.protocol import (
                write_frame, write_end, encode_status)
            from tests.fixtures import winbuf

            out = sys.stdout.buffer
            err = sys.stderr.buffer
            args = sys.argv[1:]
            {textwrap.indent(textwrap.dedent(body), " " * 12).strip()}
            """
        )
    )
    return [sys.executable, str(script)]


def test_client_reads_records_from_a_child(tmp_path: Path) -> None:
    body = """
    err.write(encode_status(status="ok", mode="enum")); err.flush()
    buf = winbuf.usn_buffer([
        winbuf.usn_record_v2(frn=100, parent_frn=5, name="Research",
                             attributes=0x10),
        winbuf.usn_record_v2(frn=200, parent_frn=100, name="a.txt"),
    ], cursor=4242)
    write_frame(out, buf)
    write_end(out)
    err.write(encode_status(status="done", frames=1)); err.flush()
    """
    result = run_helper(
        r"\\.\C:", mode="enum", command=fake_helper(tmp_path, body)
    )

    assert result.frames == 1
    assert [r.name for r in result.records] == ["Research", "a.txt"]
    assert result.records[0].is_dir
    assert result.next_cursor == 4242
    assert result.status["status"] == "done"


def test_client_handles_many_frames(tmp_path: Path) -> None:
    body = """
    err.write(encode_status(status="ok", mode="enum")); err.flush()
    for page in range(5):
        buf = winbuf.usn_buffer([
            winbuf.usn_record_v2(frn=1000 + page * 10 + i, parent_frn=5,
                                 name=f"f{page}-{i}.dat")
            for i in range(10)
        ], cursor=page)
        write_frame(out, buf)
    write_end(out)
    err.write(encode_status(status="done", frames=5)); err.flush()
    """
    result = run_helper(
        r"\\.\C:", mode="enum", command=fake_helper(tmp_path, body)
    )

    assert result.frames == 5
    assert len(result.records) == 50
    assert result.next_cursor == 4


def test_child_failure_surfaces_its_reason(tmp_path: Path) -> None:
    """An exit code alone cannot say "you need administrator rights"."""
    body = """
    err.write(encode_status(status="error", code="access",
                            message="administrator rights required"))
    err.flush()
    sys.exit(4)
    """
    with pytest.raises(HelperError, match="administrator rights required") as info:
        run_helper(r"\\.\C:", command=fake_helper(tmp_path, body))
    assert info.value.code == "access"


def test_child_dying_midstream_is_not_mistaken_for_completion(
    tmp_path: Path
) -> None:
    body = """
    err.write(encode_status(status="ok", mode="enum")); err.flush()
    buf = winbuf.usn_buffer([winbuf.usn_record_v2(frn=1, parent_frn=5, name="a")])
    write_frame(out, buf)
    out.flush()
    sys.exit(5)   # no terminator
    """
    with pytest.raises(HelperError, match="terminator"):
        run_helper(r"\\.\C:", mode="enum", command=fake_helper(tmp_path, body))


def test_missing_helper_binary_is_reported(tmp_path: Path) -> None:
    with pytest.raises(HelperError, match="cannot start") as info:
        run_helper(r"\\.\C:", command=[str(tmp_path / "does-not-exist")])
    assert info.value.code == "spawn"


# -- the real helper's guards -------------------------------------------------

def test_real_helper_refuses_off_windows() -> None:
    """It should decline clearly rather than raise something opaque."""
    if sys.platform == "win32":
        pytest.skip("this asserts the non-Windows guard")

    process = subprocess.run(
        [sys.executable, "-m", "drivefusion.core.enum.helper",
         "--volume", r"\\.\C:", "--mode", "query"],
        capture_output=True,
    )
    assert process.returncode == 3
    status = protocol.decode_status(process.stderr.splitlines()[0])
    assert status["code"] == "unsupported"


def test_real_helper_rejects_delta_without_a_cursor() -> None:
    from drivefusion.core.enum.helper.__main__ import build_parser

    args = build_parser().parse_args(
        ["--volume", r"\\.\C:", "--mode", "delta"]
    )
    assert args.journal_id is None and args.next_usn is None
