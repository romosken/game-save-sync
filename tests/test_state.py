from gamesavesync.state import (
    AppState,
    FolderStatus,
    ProfileStatus,
    StateStore,
    attention_names,
    record_result,
    status_text,
)
from gamesavesync.util import format_local_stamp as format_stamp


def test_status_text():
    assert status_text(None) == "Never synced"
    assert status_text(ProfileStatus(last_result="failed", last_direction="pull")) == "FAILED - pull"
    assert status_text(ProfileStatus(last_result="skipped")) == "Skipped: app running"
    assert (
        status_text(ProfileStatus(last_result="skipped", last_error="A sync is already running"))
        == "Skipped: sync in progress"
    )
    assert status_text(ProfileStatus(last_result="ok", pending_push=True)) == "Pending upload"
    iso = "2026-10-10T21:40:00+00:00"
    status = ProfileStatus(last_sync_at=iso, last_result="ok", last_direction="both")
    assert status_text(status) == f"OK - {format_stamp(iso)}"


def test_pending_push_and_attention(tmp_path):
    store = StateStore(tmp_path / "state.json")
    record_result(store, name="PCSX2", direction="push", result="failed", error="Cloud unreachable", push_attempted=True, push_ok=False)
    state = store.load()
    assert state.profiles["PCSX2"].pending_push is True
    assert attention_names(["PCSX2", "Other"], state) == ["PCSX2"]

    record_result(store, name="PCSX2", direction="pull", result="failed", error="Cloud unreachable", push_attempted=False)
    assert store.load().profiles["PCSX2"].pending_push is True

    record_result(
        store,
        name="PCSX2",
        direction="both",
        result="ok",
        folders={"memcards": FolderStatus(result="ok", uploaded=1)},
        push_attempted=True,
        push_ok=True,
    )
    saved = store.load().profiles["PCSX2"]
    assert saved.pending_push is False
    assert saved.folders["memcards"].uploaded == 1


def test_skip_keeps_existing_failure(tmp_path):
    store = StateStore(tmp_path / "state.json")
    record_result(store, name="PCSX2", direction="pull", result="failed", error="Cloud unreachable")
    record_result(store, name="PCSX2", direction="both", result="skipped", error="Skipped: app running")
    status = store.load().profiles["PCSX2"]
    assert status.last_result == "failed"
    assert status.last_error == "Cloud unreachable"

    record_result(store, name="Fresh", direction="both", result="skipped", error="Skipped: app running")
    assert store.load().profiles["Fresh"].last_result == "skipped"


def test_atomic_write_replaces_file(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(AppState())
    assert not (tmp_path / "state.json.tmp").exists()
    assert store.load().profiles == {}
