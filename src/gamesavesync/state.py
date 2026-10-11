"""Last-sync status for each profile, stored in state.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from gamesavesync.util import app_dir, atomic_write, format_local_stamp, iso_now


@dataclass
class FolderStatus:
    result: str = "ok"
    error: str = ""
    downloaded: int = 0
    uploaded: int = 0
    backed_up: int = 0

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "error": self.error,
            "downloaded": self.downloaded,
            "uploaded": self.uploaded,
            "backed_up": self.backed_up,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FolderStatus:
        return cls(
            result=str(data.get("result") or "ok"),
            error=str(data.get("error") or ""),
            downloaded=int(data.get("downloaded") or 0),
            uploaded=int(data.get("uploaded") or 0),
            backed_up=int(data.get("backed_up") or 0),
        )


@dataclass
class ProfileStatus:
    last_sync_at: str | None = None
    last_direction: str = ""
    last_result: str = ""
    last_error: str = ""
    pending_push: bool = False
    folders: dict[str, FolderStatus] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "last_sync_at": self.last_sync_at,
            "last_direction": self.last_direction,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "pending_push": self.pending_push,
            "folders": {label: status.to_dict() for label, status in self.folders.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProfileStatus:
        folders = data.get("folders") or {}
        if not isinstance(folders, dict):
            folders = {}
        return cls(
            last_sync_at=data.get("last_sync_at") or None,
            last_direction=str(data.get("last_direction") or ""),
            last_result=str(data.get("last_result") or ""),
            last_error=str(data.get("last_error") or ""),
            pending_push=bool(data.get("pending_push")),
            folders={
                str(label): FolderStatus.from_dict(value)
                for label, value in folders.items()
                if isinstance(value, dict)
            },
        )


@dataclass
class AppState:
    profiles: dict[str, ProfileStatus] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"profiles": {name: status.to_dict() for name, status in self.profiles.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> AppState:
        raw = data.get("profiles") or {}
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            profiles={
                str(name): ProfileStatus.from_dict(value)
                for name, value in raw.items()
                if isinstance(value, dict)
            }
        )


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_dir() / "state.json")

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return AppState()
        if not isinstance(raw, dict):
            return AppState()
        return AppState.from_dict(raw)

    def save(self, state: AppState) -> None:
        text = json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n"
        atomic_write(self.path, text)


def status_text(status: ProfileStatus | None) -> str:
    """Short label for the profile list."""
    if status is None or not status.last_result:
        if status and status.pending_push:
            return "Pending upload"
        return "Never synced"
    if status.last_result == "failed":
        direction = status.last_direction or "sync"
        return f"FAILED - {direction}"
    if status.last_result == "skipped":
        if "already running" in status.last_error.lower():
            return "Skipped: sync in progress"
        return "Skipped: app running"
    if status.pending_push:
        return "Pending upload"
    if status.last_sync_at:
        try:
            return f"OK - {format_local_stamp(status.last_sync_at)}"
        except ValueError:
            return "OK"
    return "OK"


def needs_attention(status: ProfileStatus | None) -> bool:
    if status is None:
        return False
    return status.last_result == "failed" or status.pending_push


def attention_names(profile_names: list[str], state: AppState) -> list[str]:
    return [name for name in profile_names if needs_attention(state.profiles.get(name))]


def forget_profile(store: StateStore, name: str) -> None:
    state = store.load()
    if name in state.profiles:
        del state.profiles[name]
        store.save(state)


def rename_profile(store: StateStore, old_name: str, new_name: str) -> None:
    if old_name == new_name:
        return
    state = store.load()
    if old_name in state.profiles:
        state.profiles[new_name] = state.profiles.pop(old_name)
        store.save(state)


def record_result(
    store: StateStore,
    *,
    name: str,
    direction: str,
    result: str,
    error: str = "",
    folders: dict[str, FolderStatus] | None = None,
    push_attempted: bool = False,
    push_ok: bool = False,
    keep_previous_folders: bool = False,
) -> ProfileStatus:
    """Update one profile and return the status that was written.

    ``pending_push`` stays set until a push succeeds. A failed push sets it.
    A skipped run leaves the previous pending flag and folder details in place.
    """
    state = store.load()
    previous = state.profiles.get(name, ProfileStatus())
    pending = previous.pending_push
    if push_attempted:
        pending = not push_ok
    written_folders = previous.folders if keep_previous_folders else (folders or {})
    if result == "skipped":
        # Keep an existing failure or pending upload visible on the attention banner.
        keep_previous = previous.last_result == "failed" or previous.pending_push
        updated = ProfileStatus(
            last_sync_at=previous.last_sync_at,
            last_direction=previous.last_direction,
            last_result=previous.last_result if keep_previous else "skipped",
            last_error=previous.last_error if keep_previous else (error or "Skipped: app running"),
            pending_push=pending,
            folders=previous.folders,
        )
    else:
        updated = ProfileStatus(
            last_sync_at=iso_now(),
            last_direction=direction,
            last_result=result,
            last_error=error,
            pending_push=pending,
            folders=written_folders,
        )
    state.profiles[name] = updated
    store.save(state)
    return updated
