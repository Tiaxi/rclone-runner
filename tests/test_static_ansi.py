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
