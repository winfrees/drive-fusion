"""``dfscan-helper`` — the elevated enumerator.

Reading the NTFS change journal requires a volume handle, and a volume handle
requires administrator rights. Rather than elevate the whole application, that
one capability lives here (docs/PLAN.md §6.5).

The entire program is: open a volume read-only, stream what the IOCTL returns,
exit. There is no write code, no delete code, and no network code in it, so the
elevated surface is a few hundred lines that can be audited in one sitting —
and it is covered by the same read-only lint and no-touch test as the rest.

    python -m drivefusion.core.enum.helper --volume \\\\.\\C: --mode enum
"""

from __future__ import annotations

import argparse
import sys

from drivefusion.core.enum.helper.protocol import (
    encode_status,
    write_end,
    write_frame,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3
EXIT_ACCESS = 4
EXIT_FAILED = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dfscan-helper", description="Read-only NTFS enumeration helper"
    )
    parser.add_argument("--volume", required=True, help=r"device path, e.g. \\.\C:")
    parser.add_argument(
        "--mode", choices=("enum", "delta", "query"), default="query"
    )
    parser.add_argument("--journal-id", type=int, default=None)
    parser.add_argument("--next-usn", type=int, default=None)
    parser.add_argument(
        "--buffer-bytes", type=int, default=1 << 20,
        help="IOCTL output buffer size; larger means fewer transitions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    err = sys.stderr.buffer
    out = sys.stdout.buffer

    if sys.platform != "win32":
        err.write(
            encode_status(
                status="error",
                code="unsupported",
                message="volume enumeration requires Windows",
            )
        )
        err.flush()
        return EXIT_UNSUPPORTED

    from drivefusion.core.enum import journal, winio

    try:
        if args.mode == "query":
            state = journal.query_journal(args.volume)
            err.write(
                encode_status(
                    status="ok",
                    mode="query",
                    journal_id=None if state is None else state.journal_id,
                    next_usn=None if state is None else state.next_usn,
                    lowest_valid_usn=(
                        None if state is None else state.lowest_valid_usn
                    ),
                )
            )
            err.flush()
            write_end(out)
            return EXIT_OK

        if args.mode == "enum":
            code = journal.FSCTL_ENUM_USN_DATA
            state = journal.query_journal(args.volume)
            high = state.next_usn if state else 0
            payload = journal.build_enum_input(0, high)

            def next_payload(cursor: int) -> bytes:
                return journal.build_enum_input(cursor, high)

        else:
            if args.journal_id is None or args.next_usn is None:
                err.write(
                    encode_status(
                        status="error",
                        code="usage",
                        message="delta mode needs --journal-id and --next-usn",
                    )
                )
                err.flush()
                return EXIT_USAGE

            code = journal.FSCTL_READ_USN_JOURNAL
            cursor = journal.JournalCursor(args.journal_id, args.next_usn)
            payload = journal.build_read_journal_input(cursor)

            def next_payload(next_cursor: int) -> bytes:
                return journal.build_read_journal_input(
                    journal.JournalCursor(args.journal_id, next_cursor)
                )

        err.write(encode_status(status="ok", mode=args.mode))
        err.flush()

        frames = 0
        last_cursor = None
        for buffer in _stream_buffers(
            args.volume, code, payload, args.buffer_bytes, next_payload
        ):
            write_frame(out, buffer)
            frames += 1
            last_cursor = int.from_bytes(buffer[:8], "little", signed=True)
        write_end(out)

        err.write(
            encode_status(status="done", frames=frames, next_cursor=last_cursor)
        )
        err.flush()
        return EXIT_OK

    except winio.VolumeAccessError as exc:
        err.write(
            encode_status(
                status="error",
                code="access" if exc.winerror == 5 else "volume",
                winerror=exc.winerror,
                message=str(exc),
            )
        )
        err.flush()
        return EXIT_ACCESS if exc.winerror == 5 else EXIT_FAILED
    except Exception as exc:  # pragma: no cover - defensive
        err.write(encode_status(status="error", code="failed", message=str(exc)))
        err.flush()
        return EXIT_FAILED


def _stream_buffers(device_path, code, payload, buffer_bytes, next_payload):
    """Drive the paged IOCTL, yielding raw output buffers.

    Deliberately not parsed here: the parent does that with the shared
    functions in ``records.py``, so the helper stays a pipe rather than a
    second implementation.
    """
    from drivefusion.core.enum import winio

    handle = winio.open_volume(device_path)
    try:
        current = payload
        while True:
            data = winio._ioctl(handle, code, current, buffer_bytes)
            if len(data) <= 8:
                return
            yield data
            cursor = int.from_bytes(data[:8], "little", signed=True)
            current = next_payload(cursor)
    finally:
        winio.close_handle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
