from pathlib import Path

from app.core.logs import read_log_append, read_log_chunk


def test_reads_last_lines_without_loading_entire_log(tmp_path: Path):
    log_path = tmp_path / "large.log"
    log_path.write_text("".join(f"line {index}\n" for index in range(1, 251)), encoding="utf-8")

    chunk = read_log_chunk(log_path, limit=200)

    assert chunk.text.startswith("line 51\n")
    assert chunk.text.endswith("line 250\n")
    assert chunk.has_more
    assert chunk.next_before is not None


def test_reads_older_lines_from_previous_cursor(tmp_path: Path):
    log_path = tmp_path / "large.log"
    log_path.write_text("".join(f"line {index}\n" for index in range(1, 251)), encoding="utf-8")

    tail = read_log_chunk(log_path, limit=200)
    older = read_log_chunk(log_path, before=tail.next_before, limit=200)

    assert older.text == "".join(f"line {index}\n" for index in range(1, 51))
    assert not older.has_more
    assert older.next_before is None


def test_missing_log_returns_pruned_message(tmp_path: Path):
    chunk = read_log_chunk(tmp_path / "missing.log", limit=200)

    assert chunk.text == "Log file has been pruned or is not available."
    assert not chunk.has_more
    assert chunk.next_before is None


def test_reads_log_append_from_byte_offset(tmp_path: Path):
    log_path = tmp_path / "run.log"
    log_path.write_text("one\n", encoding="utf-8")
    offset = log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("two\n")

    append = read_log_append(log_path, offset=offset)

    assert append == {
        "text": "two\n",
        "offset": log_path.stat().st_size,
        "start": offset,
        "more": False,
    }


def test_missing_append_log_returns_empty_payload(tmp_path: Path):
    append = read_log_append(tmp_path / "missing.log", offset=12)

    assert append == {"text": "", "offset": 0, "start": 0, "more": False}


def test_log_append_stops_before_a_partly_written_character(tmp_path: Path):
    log_path = tmp_path / "run.log"
    data = "päivä\n".encode()
    log_path.write_bytes(data[:2])  # "p" and the first byte of "ä"

    first = read_log_append(log_path, offset=0)
    log_path.write_bytes(data)
    second = read_log_append(log_path, offset=first["offset"])

    assert first == {"text": "p", "offset": 1, "start": 0, "more": True}
    assert second["text"] == "äivä\n"
    assert second["start"] == 1
    assert second["offset"] == len(data)


def test_log_append_returns_at_most_max_bytes_and_reports_more(tmp_path: Path):
    log_path = tmp_path / "run.log"
    log_path.write_text("ä" * 10, encoding="utf-8")  # 20 bytes

    first = read_log_append(log_path, offset=0, max_bytes=7)
    second = read_log_append(log_path, offset=first["offset"], max_bytes=100)

    assert first == {"text": "äää", "offset": 6, "start": 0, "more": True}
    assert second == {"text": "ä" * 7, "offset": 20, "start": 6, "more": False}


def test_log_chunk_reports_byte_range_and_skips_partial_character(tmp_path: Path):
    log_path = tmp_path / "run.log"
    content = "".join(f"rivi {index}\n" for index in range(5)).encode()
    log_path.write_bytes(content + "ä".encode()[:1])

    chunk = read_log_chunk(log_path, limit=2)

    assert chunk.text == "rivi 3\nrivi 4\n"
    assert chunk.end == len(content)
    assert content[chunk.start : chunk.end].decode() == chunk.text
    assert chunk.next_before == chunk.start
