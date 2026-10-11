"""Windows desktop shortcuts that launch a profile through this app."""

from __future__ import annotations

import sys
from pathlib import Path


def launch_invocation(profile_name: str) -> tuple[str, str]:
    """Return ``(executable, arguments)`` for ``launch <profile>``."""
    safe_name = profile_name.replace('"', "")
    if getattr(sys, "frozen", False):
        return sys.executable, f'launch "{safe_name}"'
    return sys.executable, f'-m gamesavesync launch "{safe_name}"'


def create_desktop_shortcut(profile_name: str, icon_source: str = "") -> Path:
    if sys.platform != "win32":
        raise OSError("Desktop shortcuts can only be created on Windows")
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = shell.SpecialFolders("Desktop")
    link_path = str(Path(desktop) / f"{profile_name}.lnk")
    shortcut = shell.CreateShortCut(link_path)
    target, arguments = launch_invocation(profile_name)
    shortcut.Targetpath = target
    shortcut.Arguments = arguments
    shortcut.WorkingDirectory = str(Path(target).resolve().parent)
    shortcut.Description = f"Launch {profile_name} and sync its saves"
    if icon_source:
        shortcut.IconLocation = f"{icon_source},0"
    shortcut.save()
    return Path(link_path)
