from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LogChunk:
    text: str
    next_before: int | None
    has_more: bool


def read_log_chunk(path: Path, before: int | None = None, limit: int = 200) -> LogChunk:
    if not path.exists():
        return LogChunk("Log file has been pruned or is not available.", None, False)

    limit = max(1, limit)
    file_size = path.stat().st_size
    end = file_size if before is None else max(0, min(before, file_size))
    if end == 0:
        return LogChunk("", None, False)

    buffer = bytearray()
    buffer_start = end
    block_size = 8192

    with path.open("rb") as log_file:
        while buffer_start > 0:
            read_start = max(0, buffer_start - block_size)
            log_file.seek(read_start)
            buffer[:0] = log_file.read(buffer_start - read_start)
            buffer_start = read_start

            start_in_buffer = _chunk_start(buffer, limit)
            if start_in_buffer is not None:
                start = buffer_start + start_in_buffer
                return LogChunk(_decode(buffer[start_in_buffer:]), start, start > 0)

    return LogChunk(_decode(buffer), None, False)


def read_log_append(path: Path, offset: int = 0) -> dict[str, object]:
    if not path.exists():
        return {"text": "", "offset": 0}

    file_size = path.stat().st_size
    start = max(0, min(offset, file_size))
    with path.open("rb") as log_file:
        log_file.seek(start)
        data = log_file.read()
    return {"text": _decode(data), "offset": file_size}


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
