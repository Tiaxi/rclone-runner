from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Most bytes one append request returns. A client that is further behind fetches again, so a
# single response (and the browser's view of it) stays small.
APPEND_MAX_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class LogChunk:
    text: str
    next_before: int | None
    has_more: bool
    # Byte offsets of the text in the file. Both fall on UTF-8 character boundaries, so the
    # browser can trim and reload the log by these offsets without re-encoding text.
    start: int = 0
    end: int = 0


def read_log_chunk(path: Path, before: int | None = None, limit: int = 200) -> LogChunk:
    if not path.exists():
        return LogChunk("Log file has been pruned or is not available.", None, False)

    limit = max(1, limit)
    with path.open("rb") as log_file:
        file_size = log_file.seek(0, 2)
        end = file_size if before is None else max(0, min(before, file_size))
        # The writer may be partway through a multi-byte character; leave it for later.
        log_file.seek(max(0, end - 3))
        end -= _incomplete_utf8_tail(log_file.read(end - max(0, end - 3)))
        if end == 0:
            return LogChunk("", None, False)

        buffer = bytearray()
        buffer_start = end
        block_size = 8192
        while buffer_start > 0:
            read_start = max(0, buffer_start - block_size)
            log_file.seek(read_start)
            buffer[:0] = log_file.read(buffer_start - read_start)
            buffer_start = read_start

            start_in_buffer = _chunk_start(buffer, limit)
            if start_in_buffer is not None:
                start = buffer_start + start_in_buffer
                return LogChunk(_decode(buffer[start_in_buffer:]), start, start > 0, start, end)

    return LogChunk(_decode(buffer), None, False, 0, end)


def read_log_append(
    path: Path, offset: int = 0, max_bytes: int = APPEND_MAX_BYTES
) -> dict[str, object]:
    """New log text from offset, up to max_bytes, ending on a whole UTF-8 character.

    "offset" is where the next request should continue, "start" where this text begins,
    and "more" says whether more was already written than this response returned.
    """
    if not path.exists():
        return {"text": "", "offset": 0, "start": 0, "more": False}

    with path.open("rb") as log_file:
        file_size = log_file.seek(0, 2)
        start = max(0, min(offset, file_size))
        log_file.seek(start)
        data = log_file.read(min(max_bytes, file_size - start))
    data = data[: len(data) - _incomplete_utf8_tail(data)]
    end = start + len(data)
    return {"text": _decode(data), "offset": end, "start": start, "more": end < file_size}


def _incomplete_utf8_tail(data: bytes) -> int:
    """Number of trailing bytes that start a UTF-8 character but do not complete it."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue  # continuation byte; keep looking for the lead byte
        if byte & 0x80 == 0:
            return 0
        needed = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4
        return back if back < needed else 0
    return 0


def _chunk_start(buffer: bytearray, limit: int) -> int | None:
    if not buffer:
        return 0
    newline_count = buffer.count(b"\n")
    line_count = newline_count if buffer.endswith(b"\n") else newline_count + 1
    excess = line_count - limit
    if excess <= 0:
        return None

    newline_seen = 0
    for index, char in enumerate(buffer):
        if char == 10:
            newline_seen += 1
            if newline_seen == excess:
                return index + 1
    return None


def _decode(value: bytes | bytearray) -> str:
    return bytes(value).decode("utf-8", errors="replace")
