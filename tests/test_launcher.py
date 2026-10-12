import json
import os
import stat
import sys
import time
from pathlib import Path

import psutil
import pytest

from gamesavesync.config import AppConfig, ConfigStore, Profile, SaveFolder
from gamesavesync.launcher import launch_environment, run_launch, wait_for_app
from gamesavesync.state import StateStore
from gamesavesync.sync import ProfileLock, SyncBusyError, is_app_running, process_matches


class _Proc:
    def __init__(self, name: str, exe: str = "", cmdline: list[str] | None = None, pid: int = 1):
        self.pid = pid
        self.info = {"name": name, "exe": exe, "cmdline": cmdline or [], "pid": pid}


def test_process_match_uses_name_path_or_command():
    assert process_matches(_Proc("pcsx2-qt.exe"), "", "pcsx2-qt.exe")
    assert process_matches(
        _Proc("python", cmdline=["python", r"C:\Games\launcher.exe"]),
        "",
        "launcher.exe",
    )
    assert process_matches(_Proc("other", exe=r"C:\Emu\pcsx2-qt.exe"), r"C:\Emu\pcsx2-qt.exe", "")
    assert not process_matches(_Proc("other", exe=r"C:\Emu\other.exe"), r"C:\Emu\pcsx2-qt.exe", "")


def test_running_app_detection():
    procs = [_Proc("pcsx2-qt.exe", pid=4)]
    assert is_app_running(r"C:\Emu\pcsx2-qt.exe", "", processes=procs)
    assert not is_app_running(r"C:\Emu\other.exe", "", processes=procs)


def test_launch_environment_hides_the_sync_tool_folder(tmp_path, monkeypatch):
    tool = tmp_path / "GameSaveSync"
    internal = tool / "_internal"
    keep = tmp_path / "System32"
    tool.mkdir()
    internal.mkdir()
    keep.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tool / "GameSaveSync.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    nested = internal / "bin"
    nested.mkdir()
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(internal), str(nested), str(tool) + os.sep, str(keep)]),
    )
    parts = launch_environment()["PATH"].split(os.pathsep)
    assert str(keep) in parts
    assert str(internal) not in parts
    assert str(nested) not in parts
    assert not any("GameSaveSync" in part for part in parts)


def test_lock_blocks_a_second_sync_and_clears_a_dead_pid(tmp_path):
    with ProfileLock("PCSX2", tmp_path):
        with pytest.raises(SyncBusyError):
            with ProfileLock("PCSX2", tmp_path):
                pass
    dead = 4_000_000
    while psutil.pid_exists(dead):
        dead += 1
    lock = tmp_path / "PCSX2.lock"
    lock.write_text(str(dead), encoding="utf-8")
    with ProfileLock("PCSX2", tmp_path):
        assert True


def _script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_wait_for_child_process(tmp_path):
    game = tmp_path / "savsync-game"
    launcher = tmp_path / "savsync-launch"
    _script(game, "#!/bin/sh\nsleep 1\n")
    _script(launcher, f"#!/bin/sh\n{game} &\nexit 0\n")
    profile = Profile(name="Game", exe=str(launcher), wait_process="savsync-game", folders=[])
    started = time.monotonic()
    wait_for_app(profile, poll_interval=0.1, appear_timeout=10)
    assert time.monotonic() - started >= 0.8


def test_launch_pulls_then_pushes_after_the_game(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    local.mkdir()
    save = local / "slot.sav"
    now = time.time()
    save.write_bytes(b"old")
    os.utime(save, (now - 200, now - 200))
    cloud_file = cloud / "GameSaveSync" / "PCSX2" / "memcards" / "slot.sav"
    cloud_file.parent.mkdir(parents=True)
    cloud_file.write_bytes(b"from-cloud")
    os.utime(cloud_file, (now - 100, now - 100))
    game = tmp_path / "play-game"
    _script(
        game,
        "#!/bin/sh\n"
        f"printf '%s+played' \"$(cat '{save}')\" > '{save}'\n",
    )
    ConfigStore(home / "config.json").save(
        AppConfig(
            remote="gdrive",
            backup_retention=5,
            profiles=[
                Profile(
                    name="PCSX2",
                    exe=str(game),
                    folders=[SaveFolder("memcards", str(local))],
                )
            ],
        )
    )
    code = run_launch("PCSX2", play_anyway=lambda _message: False, status=lambda _message: None)
    assert code == 0
    assert save.read_bytes() == b"from-cloud+played"
    assert cloud_file.read_bytes() == b"from-cloud+played"
    state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert state["profiles"]["PCSX2"]["last_result"] == "ok"
    assert state["profiles"]["PCSX2"]["pending_push"] is False


def test_launch_can_be_cancelled_when_the_cloud_is_down(app_home, tmp_path):
    home, _cloud = app_home
    local = tmp_path / "saves"
    local.mkdir()
    marker = tmp_path / "started"
    game = tmp_path / "play-game"
    _script(game, f"#!/bin/sh\ntouch '{marker}'\n")
    config_path = home / "rclone.conf"
    config_path.write_text("[other]\ntype = alias\nremote = /tmp\n", encoding="utf-8")
    ConfigStore().save(
        AppConfig(
            remote="missing",
            profiles=[Profile(name="PCSX2", exe=str(game), folders=[SaveFolder("memcards", str(local))])],
        )
    )
    asked = []
    code = run_launch(
        "PCSX2",
        play_anyway=lambda message: asked.append(message) or False,
        status=lambda _message: None,
    )
    assert code == 1
    assert asked
    assert not marker.exists()
    status = StateStore().load().profiles["PCSX2"]
    assert status.last_result == "failed"
