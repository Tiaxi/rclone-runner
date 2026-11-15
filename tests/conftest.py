"""Shared test setup."""

import pytest

import app.core.sources as sources
from app.core.sources import FlagSet

# A small stand-in for rclone's flag lists: global flags, and each subcommand's own.
GLOBAL_FLAGS = FlagSet(
    names=frozenset(
        {
            "--backup-dir",
            "--transfers",
            "--exclude",
            "--max-delete",
            "--bwlimit",
            "--config",
            "--fast-list",
            "-L",
            "--copy-links",
            "--filter",
            "-f",
            "--filter-from",
            "--cutoff-mode",
            "--sftp-host",
            "--rc",
            "--rc-addr",
            "-v",
            "--verbose",
            "--error-on-no-transfer",
            "--cpuprofile",
            "--memprofile",
            "--dry-run",
            "-n",
        }
    ),
    with_values=frozenset(
        {
            "--backup-dir",
            "--transfers",
            "--exclude",
            "--max-delete",
            "--bwlimit",
            "--config",
            "--filter",
            "-f",
            "--filter-from",
            "--cutoff-mode",
            "--sftp-host",
            "--rc-addr",
            "--cpuprofile",
            "--memprofile",
        }
    ),
)
LOCAL_FLAGS = {
    "sync": FlagSet(frozenset({"--combined"}), frozenset({"--combined"})),
    "bisync": FlagSet(frozenset({"--filters-file"}), frozenset({"--filters-file"})),
}


@pytest.fixture(autouse=True)
def known_flags(monkeypatch, request):
    """Give every test the same small rclone flag table, whether or not rclone is installed.

    Tests marked ``real_rclone`` read rclone's own lists instead.
    """
    monkeypatch.setattr(sources, "_failed_at", {})
    if request.node.get_closest_marker("real_rclone"):
        monkeypatch.setattr(sources, "_flag_sets", {})
        return
    flag_sets = {"global": GLOBAL_FLAGS} | {
        name: LOCAL_FLAGS.get(name, FlagSet(frozenset(), frozenset()))
        for name in sources.SOURCE_SUBCOMMANDS
    }
    monkeypatch.setattr(sources, "_flag_sets", flag_sets)
