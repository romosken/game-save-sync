"""Small helpers shared across the app."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

_INVALID_NAME = re.compile(r'[\\/:*?"<>|]')


def app_dir() -> Path:
    """Directory for config, tokens, logs, and sync status.

    On Windows this is ``%APPDATA%\\GameSaveSync``. Elsewhere it follows
    ``$XDG_CONFIG_HOME`` or ``~/.config``. ``GAMESAVESYNC_HOME`` overrides both.
    """
    override = os.environ.get("GAMESAVESYNC_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "GameSaveSync"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base_path = Path(xdg) if xdg else Path.home() / ".config"
    return base_path / "GameSaveSync"


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` and replace ``path`` only after the temp file is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_local_stamp(iso_value: str) -> str:
    local = parse_iso(iso_value).astimezone()
    return local.strftime("%m/%d %H:%M")


def validate_storage_name(name: str, kind: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError(f"{kind} name is required")
    if cleaned in {".", "..", ".backup"} or _INVALID_NAME.search(cleaned):
        raise ValueError(
            f"{kind} name {cleaned!r} cannot contain \\ / : * ? \" < > | "
            "or be named .backup"
        )
    return cleaned


def make_stamp() -> str:
    """Timestamp folder name that sorts in chronological order."""
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")
