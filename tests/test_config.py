import json

import pytest

from gamesavesync.config import AppConfig, ConfigError, ConfigStore, Profile, SaveFolder


def test_round_trip(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(
        remote="gdrive",
        backup_retention=4,
        profiles=[
            Profile(
                name="PCSX2",
                exe=r"C:\Emu\pcsx2-qt.exe",
                args="--fullscreen",
                wait_process="pcsx2-qt.exe",
                folders=[SaveFolder("memcards", r"C:\Emu\memcards", ["*.tmp"])],
            )
        ],
    )
    store.save(config)
    loaded = store.load()
    assert loaded == config


def test_missing_file_is_default(tmp_path):
    loaded = ConfigStore(tmp_path / "missing.json").load()
    assert loaded.remote == "gdrive"
    assert loaded.profiles == []


def test_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigStore(path).load()


def test_rejects_duplicate_profiles_and_bad_names(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(
        profiles=[
            Profile("A/B", "game.exe", folders=[SaveFolder("saves", str(tmp_path))]),
        ]
    )
    with pytest.raises(ConfigError):
        store.save(config)

    config.profiles[0].name = "Same"
    config.profiles.append(
        Profile("Same", "game.exe", folders=[SaveFolder("saves", str(tmp_path / "other"))])
    )
    with pytest.raises(ConfigError):
        store.save(config)


def test_remote_name_characters(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        store.save(AppConfig(remote="my drive"))


def test_load_checks_profiles(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"remote": "gdrive", "backup_retention": 10, "profiles": [{"name": "X"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        ConfigStore(path).load()
