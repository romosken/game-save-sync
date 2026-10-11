"""Profile configuration stored in config.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from gamesavesync.util import app_dir, atomic_write, validate_storage_name


class ConfigError(ValueError):
    """The config file is missing a required value or is not valid JSON."""


@dataclass
class SaveFolder:
    label: str
    path: str
    exclude: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"label": self.label, "path": self.path, "exclude": list(self.exclude)}

    @classmethod
    def from_dict(cls, data: dict) -> SaveFolder:
        if not isinstance(data, dict):
            raise ConfigError("Each save folder must be an object")
        exclude = data.get("exclude") or []
        if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
            raise ConfigError("Folder exclude must be a list of patterns")
        return cls(
            label=str(data.get("label", "")),
            path=str(data.get("path", "")),
            exclude=[item.strip() for item in exclude if item.strip()],
        )


@dataclass
class Profile:
    name: str
    exe: str
    args: str = ""
    wait_process: str = ""
    folders: list[SaveFolder] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "exe": self.exe,
            "args": self.args,
            "wait_process": self.wait_process,
            "folders": [folder.to_dict() for folder in self.folders],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        if not isinstance(data, dict):
            raise ConfigError("Each profile must be an object")
        folders = data.get("folders") or []
        if not isinstance(folders, list):
            raise ConfigError("Profile folders must be a list")
        return cls(
            name=str(data.get("name", "")),
            exe=str(data.get("exe", "")),
            args=str(data.get("args", "") or ""),
            wait_process=str(data.get("wait_process", "") or ""),
            folders=[SaveFolder.from_dict(item) for item in folders],
        )


@dataclass
class AppConfig:
    remote: str = "gdrive"
    backup_retention: int = 10
    drive_client_id: str = ""
    drive_client_secret: str = ""
    profiles: list[Profile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "remote": self.remote,
            "backup_retention": self.backup_retention,
            "drive_client_id": self.drive_client_id,
            "drive_client_secret": self.drive_client_secret,
            "profiles": [profile.to_dict() for profile in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict) -> AppConfig:
        if not isinstance(data, dict):
            raise ConfigError("Config root must be an object")
        profiles = data.get("profiles") or []
        if not isinstance(profiles, list):
            raise ConfigError("profiles must be a list")
        retention = data.get("backup_retention", 10)
        if isinstance(retention, bool) or not isinstance(retention, int):
            raise ConfigError("backup_retention must be an integer")
        return cls(
            remote=str(data.get("remote") or "gdrive"),
            backup_retention=retention,
            drive_client_id=str(data.get("drive_client_id") or ""),
            drive_client_secret=str(data.get("drive_client_secret") or ""),
            profiles=[Profile.from_dict(item) for item in profiles],
        )

    def get_profile(self, name: str) -> Profile:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        raise ConfigError(f"No profile named {name!r}")

    def validate(self) -> None:
        remote = self.remote.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", remote):
            raise ConfigError(
                "Remote name must start with a letter and use only letters, numbers, _ and -"
            )
        self.remote = remote
        self.drive_client_id = (self.drive_client_id or "").strip()
        self.drive_client_secret = (self.drive_client_secret or "").strip()
        if self.backup_retention < 0:
            raise ConfigError("backup_retention must be zero or greater")
        seen: set[str] = set()
        for profile in self.profiles:
            try:
                profile.name = validate_storage_name(profile.name, "Profile")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            if profile.name in seen:
                raise ConfigError(f"Duplicate profile name {profile.name!r}")
            seen.add(profile.name)
            if not profile.exe.strip():
                raise ConfigError(f"Profile {profile.name!r} needs an application path")
            profile.exe = profile.exe.strip()
            profile.args = profile.args or ""
            profile.wait_process = (profile.wait_process or "").strip()
            if not profile.folders:
                raise ConfigError(f"Profile {profile.name!r} needs at least one save folder")
            labels: set[str] = set()
            paths: set[str] = set()
            for folder in profile.folders:
                try:
                    folder.label = validate_storage_name(folder.label, "Folder")
                except ValueError as exc:
                    raise ConfigError(str(exc)) from exc
                if folder.label in labels:
                    raise ConfigError(
                        f"Profile {profile.name!r} has two folders named {folder.label!r}"
                    )
                labels.add(folder.label)
                folder.path = folder.path.strip()
                if not folder.path:
                    raise ConfigError(
                        f"Profile {profile.name!r} folder {folder.label!r} needs a path"
                    )
                normalized = os.path.normcase(os.path.abspath(folder.path))
                if normalized in paths:
                    raise ConfigError(
                        f"Profile {profile.name!r} lists {folder.path} more than once"
                    )
                paths.add(normalized)


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_dir() / "config.json")

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Could not read {self.path}: {exc}") from exc
        config = AppConfig.from_dict(raw)
        config.validate()
        return config

    def save(self, config: AppConfig) -> None:
        config.validate()
        text = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
        atomic_write(self.path, text)
