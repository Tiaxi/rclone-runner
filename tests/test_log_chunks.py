from pathlib import Path

from app.core.logs import read_log_chunk


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
