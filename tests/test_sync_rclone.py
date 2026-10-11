import os
import stat
from pathlib import Path

from gamesavesync.config import AppConfig, Profile, SaveFolder
from gamesavesync.rclone import Rclone
from gamesavesync.state import StateStore
from gamesavesync.sync import pull_profile, push_profile, sync_profile_both

PROFILE = "PCSX2"
LABEL = "memcards"


def _write(path: Path, data: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def _profile(local: Path, *, retention: int = 10, exclude: list[str] | None = None) -> tuple[AppConfig, Profile]:
    profile = Profile(
        name=PROFILE,
        exe="/usr/bin/true",
        folders=[SaveFolder(LABEL, str(local), exclude or [])],
    )
    return AppConfig(remote="gdrive", backup_retention=retention, profiles=[profile]), profile


def _cloud_file(cloud: Path, rel: str) -> Path:
    return cloud / "GameSaveSync" / PROFILE / LABEL / rel


def test_first_sync_uploads_when_cloud_is_empty(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    _write(local / "slot1.sav", b"brand-new", 1_700_000_000)
    _write(local / "cards" / "slot2.sav", b"nested", 1_700_000_100)
    assert not (cloud / "GameSaveSync").exists()
    config, profile = _profile(local)
    store = StateStore(home / "state.json")
    rclone = Rclone()

    pull = pull_profile(profile, config, rclone, store, lock=False)
    assert pull.ok
    assert pull.folders[0].downloaded == 0
    assert (local / "slot1.sav").read_bytes() == b"brand-new"
    assert not (local / ".backup").exists()

    push = push_profile(profile, config, rclone, store, lock=False)
    assert push.ok
    assert push.folders[0].uploaded == 2
    assert push.folders[0].backed_up == 0
    assert _cloud_file(cloud, "slot1.sav").read_bytes() == b"brand-new"
    assert _cloud_file(cloud, "cards/slot2.sav").read_bytes() == b"nested"
    assert not (cloud / "GameSaveSync" / PROFILE / ".backup").exists()

    again = sync_profile_both(profile, config, rclone, store, lock=False)
    assert again.ok
    assert again.folders[0].downloaded == 0
    assert again.folders[0].uploaded == 0
    saved = store.load().profiles[PROFILE]
    assert saved.last_result == "ok"
    assert saved.pending_push is False


def test_missing_cloud_directory_does_not_fail_the_sync(tmp_path):
    """Google Drive reports a missing folder instead of an empty list."""
    fake = tmp_path / "rclone-missing"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "commands = {'lsjson', 'lsf', 'copy', 'copyto', 'move', 'moveto', 'hashsum', 'purge', 'listremotes', 'lsd'}\n"
        "cmd = next(arg for arg in sys.argv[1:] if arg in commands)\n"
        "if cmd in {'lsjson', 'lsf', 'purge'}:\n"
        "    sys.stderr.write('Failed to lsjson: directory not found\\n')\n"
        "    raise SystemExit(3)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    local = tmp_path / "saves"
    _write(local / "slot.sav", b"only-local", 1_700_000_000)
    config, profile = _profile(local)
    rclone = Rclone(fake, tmp_path / "rclone.conf")
    result = sync_profile_both(profile, config, rclone, lock=False)
    assert result.ok
    assert result.folders[0].downloaded == 0
    assert result.folders[0].uploaded == 1
    assert (local / "slot.sav").read_bytes() == b"only-local"


def test_pull_replaces_older_local_and_keeps_a_backup(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    _write(local / "slot1.sav", b"local-old", 1_700_000_000)
    _write(_cloud_file(cloud, "slot1.sav"), b"from-cloud", 1_800_000_000)
    _write(_cloud_file(cloud, "slot2.sav"), b"extra", 1_800_000_000)
    config, profile = _profile(local)
    result = pull_profile(profile, config, Rclone(), StateStore(home / "state.json"), lock=False)
    assert result.ok
    assert (local / "slot1.sav").read_bytes() == b"from-cloud"
    assert (local / "slot2.sav").read_bytes() == b"extra"
    backups = list((local / ".backup").rglob("slot1.sav"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"local-old"
    again = pull_profile(profile, config, Rclone(), lock=False)
    assert again.ok
    assert again.folders[0].downloaded == 0


def test_push_uploads_newer_local_and_backs_up_cloud(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    _write(local / "slot1.sav", b"local-new", 1_800_000_000)
    _write(local / "slot3.sav", b"only-local", 1_800_000_000)
    _write(_cloud_file(cloud, "slot1.sav"), b"cloud-old", 1_700_000_000)
    _write(_cloud_file(cloud, "keep-me.sav"), b"cloud-only", 1_800_000_000)
    config, profile = _profile(local)
    result = push_profile(profile, config, Rclone(), StateStore(home / "state.json"), lock=False)
    assert result.ok
    assert _cloud_file(cloud, "slot1.sav").read_bytes() == b"local-new"
    assert _cloud_file(cloud, "slot3.sav").read_bytes() == b"only-local"
    assert _cloud_file(cloud, "keep-me.sav").read_bytes() == b"cloud-only"
    cloud_backups = list((cloud / "GameSaveSync" / PROFILE / ".backup").rglob("slot1.sav"))
    assert len(cloud_backups) == 1
    assert cloud_backups[0].read_bytes() == b"cloud-old"
    listed = list((cloud / "GameSaveSync" / PROFILE / LABEL).rglob("*"))
    assert all(".backup" not in path.parts for path in listed)


def test_same_bytes_with_different_mtime_are_not_transferred(app_home, tmp_path):
    _home, cloud = app_home
    local = tmp_path / "saves"
    _write(local / "slot.sav", b"same-bytes", 1_700_000_000)
    _write(_cloud_file(cloud, "slot.sav"), b"same-bytes", 1_800_000_000)
    config, profile = _profile(local)
    pull = pull_profile(profile, config, Rclone(), lock=False)
    push = push_profile(profile, config, Rclone(), lock=False)
    assert pull.folders[0].downloaded == 0
    assert push.folders[0].uploaded == 0
    assert not (local / ".backup").exists()


def test_tie_pull_then_push_keeps_the_cloud_bytes(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    stamp = 1_800_000_000
    _write(local / "slot.sav", b"from-pc", stamp)
    _write(_cloud_file(cloud, "slot.sav"), b"from-drive", stamp)
    config, profile = _profile(local)
    result = sync_profile_both(profile, config, Rclone(), StateStore(home / "state.json"), lock=False)
    assert result.ok
    assert (local / "slot.sav").read_bytes() == b"from-drive"
    assert _cloud_file(cloud, "slot.sav").read_bytes() == b"from-drive"
    backups = list((local / ".backup").rglob("slot.sav"))
    assert backups and backups[0].read_bytes() == b"from-pc"


def test_exclude_patterns_and_backup_retention(app_home, tmp_path):
    home, cloud = app_home
    local = tmp_path / "saves"
    _write(local / "slot.sav", b"v0", 1_700_000_000)
    _write(local / "noise.tmp", b"ignore", 1_900_000_000)
    config, profile = _profile(local, retention=2, exclude=["*.tmp"])
    store = StateStore(home / "state.json")
    for version, mtime in ((b"v1", 1_800_000_100), (b"v2", 1_800_000_200), (b"v3", 1_800_000_300)):
        _write(_cloud_file(cloud, "slot.sav"), version, mtime)
        result = pull_profile(profile, config, Rclone(), store, lock=False)
        assert result.ok
    assert (local / "slot.sav").read_bytes() == b"v3"
    assert (local / "noise.tmp").read_bytes() == b"ignore"
    stamp_dirs = [path for path in (local / ".backup").iterdir() if path.is_dir()]
    assert len(stamp_dirs) == 2
    push = push_profile(profile, config, Rclone(), store, lock=False)
    assert push.ok
    assert not (cloud / "GameSaveSync" / PROFILE / LABEL / "noise.tmp").exists()


def test_running_app_is_skipped(app_home, tmp_path):
    home, _cloud = app_home
    local = tmp_path / "saves"
    local.mkdir()
    exe = str(local / "pcsx2-qt.exe")
    profile = Profile(name=PROFILE, exe=exe, folders=[SaveFolder(LABEL, str(local))])
    config = AppConfig(profiles=[profile])

    class _Proc:
        pid = 20
        info = {"name": "pcsx2-qt.exe", "exe": exe, "cmdline": [exe], "pid": 20}

    result = sync_profile_both(
        profile,
        config,
        Rclone(),
        StateStore(home / "state.json"),
        skip_if_running=True,
        lock=False,
        process_snapshot=[_Proc()],
    )
    assert result.result == "skipped"
    assert "running" in result.error
