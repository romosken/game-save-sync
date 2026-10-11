import os

import pytest

from gamesavesync.rclone import Rclone, RcloneError, _no_window_kwargs, parse_transfer_percent


def test_rclone_console_is_hidden_on_windows_only():
    if os.name == "nt":
        options = _no_window_kwargs()
        assert options["creationflags"]
        assert options["startupinfo"].wShowWindow == 0
    else:
        assert _no_window_kwargs() == {}


class _FakeRclone(Rclone):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def has_remote(self, name: str) -> bool:
        return False

    def _run(self, args: list[str], *, timeout: float | None):
        self.calls.append(args)

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()


def test_drive_connect_sends_your_client_credentials():
    rclone = _FakeRclone()
    rclone.connect("gdrive", "drive", client_id="my-id", client_secret="my-secret")
    command = rclone.calls[0]
    assert command[:4] == ["config", "create", "gdrive", "drive"]
    assert command[command.index("client_id") + 1] == "my-id"
    assert command[command.index("client_secret") + 1] == "my-secret"
    assert "drive.file" in command


def test_parse_transfer_percent():
    line = "2026/10/11 01:29:43 NOTICE: 256 KiB / 320 KiB, 80%, 1.2 MiB/s, ETA 1s"
    assert parse_transfer_percent(line) == 80
    assert parse_transfer_percent("Transferred: 1 / 2, 50%") == 50
    assert parse_transfer_percent("directory not found") is None


def test_drive_connect_requires_a_client():
    with pytest.raises(RcloneError):
        _FakeRclone().connect("gdrive", "drive")
