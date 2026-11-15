import json
import subprocess
import textwrap
from pathlib import Path

from app.core.logs import read_log_append, read_log_chunk


def test_ansi_renderer_applies_terminal_backspace_sequences():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("abc\\b \\bd");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == "abd"


def test_ansi_renderer_keeps_color_after_cleaning_backspaces():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("\\x1b[91mabc\\b \\bd\\x1b[0m");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == '<span class="ansi-red">abd</span>'


def test_ansi_renderer_applies_cursor_left_and_right_sequences():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("abc\\x1b[D\\x1b[DZ\\x1b[C!");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == "aZc!"


def test_ansi_renderer_does_not_print_unsupported_cursor_sequences():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("abc\\x1b[A\\x1b[Bd");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == "abcd"


def test_ansi_renderer_preserves_newline_layout_when_applying_cursor_sequences():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("one\\ntwo\\x1b[DZ\\nthree");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == "one\ntwZ\nthree"


def test_ansi_renderer_treats_crlf_as_newline_not_global_cursor_reset():
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");
        const context = { window: {} };
        vm.runInNewContext(fs.readFileSync("app/static/ansi.js", "utf8"), context);
        const rendered = context.window.rcloneRunnerAnsi.render("one\\r\\ntwo\\r\\nthree");
        console.log(JSON.stringify(rendered));
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert json.loads(result.stdout) == "one\ntwo\nthree"


def test_live_log_trimming_and_scrollback_reproduce_the_log_exactly(tmp_path):
    # Multi-byte text, read in small appends so pieces split lines (and would split
    # characters if the server did not cut at character boundaries).
    log_path = tmp_path / "step.log"
    content = "".join(f"päivä {index} – ok\n" for index in range(400))
    log_path.write_text(content, encoding="utf-8")
    segments = []
    offset = 0
    while True:
        append = read_log_append(log_path, offset=offset, max_bytes=37)
        segments.append({"text": append["text"], "start": append["start"]})
        offset = append["offset"]
        if not append["more"]:
            break

    template = Path("app/templates/_log_viewer.html").read_text()
    start = template.index("    function trimLiveLog()")
    end = template.index("    async function pollLiveRun()")
    script = textwrap.dedent(
        """
        const MAX_LIVE_LOG_CHARS = 1000;
        const output = { dataset: { nextBefore: "", hasMore: "false" } };
        function updateStatus() {}
        let segments = SEGMENTS;
        TRIM_FUNCTION
        trimLiveLog();
        console.log(JSON.stringify({
          kept: segments.map((segment) => segment.text).join(""),
          nextBefore: output.dataset.nextBefore,
          hasMore: output.dataset.hasMore,
        }));
        """
    )
    script = script.replace("SEGMENTS", json.dumps(segments))
    script = script.replace("TRIM_FUNCTION", template[start:end])

    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, cwd=Path.cwd(), text=True
    )
    state = json.loads(result.stdout)

    assert len(state["kept"]) <= 1000 + 37
    assert state["hasMore"] == "true"
    older = read_log_chunk(log_path, before=int(state["nextBefore"]), limit=1_000_000)
    assert older.text + state["kept"] == content
