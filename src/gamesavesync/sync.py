"""Compare save files and copy the newer side, keeping the older one in .backup."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psutil

from gamesavesync.config import AppConfig, Profile, SaveFolder
from gamesavesync.rclone import Rclone, RcloneError
from gamesavesync.state import FolderStatus, StateStore, record_result
from gamesavesync.util import app_dir, make_stamp

logger = logging.getLogger("gamesavesync")

SKEW_SECONDS = 2
BACKUP_DIR = ".backup"
REMOTE_ROOT = "GameSaveSync"
TEMP_SUFFIX = ".gamesavesync-tmp"
_STAMP_RE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")

Direction = Literal["pull", "push", "both"]
Side = Literal["same", "local", "cloud", "tie"]


class SyncBusyError(RuntimeError):
    """Another sync already holds this profile's lock."""


@dataclass(frozen=True)
class FileInfo:
    rel_path: str
    size: int
    mtime: datetime
    hash: str | None = None


@dataclass(frozen=True)
class SyncAction:
    op: Literal["download", "upload", "backup_local", "backup_cloud"]
    rel_path: str


@dataclass
class FolderSyncResult:
    label: str
    ok: bool
    error: str = ""
    downloaded: int = 0
    uploaded: int = 0
    backed_up: int = 0

    def status(self) -> FolderStatus:
        return FolderStatus(
            result="ok" if self.ok else "failed",
            error=self.error,
            downloaded=self.downloaded,
            uploaded=self.uploaded,
            backed_up=self.backed_up,
        )


@dataclass
class ProfileSyncResult:
    name: str
    direction: str
    result: str
    error: str = ""
    folders: list[FolderSyncResult] = field(default_factory=list)
    push_attempted: bool = False
    push_ok: bool = False

    @property
    def ok(self) -> bool:
        return self.result == "ok"


def content_differs(local: FileInfo, cloud: FileInfo) -> bool:
    if local.hash and cloud.hash:
        return local.hash.lower() != cloud.hash.lower()
    if local.size != cloud.size:
        return True
    return abs((local.mtime - cloud.mtime).total_seconds()) > SKEW_SECONDS


def classify(local: FileInfo, cloud: FileInfo) -> Side:
    """Decide which copy is newer.

    Identical contents are ``same`` even when the timestamps differ. When the
    contents differ and the timestamps are more than two seconds apart, the
    later file wins. A closer timestamp with different contents is a ``tie``:
    pull keeps the cloud file and push keeps the local file, and the copy that
    is replaced is moved into ``.backup``.
    """
    if not content_differs(local, cloud):
        return "same"
    delta = (local.mtime - cloud.mtime).total_seconds()
    if delta > SKEW_SECONDS:
        return "local"
    if delta < -SKEW_SECONDS:
        return "cloud"
    return "tie"


def plan_pull(local: dict[str, FileInfo], cloud: dict[str, FileInfo]) -> list[SyncAction]:
    actions: list[SyncAction] = []
    for rel in sorted(cloud):
        if rel not in local:
            actions.append(SyncAction("download", rel))
            continue
        side = classify(local[rel], cloud[rel])
        if side in ("cloud", "tie"):
            actions.append(SyncAction("backup_local", rel))
            actions.append(SyncAction("download", rel))
    return actions


def plan_push(local: dict[str, FileInfo], cloud: dict[str, FileInfo]) -> list[SyncAction]:
    actions: list[SyncAction] = []
    for rel in sorted(local):
        if rel not in cloud:
            actions.append(SyncAction("upload", rel))
            continue
        side = classify(local[rel], cloud[rel])
        if side in ("local", "tie"):
            actions.append(SyncAction("backup_cloud", rel))
            actions.append(SyncAction("upload", rel))
    return actions


def is_excluded(rel_path: str, patterns: list[str]) -> bool:
    rel = rel_path.replace("\\", "/").lstrip("/")
    if not rel or rel.endswith(TEMP_SUFFIX):
        return True
    parts = rel.split("/")
    if BACKUP_DIR in parts:
        return True
    name = parts[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def filter_files(files: dict[str, FileInfo], patterns: list[str]) -> dict[str, FileInfo]:
    return {rel: info for rel, info in files.items() if not is_excluded(rel, patterns)}


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_local(root: Path, patterns: list[str]) -> dict[str, FileInfo]:
    """List save files. File contents are hashed later, and only when needed."""
    files: dict[str, FileInfo] = {}
    if not root.exists():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != BACKUP_DIR]
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if is_excluded(rel, patterns):
                continue
            stat = full.stat()
            files[rel] = FileInfo(
                rel_path=rel,
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            )
    return files


def ambiguous_paths(local: dict[str, FileInfo], cloud: dict[str, FileInfo]) -> list[str]:
    """Files that share a size but not a timestamp still need a content check."""
    paths: list[str] = []
    for rel, local_info in local.items():
        cloud_info = cloud.get(rel)
        if cloud_info is None or local_info.hash or cloud_info.hash:
            continue
        if local_info.size != cloud_info.size:
            continue
        delta = abs((local_info.mtime - cloud_info.mtime).total_seconds())
        if delta > SKEW_SECONDS:
            paths.append(rel)
    return sorted(paths)


def with_hashes(files: dict[str, FileInfo], hashes: dict[str, str]) -> dict[str, FileInfo]:
    if not hashes:
        return files
    updated: dict[str, FileInfo] = {}
    for rel, info in files.items():
        digest = hashes.get(rel)
        if not digest:
            updated[rel] = info
            continue
        updated[rel] = FileInfo(info.rel_path, info.size, info.mtime, digest.lower())
    return updated


def confirm_same_content(
    rclone: Rclone,
    *,
    local_dir: Path,
    remote_dir: str,
    local_files: dict[str, FileInfo],
    cloud_files: dict[str, FileInfo],
) -> tuple[dict[str, FileInfo], dict[str, FileInfo]]:
    paths = ambiguous_paths(local_files, cloud_files)
    if not paths:
        return local_files, cloud_files
    local_hashes = {rel: file_md5(local_child(local_dir, rel)) for rel in paths}
    cloud_hashes = rclone.md5_files(remote_dir, paths)
    return with_hashes(local_files, local_hashes), with_hashes(cloud_files, cloud_hashes)


def parse_rclone_time(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, frac = text.split(".", 1)
        tz = ""
        for index, char in enumerate(frac):
            if char in "+-":
                tz = frac[index:]
                frac = frac[:index]
                break
        text = f"{head}.{(frac + '000000')[:6]}{tz}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def files_from_lsjson(entries: list[dict]) -> dict[str, FileInfo]:
    files: dict[str, FileInfo] = {}
    for entry in entries:
        if entry.get("IsDir"):
            continue
        rel = str(entry.get("Path") or entry.get("Name") or "").replace("\\", "/")
        if not rel:
            continue
        hashes = entry.get("Hashes") or {}
        digest = None
        if isinstance(hashes, dict):
            # Only MD5 can be compared with the hash we compute locally.
            digest = hashes.get("md5") or hashes.get("MD5")
        files[rel] = FileInfo(
            rel_path=rel,
            size=int(entry.get("Size") or 0),
            mtime=parse_rclone_time(str(entry["ModTime"])),
            hash=str(digest) if digest else None,
        )
    return files


def prune_names(names: list[str], keep: int) -> list[str]:
    stamps = sorted(name for name in names if _STAMP_RE.match(name))
    if keep <= 0:
        return stamps
    if len(stamps) <= keep:
        return []
    return stamps[: len(stamps) - keep]


def remote_folder(remote: str, profile: str, label: str) -> str:
    return f"{remote}:{REMOTE_ROOT}/{profile}/{label}"


def remote_backup_root(remote: str, profile: str) -> str:
    return f"{remote}:{REMOTE_ROOT}/{profile}/{BACKUP_DIR}"


def local_child(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


def _copy_to_backup(src: Path, root: Path, rel: str, stamp: str) -> Path:
    dest = root / BACKUP_DIR / stamp / Path(*rel.split("/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _span(on_progress, start: float, end: float):
    def emit(percent: float) -> None:
        if on_progress is not None:
            on_progress(start + (end - start) * (percent / 100.0))

    return emit


def _download_files(
    rclone: Rclone,
    *,
    local_dir: Path,
    remote_dir: str,
    relative_paths: list[str],
    backup_paths: set[str],
    stamp: str,
    on_progress=None,
) -> tuple[int, int]:
    backed = 0
    for rel in relative_paths:
        if rel not in backup_paths:
            continue
        src = local_child(local_dir, rel)
        if src.is_file():
            _copy_to_backup(src, local_dir, rel, stamp)
            backed += 1
    if relative_paths:
        logger.info("download %s files from %s", len(relative_paths), remote_dir)
        if on_progress is not None:
            on_progress(8)
        rclone.copy_files(
            remote_dir,
            str(local_dir),
            relative_paths,
            on_progress=_span(on_progress, 8, 100),
        )
        if on_progress is not None:
            on_progress(100)
    return len(relative_paths), backed


def _upload_files(
    rclone: Rclone,
    *,
    local_dir: Path,
    remote_dir: str,
    backup_dir: str,
    relative_paths: list[str],
    backup_paths: set[str],
    on_progress=None,
) -> tuple[int, int]:
    backups = [rel for rel in relative_paths if rel in backup_paths]
    copy_start = 8.0
    if backups:
        logger.info("backup %s cloud files to %s", len(backups), backup_dir)
        if on_progress is not None:
            on_progress(5)
        rclone.move_files(remote_dir, backup_dir, backups, on_progress=_span(on_progress, 5, 25))
        copy_start = 25.0
    try:
        if relative_paths:
            logger.info("upload %s files to %s", len(relative_paths), remote_dir)
            if on_progress is not None:
                on_progress(copy_start)
            rclone.copy_files(
                str(local_dir),
                remote_dir,
                relative_paths,
                on_progress=_span(on_progress, copy_start, 100),
            )
            if on_progress is not None:
                on_progress(100)
    except Exception:
        if backups:
            try:
                rclone.move_files(backup_dir, remote_dir, backups)
            except RcloneError:
                logger.exception("Could not restore cloud files in %s", remote_dir)
        raise
    return len(relative_paths), len(backups)


def prune_local_backups(root: Path, keep: int) -> None:
    backup_root = root / BACKUP_DIR
    if not backup_root.is_dir():
        return
    names = [path.name for path in backup_root.iterdir() if path.is_dir()]
    for name in prune_names(names, keep):
        shutil.rmtree(backup_root / name, ignore_errors=True)


def prune_cloud_backups(rclone: Rclone, remote: str, profile: str, keep: int) -> None:
    root = remote_backup_root(remote, profile)
    for name in prune_names(rclone.lsf_dirs(root), keep):
        rclone.purge(f"{root}/{name}")


def short_error(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        location = exc.filename or str(exc)
        return _truncate(f"Access denied: {location}")
    raw = str(exc).strip() or exc.__class__.__name__
    lowered = raw.lower()
    network_markers = (
        "cloud unreachable",
        "timed out",
        "timeout",
        "connection refused",
        "connection reset",
        "network is unreachable",
        "no such host",
        "temporary failure",
        "tls handshake",
        "dial tcp",
        "i/o timeout",
        "context deadline exceeded",
    )
    if any(marker in lowered for marker in network_markers):
        return "Cloud unreachable"
    if "access is denied" in lowered or "permission denied" in lowered:
        return _truncate(f"Access denied: {raw}")
    return _truncate(raw)


def _truncate(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def sync_folder(
    rclone: Rclone,
    *,
    local_dir: Path,
    remote: str,
    profile: str,
    folder: SaveFolder,
    direction: Literal["pull", "push"],
    stamp: str,
    retention: int,
    on_progress=None,
) -> FolderSyncResult:
    label = folder.label
    downloaded = uploaded = backed = 0
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
        local_files = scan_local(local_dir, folder.exclude)
        folder_remote = remote_folder(remote, profile, label)
        cloud_files = filter_files(
            files_from_lsjson(rclone.lsjson(folder_remote)),
            folder.exclude,
        )
        local_files, cloud_files = confirm_same_content(
            rclone,
            local_dir=local_dir,
            remote_dir=folder_remote,
            local_files=local_files,
            cloud_files=cloud_files,
        )
        if direction == "pull":
            actions = plan_pull(local_files, cloud_files)
            backup_rels = {action.rel_path for action in actions if action.op == "backup_local"}
            downloads = [action.rel_path for action in actions if action.op == "download"]
            downloaded, backed = _download_files(
                rclone,
                local_dir=local_dir,
                remote_dir=folder_remote,
                relative_paths=downloads,
                backup_paths=backup_rels,
                stamp=stamp,
                on_progress=on_progress,
            )
            if backed:
                prune_local_backups(local_dir, retention)
        else:
            actions = plan_push(local_files, cloud_files)
            backup_rels = {action.rel_path for action in actions if action.op == "backup_cloud"}
            uploads = [action.rel_path for action in actions if action.op == "upload"]
            backup_dir = f"{remote_backup_root(remote, profile)}/{stamp}/{label}"
            uploaded, backed = _upload_files(
                rclone,
                local_dir=local_dir,
                remote_dir=folder_remote,
                backup_dir=backup_dir,
                relative_paths=uploads,
                backup_paths=backup_rels,
                on_progress=on_progress,
            )
        return FolderSyncResult(
            label=label,
            ok=True,
            downloaded=downloaded,
            uploaded=uploaded,
            backed_up=backed,
        )
    except Exception as exc:
        logger.exception("sync folder %s/%s failed", profile, label)
        return FolderSyncResult(
            label=label,
            ok=False,
            error=short_error(exc),
            downloaded=downloaded,
            uploaded=uploaded,
            backed_up=backed,
        )


def _folder_map(results: list[FolderSyncResult]) -> dict[str, FolderStatus]:
    return {item.label: item.status() for item in results}


def _merge_folders(pulls: list[FolderSyncResult], pushes: list[FolderSyncResult]) -> list[FolderSyncResult]:
    merged: list[FolderSyncResult] = []
    push_by_label = {item.label: item for item in pushes}
    push_failed = any(not item.ok for item in pushes)
    for pull in pulls:
        push = push_by_label.get(pull.label)
        if push is None:
            if push_failed:
                merged.append(
                    FolderSyncResult(
                        label=pull.label,
                        ok=False,
                        error="Not uploaded because an earlier folder failed",
                        downloaded=pull.downloaded,
                        backed_up=pull.backed_up,
                    )
                )
            else:
                merged.append(pull)
            continue
        ok = pull.ok and push.ok
        error = push.error or pull.error
        merged.append(
            FolderSyncResult(
                label=pull.label,
                ok=ok,
                error="" if ok else error,
                downloaded=pull.downloaded + push.downloaded,
                uploaded=pull.uploaded + push.uploaded,
                backed_up=pull.backed_up + push.backed_up,
            )
        )
    return merged


def _sync_direction(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    direction: Literal["pull", "push"],
    stamp: str,
    on_progress=None,
) -> list[FolderSyncResult]:
    results: list[FolderSyncResult] = []
    verb = "Downloading" if direction == "pull" else "Uploading"
    total = max(len(profile.folders), 1)
    for index, folder in enumerate(profile.folders):
        def folder_progress(percent: float, folder_index: int = index, label: str = folder.label) -> None:
            if on_progress is None:
                return
            overall = (folder_index + percent / 100.0) / total * 100.0
            on_progress(overall, f"{verb} {profile.name} / {label}  {percent:.0f}%")

        if on_progress is not None:
            on_progress(index / total * 100.0, f"Checking {profile.name} / {folder.label}")
        result = sync_folder(
            rclone,
            local_dir=Path(folder.path),
            remote=config.remote,
            profile=profile.name,
            folder=folder,
            direction=direction,
            stamp=stamp,
            retention=config.backup_retention,
            on_progress=folder_progress,
        )
        results.append(result)
        if not result.ok:
            break
    if direction == "push" and all(item.ok for item in results):
        prune_cloud_backups(rclone, config.remote, profile.name, config.backup_retention)
    return results


def _store_outcome(
    store: StateStore | None,
    outcome: ProfileSyncResult,
    *,
    keep_previous_folders: bool = False,
) -> ProfileSyncResult:
    if store is not None and outcome.result != "":
        record_result(
            store,
            name=outcome.name,
            direction=outcome.direction,
            result=outcome.result,
            error=outcome.error,
            folders=_folder_map(outcome.folders),
            push_attempted=outcome.push_attempted,
            push_ok=outcome.push_ok,
            keep_previous_folders=keep_previous_folders or outcome.result == "skipped",
        )
    return outcome


def sync_profile(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None,
    *,
    direction: Direction,
    skip_if_running: bool = False,
    process_snapshot: list | None = None,
    lock: bool = True,
    on_progress=None,
) -> ProfileSyncResult:
    if skip_if_running and is_app_running(
        profile.exe, profile.wait_process, processes=process_snapshot
    ):
        outcome = ProfileSyncResult(
            name=profile.name,
            direction=direction,
            result="skipped",
            error="Skipped: app running",
        )
        return _store_outcome(store, outcome)

    if lock:
        try:
            with ProfileLock(profile.name):
                return _sync_profile_unlocked(
                    profile, config, rclone, store, direction=direction, on_progress=on_progress
                )
        except SyncBusyError as exc:
            outcome = ProfileSyncResult(
                name=profile.name,
                direction=direction,
                result="failed",
                error=str(exc),
            )
            return _store_outcome(store, outcome)
    return _sync_profile_unlocked(
        profile, config, rclone, store, direction=direction, on_progress=on_progress
    )


def _sync_profile_unlocked(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None,
    *,
    direction: Direction,
    on_progress=None,
) -> ProfileSyncResult:
    stamp = make_stamp()
    push_attempted = False
    try:
        if direction == "pull":
            folders = _sync_direction(
                profile, config, rclone, "pull", stamp, on_progress=on_progress
            )
            failed = next((item for item in folders if not item.ok), None)
            outcome = ProfileSyncResult(
                name=profile.name,
                direction="pull",
                result="failed" if failed else "ok",
                error=failed.error if failed else "",
                folders=folders,
            )
        elif direction == "push":
            push_attempted = True
            folders = _sync_direction(
                profile, config, rclone, "push", stamp, on_progress=on_progress
            )
            failed = next((item for item in folders if not item.ok), None)
            outcome = ProfileSyncResult(
                name=profile.name,
                direction="push",
                result="failed" if failed else "ok",
                error=failed.error if failed else "",
                folders=folders,
                push_attempted=True,
                push_ok=failed is None,
            )
        else:
            def pull_progress(percent: float, message: str) -> None:
                if on_progress is not None:
                    on_progress(percent * 0.5, message)

            def push_progress(percent: float, message: str) -> None:
                if on_progress is not None:
                    on_progress(50 + percent * 0.5, message)

            pulls = _sync_direction(
                profile, config, rclone, "pull", stamp, on_progress=pull_progress
            )
            pull_failed = next((item for item in pulls if not item.ok), None)
            if pull_failed:
                outcome = ProfileSyncResult(
                    name=profile.name,
                    direction="both",
                    result="failed",
                    error=pull_failed.error,
                    folders=pulls,
                )
            else:
                push_attempted = True
                pushes = _sync_direction(
                    profile, config, rclone, "push", stamp, on_progress=push_progress
                )
                push_failed = next((item for item in pushes if not item.ok), None)
                outcome = ProfileSyncResult(
                    name=profile.name,
                    direction="both",
                    result="failed" if push_failed else "ok",
                    error=push_failed.error if push_failed else "",
                    folders=_merge_folders(pulls, pushes),
                    push_attempted=True,
                    push_ok=push_failed is None,
                )
    except Exception as exc:
        logger.exception("sync profile %s failed", profile.name)
        outcome = ProfileSyncResult(
            name=profile.name,
            direction=direction,
            result="failed",
            error=short_error(exc),
            push_attempted=push_attempted,
            push_ok=False,
        )
    logger.info(
        "profile %s direction=%s result=%s error=%s",
        profile.name,
        outcome.direction,
        outcome.result,
        outcome.error,
    )
    return _store_outcome(store, outcome)


class ProfileLock:
    def __init__(self, profile_name: str, directory: Path | None = None) -> None:
        folder = directory or (app_dir() / "locks")
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in profile_name).strip("._")
        self.profile_name = profile_name
        self.path = folder / f"{safe or 'profile'}.lock"
        self._held = False

    def __enter__(self) -> ProfileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _lock_is_stale(self.path):
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise SyncBusyError(f"A sync is already running for {self.profile_name}")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self._held = True
            return self
        raise SyncBusyError(f"A sync is already running for {self.profile_name}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._held = False


def _lock_is_stale(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return not psutil.pid_exists(pid)


def _proc_name(proc) -> str:
    info = getattr(proc, "info", None)
    if isinstance(info, dict) and info.get("name"):
        return str(info["name"])
    try:
        return str(proc.name())
    except (psutil.Error, OSError):
        return ""


def _proc_exe(proc) -> str:
    info = getattr(proc, "info", None)
    if isinstance(info, dict) and "exe" in info:
        return str(info.get("exe") or "")
    try:
        return str(proc.exe())
    except (psutil.Error, OSError, AttributeError):
        return ""


def _proc_cmdline(proc) -> list[str]:
    info = getattr(proc, "info", None)
    if isinstance(info, dict) and isinstance(info.get("cmdline"), list):
        return [str(part) for part in info["cmdline"]]
    try:
        return [str(part) for part in proc.cmdline()]
    except (psutil.Error, OSError):
        return []


def _basename(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.split("/")[-1]


def process_matches(proc, exe: str, wait_process: str) -> bool:
    target_name = _basename(wait_process or exe).lower()
    if not target_name:
        return False
    name = _proc_name(proc).lower()
    if name == target_name:
        return True
    for part in _proc_cmdline(proc):
        if _basename(part).lower() == target_name:
            return True
    if exe:
        pexe = _proc_exe(proc)
        if pexe and os.path.normcase(os.path.abspath(pexe)) == os.path.normcase(os.path.abspath(exe)):
            return True
    return False


def is_app_running(exe: str, wait_process: str = "", processes=None) -> bool:
    if processes is None:
        processes = list(psutil.process_iter())
    else:
        processes = list(processes)
    if wait_process and any(process_matches(proc, "", wait_process) for proc in processes):
        return True
    if exe and any(process_matches(proc, exe, "") for proc in processes):
        return True
    return False


def matching_pids(wait_process: str, processes=None) -> set[int]:
    if processes is None:
        processes = psutil.process_iter()
    found: set[int] = set()
    for proc in processes:
        if not process_matches(proc, "", wait_process):
            continue
        pid = getattr(proc, "pid", None)
        if pid is None:
            info = getattr(proc, "info", None)
            if isinstance(info, dict):
                pid = info.get("pid")
        if isinstance(pid, int):
            found.add(pid)
    return found


def pull_profile(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None = None,
    *,
    lock: bool = True,
    on_progress=None,
) -> ProfileSyncResult:
    return sync_profile(
        profile, config, rclone, store, direction="pull", lock=lock, on_progress=on_progress
    )


def push_profile(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None = None,
    *,
    lock: bool = True,
    on_progress=None,
) -> ProfileSyncResult:
    return sync_profile(
        profile, config, rclone, store, direction="push", lock=lock, on_progress=on_progress
    )


def sync_profile_both(
    profile: Profile,
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None = None,
    *,
    skip_if_running: bool = False,
    lock: bool = True,
    process_snapshot=None,
    on_progress=None,
) -> ProfileSyncResult:
    return sync_profile(
        profile,
        config,
        rclone,
        store,
        direction="both",
        skip_if_running=skip_if_running,
        lock=lock,
        process_snapshot=process_snapshot,
        on_progress=on_progress,
    )


def sync_all(
    config: AppConfig,
    rclone: Rclone,
    store: StateStore | None = None,
    *,
    on_progress=None,
    skip_if_running: bool = True,
) -> list[ProfileSyncResult]:
    """Pull then push every profile. One failure does not stop the rest."""
    results: list[ProfileSyncResult] = []
    total = max(len(config.profiles), 1)
    for index, profile in enumerate(config.profiles, start=1):
        def profile_progress(percent: float, message: str, profile_index: int = index) -> None:
            if on_progress is None:
                return
            overall = ((profile_index - 1) + percent / 100.0) / total * 100.0
            on_progress(overall, message)

        try:
            with ProfileLock(profile.name):
                outcome = sync_profile_both(
                    profile,
                    config,
                    rclone,
                    store,
                    skip_if_running=skip_if_running,
                    lock=False,
                    on_progress=profile_progress,
                )
        except SyncBusyError as exc:
            outcome = ProfileSyncResult(
                name=profile.name,
                direction="both",
                result="skipped",
                error=str(exc),
            )
            _store_outcome(store, outcome)
        results.append(outcome)
    return results

