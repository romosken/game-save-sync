"""Command line entry point. With no command, open the settings window."""

from __future__ import annotations

import argparse
import sys

from gamesavesync import __version__
from gamesavesync.config import ConfigError, ConfigStore
from gamesavesync.logging_setup import setup_logging
from gamesavesync.rclone import Rclone, RcloneError
from gamesavesync.state import StateStore
from gamesavesync.sync import pull_profile, push_profile, sync_all, sync_profile_both


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gamesavesync",
        description="Sync emulator and offline game saves through cloud storage.",
    )
    parser.add_argument("--version", action="version", version=f"gamesavesync {__version__}")
    sub = parser.add_subparsers(dest="command")

    launch = sub.add_parser("launch", help="Pull saves, run the linked app, then push saves")
    launch.add_argument("profile")

    pull = sub.add_parser("pull", help="Download newer saves for one profile")
    pull.add_argument("profile")

    push = sub.add_parser("push", help="Upload newer saves for one profile")
    push.add_argument("profile")

    sync = sub.add_parser("sync", help="Pull then push one profile")
    sync.add_argument("profile")

    sub.add_parser("sync-all", help="Pull then push every profile")

    args = parser.parse_args(argv)
    setup_logging()
    try:
        if args.command is None:
            from gamesavesync.ui import run_ui

            run_ui()
            return 0
        if args.command == "launch":
            from gamesavesync.launcher import launch_command

            return launch_command(args.profile)
        if args.command == "pull":
            return _run_one(args.profile, "pull")
        if args.command == "push":
            return _run_one(args.profile, "push")
        if args.command == "sync":
            return _run_one(args.profile, "both")
        if args.command == "sync-all":
            return _run_all()
    except (ConfigError, RcloneError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


def _run_one(profile_name: str, direction: str) -> int:
    config = ConfigStore().load()
    profile = config.get_profile(profile_name)
    rclone = Rclone()
    store = StateStore()
    if direction == "pull":
        result = pull_profile(profile, config, rclone, store)
    elif direction == "push":
        result = push_profile(profile, config, rclone, store)
    else:
        result = sync_profile_both(profile, config, rclone, store)
    _print_result(result)
    return 0 if result.ok else 1


def _run_all() -> int:
    config = ConfigStore().load()
    results = sync_all(config, Rclone(), StateStore())
    failed = 0
    for result in results:
        _print_result(result)
        if result.result == "failed":
            failed += 1
    return 1 if failed else 0


def _print_result(result) -> None:
    line = f"{result.name}: {result.result}"
    if result.error:
        line = f"{line} ({result.error})"
    print(line)
    for folder in result.folders:
        state = "ok" if folder.ok else "failed"
        detail = folder.error or (
            f"downloaded {folder.downloaded}, uploaded {folder.uploaded}, "
            f"backed up {folder.backed_up}"
        )
        print(f"  {folder.label}: {state} ({detail})")


if __name__ == "__main__":
    raise SystemExit(main())
