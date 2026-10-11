"""Pull saves, start the linked app, then push saves when it exits."""

from __future__ import annotations

import logging
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from gamesavesync.config import ConfigError, ConfigStore, Profile
from gamesavesync.logging_setup import setup_logging
from gamesavesync.rclone import Rclone, RcloneError
from gamesavesync.state import StateStore
from gamesavesync.sync import (
    ProfileLock,
    SyncBusyError,
    matching_pids,
    pull_profile,
    push_profile,
)

logger = logging.getLogger("gamesavesync")


class LaunchError(RuntimeError):
    """The linked application could not be started."""


def split_args(args: str) -> list[str]:
    if not args or not args.strip():
        return []
    return shlex.split(args, posix=(os.name != "nt"))


def launch_environment() -> dict[str, str]:
    """Environment for the game, without this tool's bundled DLLs on PATH.

    A packaged build keeps VCRUNTIME140.dll next to GameSaveSync.exe. If the
    game inherits that folder on PATH, Windows loads the wrong copy and
    emulators such as RPCS3 refuse to start.
    """
    env = os.environ.copy()
    blocked: set[str] = set()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        blocked.add(os.path.normcase(os.path.abspath(meipass)))
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        blocked.add(os.path.normcase(app_dir))
        blocked.add(os.path.normcase(os.path.join(app_dir, "_internal")))
    kept: list[str] = []
    for part in env.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        try:
            normal = os.path.normcase(os.path.abspath(part))
        except (OSError, ValueError):
            kept.append(part)
            continue
        if normal not in blocked:
            kept.append(part)
    env["PATH"] = os.pathsep.join(kept)
    env.pop("_MEIPASS2", None)
    return env


def wait_for_app(
    profile: Profile,
    *,
    popen=subprocess.Popen,
    sleep=time.sleep,
    list_processes=None,
    pid_exists=None,
    appear_timeout: float = 120,
    poll_interval: float = 0.5,
) -> int:
    """Start ``profile.exe`` and block until it, or ``wait_process``, exits.

    ``wait_process`` covers launchers that start the real game and then quit.
    Pids already running under that name are ignored so an unrelated copy is
    left alone.
    """
    exe = Path(profile.exe)
    if not exe.is_file():
        raise LaunchError(f"Application not found: {profile.exe}")

    def processes():
        if list_processes is None:
            return None
        return list_processes()

    before: set[int] = set()
    if profile.wait_process:
        before = matching_pids(profile.wait_process, processes())
    proc = popen(
        [str(exe), *split_args(profile.args)],
        cwd=str(exe.resolve().parent),
        env=launch_environment(),
    )
    if not profile.wait_process:
        return proc.wait()
    deadline = time.monotonic() + appear_timeout
    found: set[int] = set()
    while time.monotonic() < deadline:
        found = matching_pids(profile.wait_process, processes()) - before
        if found:
            break
        sleep(poll_interval)
    if not found:
        if proc.poll() is None:
            return proc.wait()
        return proc.returncode if proc.returncode is not None else 0

    exists = pid_exists or psutil.pid_exists
    while any(exists(pid) for pid in found) or proc.poll() is None:
        sleep(poll_interval)
    return proc.returncode if proc.returncode is not None else 0


def run_launch(
    profile_name: str,
    *,
    play_anyway,
    status=None,
) -> int:
    """Sync one profile around a play session. Returns a process exit code."""
    status = status or (lambda _message: None)
    logger_obj = setup_logging()
    config_store = ConfigStore()
    state_store = StateStore()
    try:
        config = config_store.load()
        profile = config.get_profile(profile_name)
    except ConfigError as exc:
        status(str(exc))
        logger_obj.error("%s", exc)
        return 2
    if not Path(profile.exe).is_file():
        message = f"Application not found: {profile.exe}"
        status(message)
        logger_obj.error("%s", message)
        return 1
    try:
        rclone = Rclone()
    except RcloneError as exc:
        status(str(exc))
        logger_obj.error("%s", exc)
        return 1

    try:
        with ProfileLock(profile.name):
            status(f"Checking cloud saves for {profile.name}...")
            pull = pull_profile(profile, config, rclone, state_store, lock=False)
            if not pull.ok:
                status(pull.error or "Pull failed")
                if not play_anyway(pull.error or "Pull failed"):
                    return 1
            status(f"Starting {profile.name}...")
            code = wait_for_app(profile)
            logger_obj.info("%s exited with code %s", profile.name, code)
            status(f"Uploading saves for {profile.name}...")
            push = push_profile(profile, config, rclone, state_store, lock=False)
            if not push.ok:
                status(push.error or "Push failed")
                return 1
    except SyncBusyError as exc:
        status(str(exc))
        logger_obj.error("%s", exc)
        return 1
    except LaunchError as exc:
        status(str(exc))
        logger_obj.error("%s", exc)
        return 1
    status("Saves synced.")
    return 0


def console_play_anyway(message: str) -> bool:
    forced = os.environ.get("GAMESAVESYNC_PLAY_ANYWAY")
    if forced == "1":
        return True
    if forced == "0":
        return False
    if not sys.stdin or not sys.stdin.isatty():
        return False
    print(f"Cloud unreachable. {message}", file=sys.stderr)
    answer = input("Play anyway? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def launch_command(profile_name: str) -> int:
    if _gui_available():
        return _launch_with_window(profile_name)
    return run_launch(profile_name, play_anyway=console_play_anyway, status=_print_status)


def _print_status(message: str) -> None:
    print(message, flush=True)


def _gui_available() -> bool:
    if os.environ.get("GAMESAVESYNC_NO_GUI") == "1":
        return False
    try:
        import tkinter as tk
    except ImportError:
        return False
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


def _launch_with_window(profile_name: str) -> int:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Game Save Sync")
    root.resizable(False, False)
    root.geometry("460x140")
    status_var = tk.StringVar(value=f"Preparing {profile_name}...")
    ttk.Label(root, textvariable=status_var, wraplength=420, padding=16).pack(fill="both", expand=True)
    events: queue.Queue = queue.Queue()
    answers: queue.Queue = queue.Queue()
    holder = {"code": 1}

    def on_status(message: str) -> None:
        events.put(("status", message))

    def on_ask(message: str) -> bool:
        events.put(("ask", message))
        return bool(answers.get())

    def worker() -> None:
        code = run_launch(profile_name, play_anyway=on_ask, status=on_status)
        events.put(("done", code))

    def ask(message: str) -> bool:
        root.deiconify()
        dialog = tk.Toplevel(root)
        dialog.title("Cloud unreachable")
        dialog.transient(root)
        dialog.resizable(False, False)
        choice = {"value": False}
        ttk.Label(
            dialog,
            text=f"Cloud unreachable.\n\n{message}\n\nPlay anyway?",
            wraplength=420,
            padding=16,
        ).pack(fill="both")
        buttons = ttk.Frame(dialog, padding=(16, 0, 16, 16))
        buttons.pack(fill="x")

        def play() -> None:
            choice["value"] = True
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="Play anyway", command=play).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.grab_set()
        root.wait_window(dialog)
        return choice["value"]

    def poll() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "status":
                    status_var.set(str(payload))
                elif kind == "ask":
                    answers.put(ask(str(payload)))
                elif kind == "done":
                    holder["code"] = int(payload)
                    root.destroy()
                    return
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(100, poll)

    root.protocol("WM_DELETE_WINDOW", root.withdraw)
    threading.Thread(target=worker, daemon=False).start()
    poll()
    root.mainloop()
    return holder["code"]
