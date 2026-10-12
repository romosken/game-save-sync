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


def _sync_tool_roots() -> list[str]:
    """Folders that contain this tool's private DLLs."""
    roots: list[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        roots.append(app_dir)
        roots.append(os.path.join(app_dir, "_internal"))
    return roots


def _inside_sync_tool(path: str, roots: list[str]) -> bool:
    try:
        normal = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            root_normal = os.path.normcase(os.path.normpath(os.path.abspath(root)))
        except (OSError, ValueError):
            continue
        if normal == root_normal or normal.startswith(root_normal + os.sep):
            return True
    return False


def launch_environment() -> dict[str, str]:
    """Environment for the game, without this tool's bundled DLLs on PATH.

    A packaged build keeps VCRUNTIME140.dll in its ``_internal`` folder. If the
    game inherits that folder, Windows loads the wrong copy and emulators such
    as RPCS3 refuse to start.
    """
    env = os.environ.copy()
    roots = _sync_tool_roots()
    kept = [
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part and not _inside_sync_tool(part, roots)
    ]
    if os.name == "nt":
        system_root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
        system_dirs = [
            os.path.join(system_root, "System32"),
            system_root,
            os.path.join(system_root, "System32", "Wbem"),
        ]
        merged: list[str] = []
        seen: set[str] = set()
        for part in system_dirs + kept:
            key = os.path.normcase(os.path.normpath(part))
            if key in seen:
                continue
            seen.add(key)
            merged.append(part)
        kept = merged
    env["PATH"] = os.pathsep.join(kept)
    env.pop("_MEIPASS2", None)
    return env


def _apply_launch_environment() -> dict[str, str | None]:
    """Point DLL lookup at Windows while the game starts, then restore it."""
    previous_path = os.environ.get("PATH")
    cleaned = launch_environment()
    os.environ["PATH"] = cleaned["PATH"]
    previous_dll: str | None = None
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetDllDirectoryW(32768, buffer)
        previous_dll = buffer.value if length else None
        kernel32.SetDllDirectoryW(None)
    return {"PATH": previous_path, "dll": previous_dll}


def _restore_launch_environment(saved: dict[str, str | None]) -> None:
    previous_path = saved.get("PATH")
    if previous_path is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = previous_path
    if os.name != "nt":
        return
    import ctypes

    ctypes.WinDLL("kernel32", use_last_error=True).SetDllDirectoryW(saved.get("dll"))


def _shell_execute_and_wait(exe: Path, args: list[str]) -> int:
    """Open the program from its own folder, the way Explorer does."""
    import ctypes
    from ctypes import wintypes

    resolved = exe.resolve()
    game_dir = str(resolved.parent)
    parameters = subprocess.list2cmdline(args) if args else None

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x00000040
    sw_shownormal = 1
    infinite = 0xFFFFFFFF
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell_execute = shell32.ShellExecuteExW
    shell_execute.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell_execute.restype = wintypes.BOOL

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "open"
    info.lpFile = str(resolved)
    info.lpParameters = parameters
    info.lpDirectory = game_dir
    info.nShow = sw_shownormal
    saved = _apply_launch_environment()
    try:
        started = bool(shell_execute(ctypes.byref(info)))
        error = ctypes.get_last_error()
    finally:
        _restore_launch_environment(saved)
    if not started:
        raise LaunchError(f"Could not start {resolved} (Windows error {error})")
    if not info.hProcess:
        raise LaunchError(f"Started {resolved} but could not wait for it to close")
    kernel32.WaitForSingleObject(info.hProcess, infinite)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return int(code.value)


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

    game_dir = str(exe.resolve().parent)
    arguments = split_args(profile.args)
    # On Windows, open the program from its own folder after the sync. A direct
    # child of this tool still searches the sync tool's DLL folder and emulators
    # then refuse VCRUNTIME140.dll.
    if os.name == "nt" and not profile.wait_process:
        return _shell_execute_and_wait(exe, arguments)

    before: set[int] = set()
    if profile.wait_process:
        before = matching_pids(profile.wait_process, processes())
    saved = _apply_launch_environment() if os.name == "nt" else None
    try:
        proc = popen(
            [str(exe), *arguments],
            cwd=game_dir,
            env=launch_environment(),
        )
    finally:
        if saved is not None:
            _restore_launch_environment(saved)
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
