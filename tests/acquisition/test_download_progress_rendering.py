from __future__ import annotations

from mpilot.acquisition.domain.download_progress import (
    dynamic_progress_watch_policy,
    render_download_status_payload,
    render_download_status,
    render_downloads_status,
    render_tracking_expired_status,
)
from mpilot.acquisition.models import TorrentStatus


def test_render_download_status_returns_chat_progress_bar():
    status = TorrentStatus(
        name="ubuntu.iso",
        state="downloading",
        progress=0.4,
        size=2_030_000_000,
        seeds=12,
        hash="abcdef1234567890",
        download_speed=12_400_000,
        eta=96,
    )

    message = render_download_status(status)

    assert message == (
        "⬇️ ubuntu.iso\n"
        "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 40%\n"
        "💾 812 MB / 2 GB\n"
        "⚡ Speed: 12.4 MB/s\n"
        "⏱️ ETA: 1m 36s"
    )


def test_render_download_status_handles_complete_downloads():
    status = TorrentStatus(
        name="movie.mkv",
        state="uploading",
        progress=1.0,
        size=8_000_000_000,
        seeds=4,
        hash="abcdef1234567890",
    )

    message = render_download_status(status)

    assert message == (
        "✅ movie.mkv\n"
        "✅ 100%\n"
        "💾 8 GB / 8 GB"
    )


def test_render_tracking_expired_status_keeps_last_snapshot_and_refresh_hint():
    status = TorrentStatus(
        name="movie.mkv",
        state="stalledDL",
        progress=0.52,
        size=8_000_000_000,
        seeds=0,
        hash="abcdef1234567890",
    )

    message = render_tracking_expired_status(status)

    assert message.endswith("Still downloading. Ask for status again to refresh; completion will still notify you.")
    assert "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 52%" in message


def test_render_downloads_status_handles_empty_list():
    assert render_downloads_status([]) == "No active downloads."


def test_dynamic_progress_watch_policy_is_bounded_to_fifteen_minutes():
    policy = dynamic_progress_watch_policy()

    assert policy == {
        "mode": "bounded_edit_loop",
        "max_duration_seconds": 900,
        "update_interval_seconds": 3,
        "min_progress_delta": 0.03,
        "completion_notifications_are_separate": True,
        "timeout_message": "Still downloading. Ask for status again to refresh; completion will still notify you.",
    }


def test_render_download_status_handles_qbittorrent_v5_stopped_state():
    from mpilot.acquisition.models import TorrentStatus

    status = TorrentStatus(
        name="paused.iso",
        state="stoppedDL",  # qBittorrent v5 — was pausedDL pre-v5
        progress=0.37,
        size=2_000_000_000,
        seeds=0,
        hash="abcdef1234567890",
    )

    message = render_download_status(status)

    # Title flips to ⏸️ and bar cells tint orange — same as the legacy pausedDL.
    assert message.startswith("⏸️ paused.iso\n🟧🟧🟧⬜⬜⬜⬜⬜⬜⬜ 37%")


def test_render_download_status_payload_includes_pause_and_delete_buttons_for_active_download():
    status = TorrentStatus(
        name="ubuntu.iso",
        state="downloading",
        progress=0.4,
        size=2_030_000_000,
        seeds=12,
        hash="abcdef1234567890abcdef1234567890abcdef12",
    )

    payload = render_download_status_payload(status)

    assert payload == {
        "message": "⬇️ ubuntu.iso\n🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 40%\n💾 812 MB / 2 GB",
        "buttons": [
            {
                "text": "⏸️",
                "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:pause",
            },
            {
                "text": "❌",
                "callback_data": "dl:abcdef1234567890abcdef1234567890abcdef12:delete",
            },
        ],
    }


def test_render_download_status_payload_includes_resume_and_delete_buttons_for_stopped_download():
    status = TorrentStatus(
        name="paused.iso",
        state="stoppedDL",
        progress=0.37,
        size=2_000_000_000,
        seeds=0,
        hash="abcdef1234567890abcdef1234567890abcdef12",
    )

    payload = render_download_status_payload(status)

    assert [button["text"] for button in payload["buttons"]] == ["▶️", "❌"]
    assert payload["buttons"][0]["callback_data"].endswith(":resume")


def test_render_download_status_payload_omits_buttons_for_complete_download():
    status = TorrentStatus(
        name="movie.mkv",
        state="uploading",
        progress=1.0,
        size=8_000_000_000,
        seeds=4,
        hash="abcdef1234567890abcdef1234567890abcdef12",
    )

    payload = render_download_status_payload(status)

    assert payload["message"] == "✅ movie.mkv\n✅ 100%\n💾 8 GB / 8 GB"
    assert payload["buttons"] == []


def test_render_download_status_near_complete_does_not_round_to_100_percent():
    status = TorrentStatus(
        name="almost-done.mkv",
        state="downloading",
        progress=0.995,
        size=2_100_000_000,
        seeds=8,
        hash="abcdef1234567890abcdef1234567890abcdef12",
    )

    payload = render_download_status_payload(status)

    assert payload["message"] == (
        "⬇️ almost-done.mkv\n"
        "🟩🟩🟩🟩🟩🟩🟩🟩🟩⬜ 99%\n"
        "💾 2.1 GB / 2.1 GB"
    )
    assert [button["text"] for button in payload["buttons"]] == ["⏸️", "❌"]
