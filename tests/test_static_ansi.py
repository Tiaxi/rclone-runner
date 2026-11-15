import json
import subprocess
import textwrap
from pathlib import Path

from app.core.logs import read_log_chunk


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


def test_live_log_view_trims_old_output_and_keeps_byte_offset_for_scrollback(tmp_path):
    template = Path("app/templates/_log_viewer.html").read_text()
    start = template.index("    function trimLiveLog()")
    end = template.index("    async function pollLiveRun()")
    script = textwrap.dedent(
        """
        const MAX_LIVE_LOG_CHARS = 1000;
        const output = { dataset: { nextBefore: "", hasMore: "false" } };
        function updateStatus() {}
        const lines = [];
        for (let i = 0; i < 300; i++) lines.push(`päivä ${i}\\n`);
        let rawLogText = lines.join("");
        const fullBytes = new TextEncoder().encode(rawLogText).length;
        let appendOffset = fullBytes;
        TRIM_FUNCTION
        trimLiveLog();
        const keptBytes = new TextEncoder().encode(rawLogText).length;
        console.log(JSON.stringify({
          length: rawLogText.length,
          startsAtLine: rawLogText.startsWith("päivä "),
          endsWithLast: rawLogText.endsWith("päivä 299\\n"),
          nextBefore: Number(output.dataset.nextBefore),
          expectedNextBefore: fullBytes - keptBytes,
          hasMore: output.dataset.hasMore,
          firstLine: rawLogText.split("\\n", 1)[0],
        }));
        """
    ).replace("TRIM_FUNCTION", template[start:end])

    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, cwd=Path.cwd(), text=True
    )
    state = json.loads(result.stdout)

    assert state["length"] <= 1000
    assert state["startsAtLine"] is True
    assert state["endsWithLast"] is True
    assert state["nextBefore"] == state["expectedNextBefore"]
    assert state["hasMore"] == "true"
    # Scrolling up loads lines ending at nextBefore: exactly the line before the kept text.
    log_path = tmp_path / "step.log"
    log_path.write_text("".join(f"päivä {i}\n" for i in range(300)), encoding="utf-8")
    first_kept = int(state["firstLine"].split()[1])
    older = read_log_chunk(log_path, before=state["nextBefore"], limit=1)
    assert older.text == f"päivä {first_kept - 1}\n"
