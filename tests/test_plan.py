from datetime import datetime, timedelta, timezone
from pathlib import Path

from gamesavesync.sync import (
    FileInfo,
    classify,
    files_from_lsjson,
    is_excluded,
    parse_rclone_time,
    plan_pull,
    plan_push,
    prune_names,
    scan_local,
    short_error,
)

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)


def info(name: str, content: str, minutes: int, size: int | None = None) -> FileInfo:
    payload = content.encode()
    return FileInfo(
        rel_path=name,
        size=size if size is not None else len(payload),
        mtime=NOW + timedelta(minutes=minutes),
        hash=str(abs(hash(content))),
    )


def test_pull_downloads_cloud_only_and_newer_cloud_copy():
    local = {"slot1.sav": info("slot1.sav", "old", 0)}
    cloud = {
        "slot1.sav": info("slot1.sav", "new", 10),
        "slot2.sav": info("slot2.sav", "only-cloud", 10),
    }
    actions = [(item.op, item.rel_path) for item in plan_pull(local, cloud)]
    assert actions == [
        ("backup_local", "slot1.sav"),
        ("download", "slot1.sav"),
        ("download", "slot2.sav"),
    ]
    assert plan_push(local, cloud) == []


def test_push_uploads_local_only_and_newer_local_copy():
    local = {
        "slot1.sav": info("slot1.sav", "new", 10),
        "slot3.sav": info("slot3.sav", "only-local", 10),
    }
    cloud = {"slot1.sav": info("slot1.sav", "old", 0)}
    actions = [(item.op, item.rel_path) for item in plan_push(local, cloud)]
    assert actions == [
        ("backup_cloud", "slot1.sav"),
        ("upload", "slot1.sav"),
        ("upload", "slot3.sav"),
    ]
    assert plan_pull(local, cloud) == []


def test_same_contents_are_left_alone_even_if_mtime_differs():
    local = {"slot.sav": info("slot.sav", "same", 0)}
    cloud = {"slot.sav": info("slot.sav", "same", 50)}
    assert classify(local["slot.sav"], cloud["slot.sav"]) == "same"
    assert plan_pull(local, cloud) == []
    assert plan_push(local, cloud) == []


def test_tie_resolves_toward_the_side_being_applied():
    local = {"slot.sav": info("slot.sav", "local", 0)}
    cloud = {"slot.sav": info("slot.sav", "cloud", 0)}
    assert classify(local["slot.sav"], cloud["slot.sav"]) == "tie"
    assert [item.op for item in plan_pull(local, cloud)] == ["backup_local", "download"]
    assert [item.op for item in plan_push(local, cloud)] == ["backup_cloud", "upload"]


def test_size_fallback_when_hash_missing():
    local = FileInfo("a.sav", 4, NOW, hash=None)
    cloud = FileInfo("a.sav", 8, NOW + timedelta(minutes=5), hash=None)
    assert classify(local, cloud) == "cloud"


def test_excludes_backups_temp_files_and_patterns():
    assert is_excluded(".backup/20260101-000000-000000/slot.sav", [])
    assert is_excluded("nested/.backup/slot.sav", [])
    assert is_excluded("slot.sav.gamesavesync-tmp", [])
    assert is_excluded("notes.tmp", ["*.tmp"])
    assert not is_excluded("slot.sav", ["*.tmp"])


def test_scan_skips_backup_dir_and_patterns(tmp_path: Path):
    root = tmp_path / "saves"
    (root / "slot.sav").parent.mkdir(parents=True)
    (root / "slot.sav").write_bytes(b"save")
    (root / "skip.tmp").write_bytes(b"tmp")
    backup = root / ".backup" / "20260101-000000-000000"
    backup.mkdir(parents=True)
    (backup / "slot.sav").write_bytes(b"old")
    found = scan_local(root, ["*.tmp"])
    assert list(found) == ["slot.sav"]


def test_listing_keeps_md5_only():
    files = files_from_lsjson(
        [
            {
                "Path": "a.sav",
                "Size": 3,
                "ModTime": "2026-10-10T18:00:00.000000000Z",
                "IsDir": False,
                "Hashes": {"dropbox": "abc", "md5": "abc123"},
            },
            {
                "Path": "b.sav",
                "Size": 3,
                "ModTime": "2026-10-10T18:00:00Z",
                "IsDir": False,
                "Hashes": {"dropbox": "abc"},
            },
        ]
    )
    assert files["a.sav"].hash == "abc123"
    assert files["b.sav"].hash is None


def test_parse_time_and_short_errors():
    parsed = parse_rclone_time("2026-10-10T18:40:00.000000000Z")
    assert parsed.year == 2026
    assert parsed.utcoffset() is not None
    assert short_error(TimeoutError("connection timed out")) == "Cloud unreachable"
    denied = PermissionError(13, "Permission denied", r"C:\saves\slot.sav")
    assert short_error(denied) == r"Access denied: C:\saves\slot.sav"


def test_prune_names_keeps_the_newest():
    names = ["20260101-000000-000001", "notes", "20260103-000000-000003", "20260102-000000-000002"]
    assert prune_names(names, 2) == ["20260101-000000-000001"]
    assert prune_names(names, 0) == [
        "20260101-000000-000001",
        "20260102-000000-000002",
        "20260103-000000-000003",
    ]
    assert prune_names(names, 10) == []
