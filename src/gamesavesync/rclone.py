"""Thin wrapper around the bundled or installed rclone binary."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from gamesavesync.util import app_dir

LIST_TIMEOUT = 120
TRANSFER_TIMEOUT = 3600
AUTH_TIMEOUT = 600


class RcloneError(RuntimeError):
    def __init__(self, message: str, *, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def find_rclone() -> Path:
    """Locate rclone.exe next to the app, under vendor/, or on PATH."""
    override = os.environ.get("GAMESAVESYNC_RCLONE")
    if override and Path(override).is_file():
        return Path(override)
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
        candidates.extend(
            [
                exe_dir / "rclone.exe",
                exe_dir / "rclone",
                meipass / "rclone.exe",
                meipass / "rclone",
            ]
        )
    project = Path(__file__).resolve().parents[2]
    candidates.extend([project / "vendor" / "rclone.exe", project / "vendor" / "rclone"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("rclone")
    if found:
        return Path(found)
    raise RcloneError(
        "rclone was not found. Install rclone or place rclone.exe next to GameSaveSync."
    )


def is_missing_directory(exc: RcloneError) -> bool:
    text = f"{exc}\n{exc.stderr}".lower()
    return "directory not found" in text


_SHARED_CLIENT_MARKERS = (
    "shared google drive client_id",
    "shared client_id",
    "making-your-own-client-id",
    "continue using the shared client_id",
)

_OWN_CLIENT_HELP = (
    "Google Drive no longer accepts rclone's shared login. "
    "In Connect cloud, paste a client ID and client secret from your own "
    "Google Cloud Desktop app. Enable the Google Drive API and add the scope "
    "https://www.googleapis.com/auth/drive.file. "
    "Create the client at https://console.cloud.google.com/"
)


@contextmanager
def _path_list(relative_paths: list[str]) -> Iterator[str]:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        prefix="gamesavesync-",
        suffix=".txt",
    )
    try:
        handle.write("\n".join(relative_paths))
        handle.write("\n")
        handle.close()
        yield handle.name
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass


def _no_window_kwargs() -> dict:
    """Stop Windows from opening a console for rclone.exe."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


_TRANSFER_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")


def parse_transfer_percent(line: str) -> float | None:
    """Read a percentage from an rclone stats line."""
    if "ETA" not in line and "Transferred" not in line:
        return None
    match = _TRANSFER_PERCENT.search(line)
    if match is None:
        return None
    return max(0.0, min(100.0, float(match.group(1))))


def _useful_rclone_output(stdout: str, stderr: str) -> str:
    kept: list[str] = []
    saw_shared_client = False
    for chunk in (stderr, stdout):
        for line in (chunk or "").splitlines():
            lowered = line.lower()
            if any(marker in lowered for marker in _SHARED_CLIENT_MARKERS):
                saw_shared_client = True
                continue
            stripped = line.strip()
            if stripped:
                kept.append(stripped)
    if kept:
        return "\n".join(kept)
    if saw_shared_client:
        return _OWN_CLIENT_HELP
    return ""


class Rclone:
    def __init__(self, exe: Path | None = None, config_path: Path | None = None) -> None:
        self.exe = exe or find_rclone()
        self.config_path = config_path or (app_dir() / "rclone.conf")

    def _run(
        self,
        args: list[str],
        *,
        timeout: float | None,
        on_line=None,
    ) -> subprocess.CompletedProcess[str]:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        command = [str(self.exe), "--config", str(self.config_path), *args]
        try:
            if on_line is None:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                    **_no_window_kwargs(),
                )
            else:
                completed = self._run_streaming(command, timeout=timeout, on_line=on_line)
        except subprocess.TimeoutExpired as exc:
            raise RcloneError("Cloud unreachable (timed out)", stderr=str(exc)) from exc
        except OSError as exc:
            raise RcloneError(str(exc)) from exc
        if completed.returncode != 0:
            detail = _useful_rclone_output(completed.stdout, completed.stderr)
            message = detail or f"rclone exited with status {completed.returncode}"
            raw = f"{completed.stderr or ''}\n{completed.stdout or ''}".strip()
            raise RcloneError(message, returncode=completed.returncode, stderr=raw)
        return completed

    def _run_streaming(self, command: list[str], *, timeout: float | None, on_line) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def read_stdout() -> None:
            if process.stdout is None:
                return
            stdout_parts.append(process.stdout.read())

        def read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_parts.append(line)
                on_line(line.rstrip("\r\n"))

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def lsjson(self, remote_path: str) -> list[dict]:
        try:
            completed = self._run(
                ["lsjson", remote_path, "--recursive"],
                timeout=LIST_TIMEOUT,
            )
        except RcloneError as exc:
            if is_missing_directory(exc):
                return []
            raise
        text = completed.stdout.strip()
        if not text:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            raise RcloneError(f"Unexpected rclone listing for {remote_path}")
        return data

    def lsf_dirs(self, remote_path: str) -> list[str]:
        try:
            completed = self._run(["lsf", remote_path, "--dirs-only"], timeout=LIST_TIMEOUT)
        except RcloneError as exc:
            if is_missing_directory(exc):
                return []
            raise
        names = []
        for line in completed.stdout.splitlines():
            name = line.strip().rstrip("/")
            if name:
                names.append(name)
        return names

    def md5_files(self, remote_dir: str, relative_paths: list[str]) -> dict[str, str]:
        """Hash only the listed files. Used when size matches but the time does not."""
        paths = [path.replace("\\", "/").lstrip("/") for path in relative_paths if path.strip()]
        if not paths:
            return {}
        with _path_list(paths) as list_path:
            completed = self._run(
                ["hashsum", "MD5", remote_dir, "--files-from-raw", list_path],
                timeout=LIST_TIMEOUT,
            )
        hashes: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            pieces = line.split(maxsplit=1)
            if len(pieces) != 2:
                continue
            hashes[pieces[1].replace("\\", "/")] = pieces[0].lower()
        return hashes

    def copyto(self, source: str, dest: str) -> None:
        self._run(["copyto", source, dest], timeout=TRANSFER_TIMEOUT)

    def moveto(self, source: str, dest: str) -> None:
        self._run(["moveto", source, dest], timeout=TRANSFER_TIMEOUT)

    def copy_files(self, source: str, dest: str, relative_paths: list[str], on_progress=None) -> None:
        """Copy many files in one rclone process, keeping their subfolders."""
        self._transfer_files("copy", source, dest, relative_paths, on_progress=on_progress)

    def move_files(self, source: str, dest: str, relative_paths: list[str], on_progress=None) -> None:
        """Move many files in one rclone process, keeping their subfolders."""
        self._transfer_files("move", source, dest, relative_paths, on_progress=on_progress)

    def _transfer_files(
        self,
        operation: str,
        source: str,
        dest: str,
        relative_paths: list[str],
        on_progress=None,
    ) -> None:
        paths = [path.replace("\\", "/").lstrip("/") for path in relative_paths if path.strip()]
        if not paths:
            return
        with _path_list(paths) as list_path:
            def on_line(line: str) -> None:
                if on_progress is None:
                    return
                percent = parse_transfer_percent(line)
                if percent is not None:
                    on_progress(percent)

            self._run(
                [
                    operation,
                    source,
                    dest,
                    "--files-from-raw",
                    list_path,
                    "--transfers",
                    "8",
                    "--checkers",
                    "8",
                    "--stats",
                    "500ms",
                    "--stats-one-line",
                ],
                timeout=TRANSFER_TIMEOUT,
                on_line=on_line,
            )

    def purge(self, remote_path: str) -> None:
        try:
            self._run(["purge", remote_path], timeout=LIST_TIMEOUT)
        except RcloneError as exc:
            if is_missing_directory(exc):
                return
            raise

    def list_remotes(self) -> list[str]:
        if not self.config_path.exists():
            return []
        completed = self._run(["listremotes"], timeout=LIST_TIMEOUT)
        remotes = []
        for line in completed.stdout.splitlines():
            name = line.strip()
            if name.endswith(":"):
                name = name[:-1]
            if name:
                remotes.append(name)
        return remotes

    def has_remote(self, name: str) -> bool:
        return name in self.list_remotes()

    def check_connection(self, remote: str) -> None:
        self._run(["lsd", f"{remote}:"], timeout=LIST_TIMEOUT)

    def connect(
        self,
        remote_name: str,
        backend: str,
        *,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        """Create or reconnect an OAuth remote.

        Google Drive is created with the ``drive.file`` scope, so the token can
        only see files this app created. The provider login page collects the
        password; rclone stores the refresh token in the local config file.
        Google requires your own OAuth client id and secret. rclone's shared
        client is retired during 2026 and ``config create`` refuses it.
        """
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        if backend == "drive" and (not client_id or not client_secret):
            raise RcloneError(_OWN_CLIENT_HELP)
        if self.has_remote(remote_name):
            if backend == "drive":
                self._run(
                    [
                        "config",
                        "update",
                        remote_name,
                        "client_id",
                        client_id,
                        "client_secret",
                        client_secret,
                        "scope",
                        "drive.file",
                        "config_is_local",
                        "true",
                    ],
                    timeout=AUTH_TIMEOUT,
                )
            self._run(["config", "reconnect", f"{remote_name}:"], timeout=AUTH_TIMEOUT)
            return
        args = ["config", "create", remote_name, backend]
        if backend == "drive":
            # drive.file can only see files this app creates. config_is_local
            # opens the provider login in a browser on this PC.
            args.extend(
                [
                    "client_id",
                    client_id,
                    "client_secret",
                    client_secret,
                    "scope",
                    "drive.file",
                    "config_is_local",
                    "true",
                ]
            )
        self._run(args, timeout=AUTH_TIMEOUT)
