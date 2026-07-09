from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.client import QbitlarrApiError
from mcp_server.server import _maybe_register_completion_watch, _start_notifier_if_pending
from mcp_server.notifications import (
    DownloadCompletionNotifier,
    DownloadWatchStore,
    _call_send_status_message,
    completion_hook_from_env,
    default_watch_store_path,
    _send_or_edit_telegram_status_message,
    _telegram_bot_token,
    run_completion_hook_from_env,
    send_hermes_message,
)


class FakeClient:
    def __init__(self, statuses):
        self.statuses = statuses

    async def get_download_status(self, info_hash):
        return self.statuses[info_hash]


class FakeNotifier:
    def __init__(self):
        self.watches = []
        self.events = []
        self.progress_updates = []

    async def register_watch(
        self,
        *,
        info_hash,
        title,
        notification_target,
        metadata=None,
        requester_id=None,
        track_progress=False,
        start=True,
    ):
        self.events.append(("register", start))
        watch = {
            "info_hash": info_hash,
            "title": title,
            "notification_target": notification_target,
            "metadata": metadata or {},
            "requester_id": requester_id,
            "track_progress": track_progress,
        }
        self.watches.append(watch)
        return watch

    async def publish_progress_snapshot(self, watch, status):
        self.events.append(("publish_progress", watch["info_hash"]))
        self.progress_updates.append((watch, status))

    def start(self):
        self.events.append(("start", None))


def test_download_watch_store_deduplicates_by_hash_and_target(tmp_path):
    store = DownloadWatchStore(tmp_path / "watches.json")

    first = store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567"},
        requester_id="user-a",
    )
    second = store.upsert_watch(
        info_hash="ABCDEF",
        title="Updated Title",
        notification_target="telegram:12345",
        metadata={"media_type": "movie"},
        requester_id="user-a",
    )

    assert first["created_at"] == second["created_at"]
    assert second["title"] == "Updated Title"
    assert second["metadata"] == {"imdb_id": "tt1234567", "media_type": "movie"}
    assert len(store.pending_watches()) == 1


def test_download_watch_store_reactivates_requeued_hash(tmp_path):
    store = DownloadWatchStore(tmp_path / "watches.json")
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="user-a",
    )
    store.mark_completion_notified(
        info_hash="abcdef",
        notification_target="telegram:12345",
    )
    store.mark_abandoned(
        info_hash="abcdef",
        notification_target="telegram:12345",
        error="Download not found",
    )

    watch = store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie Retry",
        notification_target="telegram:12345",
        requester_id="user-a",
    )

    assert watch["title"] == "Example Movie Retry"
    assert watch["abandoned_at"] is None
    assert watch["notified_at"] is None
    assert watch["completion_notified_at"] is None
    assert watch["error_count"] == 0
    assert watch["last_error"] is None
    assert len(store.pending_watches()) == 1


def test_download_watch_store_quarantines_corrupt_json(tmp_path):
    store_path = tmp_path / "watches.json"
    store_path.write_text("{not-json", encoding="utf-8")
    store = DownloadWatchStore(store_path)

    assert store.pending_watches() == []
    assert not store_path.exists()
    assert (tmp_path / "watches.json.corrupt").read_text(encoding="utf-8") == "{not-json"

    watch = store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="user-a",
    )

    assert watch["info_hash"] == "abcdef"
    assert len(store.pending_watches()) == 1


def test_default_watch_store_path_uses_mpilot_acquisition_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("QBITLARR_NOTIFICATION_WATCHES_PATH", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_NOTIFICATION_WATCHES_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert (
        default_watch_store_path()
        == tmp_path / "xdg" / "mpilot" / "acquisition" / "download-notification-watches.json"
    )


def test_default_watch_store_path_falls_back_to_existing_legacy_path(monkeypatch, tmp_path):
    monkeypatch.delenv("QBITLARR_NOTIFICATION_WATCHES_PATH", raising=False)
    monkeypatch.delenv("MPILOT_ACQUISITION_NOTIFICATION_WATCHES_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    legacy_path = tmp_path / "xdg" / "qbitlarr" / "download-notification-watches.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("[]", encoding="utf-8")

    assert default_watch_store_path() == legacy_path

    mpilot_path = tmp_path / "xdg" / "mpilot" / "acquisition" / "download-notification-watches.json"
    mpilot_path.parent.mkdir(parents=True)
    mpilot_path.write_text("[]", encoding="utf-8")

    assert default_watch_store_path() == mpilot_path


def test_notifier_sends_one_message_when_download_completes(tmp_path):
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    notifier = DownloadCompletionNotifier(store=store, client=client, send_message=fake_send)
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567"},
        requester_id="user-a",
    )

    asyncio.run(notifier.poll_once())
    asyncio.run(notifier.poll_once())

    assert sent == [
        (
            "telegram:12345",
            "Download complete: Example Movie (tt1234567)",
        )
    ]
    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    assert watches[0]["notified_at"] is not None


def test_notifier_runs_completion_hook_before_completion_message(tmp_path):
    events: list[dict] = []
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
                "content_path": "/media/Movies/Example.Movie.mkv",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    async def fake_hook(event):
        events.append(event)

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        completion_hook=fake_hook,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567", "media_type": "movie"},
        requester_id="telegram:12345",
    )

    asyncio.run(notifier.poll_once())

    assert len(events) == 1
    assert events[0]["event"] == "download_complete"
    assert events[0]["info_hash"] == "abcdef"
    assert events[0]["content_path"] == "/media/Movies/Example.Movie.mkv"
    assert events[0]["imdb_id"] == "tt1234567"
    assert events[0]["media_type"] == "movie"
    assert events[0]["notification_target"] == "telegram:12345"
    assert events[0]["requester_id"] == "telegram:12345"
    assert sent == [
        (
            "telegram:12345",
            "Download complete: Example Movie (tt1234567)",
        )
    ]


def test_notifier_appends_completion_followup_message(tmp_path):
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
                "content_path": "/media/Movies/Example.Movie.mkv",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    notifier = DownloadCompletionNotifier(store=store, client=client, send_message=fake_send)
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={
            "imdb_id": "tt1234567",
            "completion_followup_message": "下载完成，准备开始处理字幕。",
        },
        requester_id="telegram:12345",
    )

    asyncio.run(notifier.poll_once())

    assert sent == [
        (
            "telegram:12345",
            "Download complete: Example Movie (tt1234567)\n"
            "下载完成，准备开始处理字幕。",
        )
    ]


def test_notifier_sends_download_complete_even_when_completion_hook_retries(tmp_path):
    sent: list[tuple[str, str]] = []
    events: list[dict] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
                "content_path": "/media/Movies/Example.Movie.mkv",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    async def fake_hook(event):
        events.append(event)
        raise RuntimeError("runtime unavailable")

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        completion_hook=fake_hook,
        max_errors=3,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567"},
        requester_id="telegram:12345",
    )

    asyncio.run(notifier.poll_once())
    asyncio.run(notifier.poll_once())

    assert len(events) == 2
    assert sent == [
        (
            "telegram:12345",
            "Download complete: Example Movie (tt1234567)",
        )
    ]
    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    assert watches[0]["completion_notified_at"] is not None
    assert watches[0]["notified_at"] is None
    assert watches[0]["error_count"] == 2


def test_notifier_keeps_polling_other_watches_when_completion_send_fails(tmp_path):
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "badfeed": {
                "name": "Broken.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "badfeed",
            },
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
            },
        }
    )

    async def fake_send(target, message):
        if "Broken Movie" in message:
            raise RuntimeError("telegram unavailable")
        sent.append((target, message))

    notifier = DownloadCompletionNotifier(store=store, client=client, send_message=fake_send, max_errors=3)
    store.upsert_watch(
        info_hash="badfeed",
        title="Broken Movie",
        notification_target="telegram:12345",
        requester_id="user-a",
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="user-a",
    )

    asyncio.run(notifier.poll_once())

    assert sent == [("telegram:12345", "Download complete: Example Movie")]
    watches = {watch["info_hash"]: watch for watch in json.loads(Path(tmp_path / "watches.json").read_text())["watches"]}
    assert watches["badfeed"]["notified_at"] is None
    assert watches["badfeed"]["error_count"] == 1
    assert watches["badfeed"]["last_error"] == "completion notification failed: telegram unavailable"
    assert watches["abcdef"]["notified_at"] is not None


def test_poll_loop_continues_after_poll_once_failure(monkeypatch, tmp_path):
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient({})
    notifier = DownloadCompletionNotifier(store=store, client=client, send_message=lambda _target, _message: None)
    calls = []

    async def flaky_poll_once():
        calls.append("poll")
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        raise asyncio.CancelledError()

    async def no_sleep(_seconds):
        return None

    notifier.poll_once = flaky_poll_once
    monkeypatch.setattr("mcp_server.notifications.asyncio.sleep", no_sleep)

    try:
        asyncio.run(notifier._poll_loop())
    except asyncio.CancelledError:
        pass

    assert calls == ["poll", "poll"]


def test_notifier_publishes_separate_progress_status_message(tmp_path):
    status_updates: list[tuple[str, str, str, str | None]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "downloading",
                "progress": 0.4,
                "size": 2_000_000_000,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(_target, _message):
        raise AssertionError("completion messages should not be sent for incomplete downloads")

    async def fake_status_send(target, status_key, message, message_id=None):
        status_updates.append((target, status_key, message, message_id))
        return "progress-message-1"

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        send_status_message=fake_status_send,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="telegram:12345",
        track_progress=True,
    )

    asyncio.run(notifier.poll_once())

    assert status_updates == [
        (
            "telegram:12345",
            "qbitlarr-download:abcdef",
            "⬇️ Example.Movie.2026.1080p.WEB-DL.H.264-GRP\n"
            "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 40%\n"
            "💾 800 MB / 2 GB",
            None,
        )
    ]
    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    progress = watches[0]["progress_tracking"]
    assert progress["message_id"] == "progress-message-1"
    assert progress["last_message"].startswith("⬇️ Example.Movie")


def test_notifier_keeps_watch_when_progress_status_send_fails(tmp_path):
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "downloading",
                "progress": 0.4,
                "size": 2_000_000_000,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(_target, _message):
        raise AssertionError("completion messages should not be sent for incomplete downloads")

    async def failing_status_send(_target, _status_key, _message, message_id=None):
        raise RuntimeError("telegram unavailable")

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        send_status_message=failing_status_send,
        max_errors=10,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="telegram:12345",
        track_progress=True,
    )

    asyncio.run(notifier.poll_once())

    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    assert watches[0]["abandoned_at"] is None
    assert watches[0]["error_count"] == 1
    assert watches[0]["last_error"] == "progress update failed: telegram unavailable"
    assert watches[0]["progress_tracking"]["message_id"] is None


def test_status_message_type_error_inside_callback_is_not_retried_as_legacy_signature():
    calls = []

    async def fake_status_send(_target, _status_key, _message, _message_id=None, _buttons=None):
        calls.append("called")
        raise TypeError("argument exploded inside transport")

    try:
        asyncio.run(
            _call_send_status_message(
                fake_status_send,
                "telegram:12345",
                "qbitlarr-download:abcdef",
                "status",
                None,
                [{"text": "Pause", "callback_data": "dl:pause:abcdef"}],
            )
        )
    except TypeError as exc:
        assert str(exc) == "argument exploded inside transport"
    else:
        raise AssertionError("internal TypeError should propagate")

    assert calls == ["called"]


def test_notifier_finalizes_expired_progress_message_on_completion(tmp_path):
    sent: list[tuple[str, str]] = []
    status_updates: list[tuple[str, str, str, str | None]] = []
    store_path = tmp_path / "watches.json"
    store = DownloadWatchStore(store_path)
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    async def fake_status_send(target, status_key, message, message_id=None):
        status_updates.append((target, status_key, message, message_id))
        return message_id

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        send_status_message=fake_status_send,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="telegram:12345",
        track_progress=True,
    )
    payload = json.loads(store_path.read_text())
    payload["watches"][0]["progress_tracking"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    payload["watches"][0]["progress_tracking"]["message_id"] = "progress-message-1"
    store_path.write_text(json.dumps(payload))

    asyncio.run(notifier.poll_once())

    assert len(status_updates) == 1
    assert status_updates[0][0] == "telegram:12345"
    assert status_updates[0][1] == "qbitlarr-download:abcdef"
    assert status_updates[0][3] == "progress-message-1"
    assert status_updates[0][2].startswith("✅ Example.Movie.2026")
    assert "✅ 100%" in status_updates[0][2]
    assert sent == [
        (
            "telegram:12345",
            "Download complete: Example Movie",
        )
    ]


def test_notifier_does_not_create_final_100_percent_progress_message_on_completion(tmp_path):
    sent: list[tuple[str, str]] = []
    status_updates: list[tuple[str, str, str, str | None]] = []
    store_path = tmp_path / "watches.json"
    store = DownloadWatchStore(store_path)
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "uploading",
                "progress": 1.0,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    async def fake_status_send(target, status_key, message, message_id=None):
        status_updates.append((target, status_key, message, message_id))
        return "replacement-progress-message"

    notifier = DownloadCompletionNotifier(
        store=store,
        client=client,
        send_message=fake_send,
        send_status_message=fake_status_send,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="telegram:12345",
        track_progress=True,
    )
    payload = json.loads(store_path.read_text())
    store_path.write_text(json.dumps(payload))

    asyncio.run(notifier.poll_once())

    assert status_updates == []
    assert sent == [("telegram:12345", "Download complete: Example Movie")]


def test_notifier_keeps_incomplete_download_pending(tmp_path):
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")
    client = FakeClient(
        {
            "abcdef": {
                "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
                "state": "downloading",
                "progress": 0.5,
                "hash": "abcdef",
            }
        }
    )

    async def fake_send(target, message):
        sent.append((target, message))

    notifier = DownloadCompletionNotifier(store=store, client=client, send_message=fake_send)
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        requester_id="user-a",
    )

    asyncio.run(notifier.poll_once())

    assert sent == []
    assert len(store.pending_watches()) == 1


def test_mcp_wrapper_registers_watch_from_download_status_payload():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "seeds": 12,
        },
        "imdb_id": "tt1234567",
        "media_type": "movie",
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target="telegram:12345",
            requester_id="user-a",
        )
    )

    assert notifier.watches == [
        {
            "info_hash": "abcdef1234567890",
            "title": "Example Movie",
            "notification_target": "telegram:12345",
            "metadata": {"imdb_id": "tt1234567", "media_type": "movie"},
            "requester_id": "user-a",
            "track_progress": True,
        }
    ]
    assert payload["message"] == ""
    assert payload["notification_watch"]["status"] == "watching"
    assert payload["progress_watch"]["status"] == "tracking"


def test_mcp_wrapper_registers_completion_followup_message():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "seeds": 12,
        },
        "imdb_id": "tt1234567",
        "media_type": "movie",
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target="telegram:12345",
            requester_id="user-a",
            completion_followup_message="下载完成，准备开始处理字幕。",
        )
    )

    assert notifier.watches[0]["metadata"] == {
        "imdb_id": "tt1234567",
        "media_type": "movie",
        "completion_followup_message": "下载完成，准备开始处理字幕。",
    }


def test_mcp_wrapper_does_not_store_incomplete_future_content_path():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "progress": 0.42,
            "state": "downloading",
            "content_path": "/media/Movies/Example.Movie.mkv",
        },
        "imdb_id": "tt1234567",
        "media_type": "movie",
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target="telegram:12345",
            requester_id="user-a",
        )
    )

    assert notifier.watches[0]["metadata"] == {"imdb_id": "tt1234567", "media_type": "movie"}


def test_mcp_wrapper_publishes_initial_progress_before_starting_background_loop():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "progress": 0.42,
            "state": "downloading",
        },
        "imdb_id": "tt1234567",
        "media_type": "movie",
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target="telegram:12345",
            requester_id="user-a",
        )
    )

    assert notifier.events == [
        ("register", False),
        ("start", None),
    ]
    assert notifier.progress_updates == []


def test_mcp_wrapper_defaults_notification_target_to_requester_id():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "seeds": 12,
        },
        "imdb_id": "tt1234567",
        "media_type": "movie",
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target=None,
            requester_id="telegram:123456789",
        )
    )

    assert notifier.watches == [
        {
            "info_hash": "abcdef1234567890",
            "title": "Example Movie",
            "notification_target": "telegram:123456789",
            "metadata": {"imdb_id": "tt1234567", "media_type": "movie"},
            "requester_id": "telegram:123456789",
            "track_progress": True,
        }
    ]
    assert payload["message"] == ""
    assert payload["notification_watch"]["status"] == "watching"
    assert payload["progress_watch"]["status"] == "tracking"


def test_mcp_wrapper_skips_default_notification_for_non_target_requester_id():
    notifier = FakeNotifier()
    payload = {
        "status": "success",
        "action": "auto_download",
        "title": "Example Movie",
        "message": "Example Movie is now downloading with 12 seeders. You can ask for a status update any time.",
        "download_status": {
            "hash": "abcdef1234567890",
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "seeds": 12,
        },
    }

    asyncio.run(
        _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target=None,
            requester_id="friend-a",
        )
    )

    assert notifier.watches == []
    assert "notification_watch" not in payload


def test_notifier_abandons_watch_after_repeated_status_errors(tmp_path):
    sent: list[tuple[str, str]] = []
    store = DownloadWatchStore(tmp_path / "watches.json")

    class MissingClient:
        async def get_download_status(self, info_hash):
            raise RuntimeError("not found")

    async def fake_send(target, message):
        sent.append((target, message))

    notifier = DownloadCompletionNotifier(
        store=store,
        client=MissingClient(),
        send_message=fake_send,
        max_errors=2,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567"},
    )

    asyncio.run(notifier.poll_once())
    assert len(store.pending_watches()) == 1

    asyncio.run(notifier.poll_once())

    assert store.pending_watches() == []
    assert sent == [
        (
            "telegram:12345",
            "Download tracking stopped: Example Movie (tt1234567)\n"
            "I couldn't verify this torrent status after repeated attempts.",
        )
    ]
    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    assert watches[0]["abandoned_at"] is not None
    assert watches[0]["error_count"] == 2


def test_notifier_reports_deleted_download_immediately(tmp_path):
    sent: list[tuple[str, str]] = []
    events: list[dict] = []
    store = DownloadWatchStore(tmp_path / "watches.json")

    class MissingClient:
        async def get_download_status(self, info_hash):
            raise QbitlarrApiError("Download not found", status_code=404)

    async def fake_send(target, message):
        sent.append((target, message))

    async def fake_hook(event):
        events.append(event)

    notifier = DownloadCompletionNotifier(
        store=store,
        client=MissingClient(),
        send_message=fake_send,
        completion_hook=fake_hook,
        max_errors=10,
    )
    store.upsert_watch(
        info_hash="abcdef",
        title="Example Movie",
        notification_target="telegram:12345",
        metadata={"imdb_id": "tt1234567"},
        requester_id="telegram:12345",
    )

    asyncio.run(notifier.poll_once())

    assert events == [
        {
            "event": "download_removed",
            "info_hash": "abcdef",
            "title": "Example Movie",
            "notification_target": "telegram:12345",
            "requester_id": "telegram:12345",
            "metadata": {"imdb_id": "tt1234567"},
            "error": {"type": "QbitlarrApiError", "message": "Download not found"},
        }
    ]
    assert store.pending_watches() == []
    assert sent == [
        (
            "telegram:12345",
            "Download task removed: Example Movie (tt1234567)\n"
            "It was deleted from qBittorrent before it finished, possibly by an administrator.",
        )
    ]
    watches = json.loads(Path(tmp_path / "watches.json").read_text())["watches"]
    assert watches[0]["abandoned_at"] is not None
    assert watches[0]["last_error"] == "Download not found"


def test_start_notifier_if_pending_starts_only_when_store_has_pending_watches():
    class FakeStore:
        def __init__(self, watches):
            self._watches = watches

        def pending_watches(self):
            return self._watches

    class FakeNotifier:
        def __init__(self, watches):
            self.store = FakeStore(watches)
            self.started = False

        def start(self):
            self.started = True

    idle_notifier = FakeNotifier([])
    _start_notifier_if_pending(idle_notifier)
    assert idle_notifier.started is False

    pending_notifier = FakeNotifier([{"info_hash": "abcdef"}])
    _start_notifier_if_pending(pending_notifier)
    assert pending_notifier.started is True


def test_send_hermes_message_uses_configured_profile(monkeypatch):
    calls = []

    class FakeProcess:
        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setenv("QBITLARR_HERMES_BIN", "/usr/local/bin/hermes")
    monkeypatch.setenv("QBITLARR_HERMES_PROFILE", "example-bot")
    monkeypatch.setattr("mcp_server.notifications.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(send_hermes_message("telegram:12345", "done"))

    assert calls == [
        (
            (
                "/usr/local/bin/hermes",
                "--profile",
                "example-bot",
                "send",
                "--to",
                "telegram:12345",
                "--quiet",
                "done",
            ),
            {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            },
        )
    ]


def test_send_hermes_message_times_out(monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.killed = False
            self.reaped_after_kill = False

        async def wait(self):
            if self.killed:
                self.reaped_after_kill = True
                return -9
            await asyncio.Event().wait()
            return 0

        def kill(self):
            self.killed = True

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def fake_wait_for(_awaitable, timeout):
        assert timeout == 0.5
        raise TimeoutError

    monkeypatch.setenv("QBITLARR_HERMES_SEND_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setattr("mcp_server.notifications.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr("mcp_server.notifications.asyncio.wait_for", fake_wait_for)

    try:
        asyncio.run(send_hermes_message("telegram:12345", "done"))
    except RuntimeError as exc:
        assert str(exc) == "hermes send timed out after 0.5 seconds"
    else:
        raise AssertionError("send_hermes_message should fail when hermes times out")
    assert process.killed is True
    assert process.reaped_after_kill is True


def test_run_completion_hook_from_env_sends_json_payload(monkeypatch):
    calls = []

    class FakeProcess:
        async def communicate(self, payload):
            calls.append(payload)
            return b"ok", b""

        @property
        def returncode(self):
            return 0

    async def fake_create_subprocess_exec(*command, **kwargs):
        assert command == ("/bin/hook", "--flag")
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
        return FakeProcess()

    monkeypatch.setenv("QBITLARR_COMPLETION_HOOK_COMMAND", "/bin/hook --flag")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(run_completion_hook_from_env({"info_hash": "abcdef", "content_path": "/media/Movie.mkv"}))

    assert json.loads(calls[0].decode()) == {
        "content_path": "/media/Movie.mkv",
        "info_hash": "abcdef",
    }


def test_telegram_bot_token_prefers_hermes_profile_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    profile = tmp_path / "profile" / "example-bot"
    (home / ".hermes").mkdir(parents=True)
    profile.mkdir(parents=True)
    (home / ".hermes" / ".env").write_text("TELEGRAM_BOT_TOKEN=global-token\n")
    (profile / ".env").write_text("TELEGRAM_BOT_TOKEN=profile-token\n")

    monkeypatch.delenv("QBITLARR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("QBITLARR_HERMES_ENV_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert _telegram_bot_token() == "profile-token"


def test_download_notification_paths_prefer_mpilot_acquisition_env(monkeypatch, tmp_path):
    mpilot_path = tmp_path / "mpilot-watches.json"
    legacy_path = tmp_path / "legacy-watches.json"
    monkeypatch.setenv("MPILOT_ACQUISITION_NOTIFICATION_WATCHES_PATH", str(mpilot_path))
    monkeypatch.setenv("QBITLARR_NOTIFICATION_WATCHES_PATH", str(legacy_path))

    assert default_watch_store_path() == mpilot_path


def test_download_telegram_token_prefers_mpilot_env(monkeypatch):
    monkeypatch.setenv("MPILOT_TELEGRAM_BOT_TOKEN", "mpilot-token")
    monkeypatch.setenv("QBITLARR_TELEGRAM_BOT_TOKEN", "legacy-token")

    assert _telegram_bot_token() == "mpilot-token"


def test_completion_hook_uses_mpilot_acquisition_env(monkeypatch):
    monkeypatch.setenv("MPILOT_ACQUISITION_COMPLETION_HOOK_COMMAND", "/bin/hook")
    monkeypatch.setenv("QBITLARR_COMPLETION_HOOK_COMMAND", "")

    assert completion_hook_from_env() is run_completion_hook_from_env


def test_telegram_status_message_sends_new_message_when_edit_target_is_missing(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_telegram_api_post(_client, _token, method, payload):
        calls.append((method, payload))
        if method == "editMessageText":
            raise RuntimeError("Bad Request: message to edit not found")
        return {"result": {"message_id": 222}}

    monkeypatch.setattr("mcp_server.notifications._telegram_api_post", fake_telegram_api_post)

    result = asyncio.run(
        _send_or_edit_telegram_status_message(
            token="token",
            chat_id="12345",
            thread_id=None,
            message="progress",
            message_id="111",
        )
    )

    assert result == "222"
    assert calls == [
        (
            "editMessageText",
            {
                "chat_id": "12345",
                "message_id": 111,
                "text": "progress",
                "disable_web_page_preview": True,
            },
        ),
        (
            "sendMessage",
            {
                "chat_id": "12345",
                "text": "progress",
                "disable_web_page_preview": True,
            },
        ),
    ]


def test_telegram_status_message_sends_download_control_buttons(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_telegram_api_post(_client, _token, method, payload):
        calls.append((method, payload))
        return {"result": {"message_id": 222}}

    monkeypatch.setattr("mcp_server.notifications._telegram_api_post", fake_telegram_api_post)

    result = asyncio.run(
        _send_or_edit_telegram_status_message(
            token="token",
            chat_id="12345",
            thread_id=None,
            message="progress",
            message_id=None,
            buttons=[
                {"text": "⏸️", "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:pause"},
                {"text": "❌", "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:delete"},
            ],
        )
    )

    assert result == "222"
    assert calls == [
        (
            "sendMessage",
            {
                "chat_id": "12345",
                "text": "progress",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "⏸️",
                                "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:pause",
                            },
                            {
                                "text": "❌",
                                "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:delete",
                            },
                        ]
                    ]
                },
            },
        )
    ]


def test_telegram_status_message_keeps_existing_message_on_transient_edit_failure(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_telegram_api_post(_client, _token, method, payload):
        calls.append((method, payload))
        if method == "editMessageText":
            raise RuntimeError("Too Many Requests: retry after 30")
        raise AssertionError("transient edit failures should not create a second progress message")

    monkeypatch.setattr("mcp_server.notifications._telegram_api_post", fake_telegram_api_post)

    result = asyncio.run(
        _send_or_edit_telegram_status_message(
            token="token",
            chat_id="12345",
            thread_id=None,
            message="progress",
            message_id="111",
        )
    )

    assert result == "111"
    assert calls == [
        (
            "editMessageText",
            {
                "chat_id": "12345",
                "message_id": 111,
                "text": "progress",
                "disable_web_page_preview": True,
            },
        )
    ]


def test_run_completion_hook_from_env_honors_cwd_and_pythonpath(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        async def communicate(self, payload):
            return b"ok", b""

        @property
        def returncode(self):
            return 0

    async def fake_create_subprocess_exec(*command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setenv("QBITLARR_COMPLETION_HOOK_COMMAND", "/bin/hook")
    monkeypatch.setenv("QBITLARR_COMPLETION_HOOK_CWD", str(tmp_path))
    monkeypatch.setenv("QBITLARR_COMPLETION_HOOK_PYTHONPATH", "/opt/mst")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(run_completion_hook_from_env({"info_hash": "abcdef"}))

    command, kwargs = calls[0]
    assert command == ("/bin/hook",)
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["PYTHONPATH"].split(":")[0] == "/opt/mst"
