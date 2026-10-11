import os
import shutil
import subprocess
from pathlib import Path

import pytest


def rclone_exe() -> str:
    env = os.environ.get("GAMESAVESYNC_RCLONE")
    if env and Path(env).is_file():
        return env
    found = shutil.which("rclone")
    if found:
        return found
    candidate = Path(__file__).resolve().parents[1] / ".tools" / "rclone"
    if candidate.is_file():
        return str(candidate)
    pytest.skip("rclone is not installed")


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "app"
    cloud = tmp_path / "cloud"
    home.mkdir()
    cloud.mkdir()
    exe = rclone_exe()
    monkeypatch.setenv("GAMESAVESYNC_HOME", str(home))
    monkeypatch.setenv("GAMESAVESYNC_RCLONE", exe)
    monkeypatch.setenv("GAMESAVESYNC_NO_GUI", "1")
    subprocess.run(
        [
            exe,
            "--config",
            str(home / "rclone.conf"),
            "config",
            "create",
            "gdrive",
            "alias",
            "remote",
            str(cloud),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return home, cloud
