import json
import subprocess
import textwrap
from pathlib import Path


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
