import json
import os
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles.jobs import JobStore
from mpilot.subtitles import notifications
from mpilot.subtitles.notifications import (
    JobCompletionNotifier,
    JobNotificationStore,
    resolve_notification_target,
)
from tests.subtitles.test_jobs import sample_request


class JobNotificationTests(unittest.TestCase):
    def test_missing_title_fallback_follows_notification_language(self):
        self.assertEqual(notifications._title_for_watch({"language": "en"}), "your subtitle job")
        self.assertEqual(notifications._title_for_watch({"language": "fr"}), "votre tâche de sous-titres")
        self.assertEqual(notifications._title_for_watch({"language": "zh"}), "字幕任务")

    def test_notification_store_deduplicates_by_job_and_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobNotificationStore(Path(tmp) / "watches.json")

            first = store.upsert_watch(
                job_id="job_123",
                job_store_dir=Path(tmp) / "jobs",
                title="First title",
                notification_target="telegram:12345",
                requester_id="telegram:12345",
                language="zh",
                metadata={"imdb": "tt1234567"},
            )
            second = store.upsert_watch(
                job_id="job_123",
                job_store_dir=Path(tmp) / "jobs",
                title="Updated title",
                notification_target="telegram:12345",
                requester_id="telegram:12345",
                language="fr",
                metadata={"rating_key": "1468"},
            )

            self.assertEqual(first["created_at"], second["created_at"])
            self.assertEqual(second["title"], "Updated title")
            self.assertEqual(second["language"], "fr")
            self.assertEqual(second["metadata"], {"imdb": "tt1234567", "rating_key": "1468"})
            self.assertEqual(len(store.pending_watches()), 1)

    def test_notifier_sends_one_message_when_job_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_succeeded(job["job_id"], {"ok": True})
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append((target, message)),
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="The Devil Wears Prada",
                notification_target="telegram:12345",
                language="zh",
            )

            notifier.poll_once()
            notifier.poll_once()

            self.assertEqual(
                sent,
                [
                    (
                        "telegram:12345",
                        "字幕已制作完成：The Devil Wears Prada\n请先退回到影片详情页，再重新进入播放，播放器就会加载最新字幕。",
                    )
                ],
            )
            watches = json.loads((Path(tmp) / "watches.json").read_text(encoding="utf-8"))["watches"]
            self.assertIsNotNone(watches[0]["notified_at"])
            self.assertEqual(watches[0]["terminal_status"], "succeeded")

    def test_competing_notifiers_send_terminal_notification_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_succeeded(job["job_id"], {"ok": True})
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []
            second_notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append(("second", target, message)),
            )
            triggered_second_poll = False

            def first_send(target, message):
                nonlocal triggered_second_poll
                sent.append(("first", target, message))
                if not triggered_second_poll:
                    triggered_second_poll = True
                    second_notifier.poll_once()

            first_notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=first_send,
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Race Movie",
                notification_target="telegram:12345",
                language="zh",
            )

            first_notifier.poll_once()

            self.assertEqual([entry[0] for entry in sent], ["first"])
            watches = json.loads((Path(tmp) / "watches.json").read_text(encoding="utf-8"))["watches"]
            self.assertIsNotNone(watches[0]["notified_at"])
            self.assertEqual(watches[0]["terminal_status"], "succeeded")

    def test_notifier_keeps_running_job_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append((target, message)),
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Running Movie",
                notification_target="telegram:12345",
                language="en",
            )

            notifier.poll_once()

            self.assertEqual(sent, [])
            self.assertEqual(len(notification_store.pending_watches()), 1)

    def test_notifier_updates_running_status_message_with_translation_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_progress(
                job["job_id"],
                {
                    "stage": "translating",
                    "message": "Translated subtitle chunk 3 of 14.",
                    "details": {"completed_chunks": 3, "total_chunks": 14},
                },
            )
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            status_updates = []

            def send_status_message(target, status_key, message, message_id=None):
                status_updates.append((target, status_key, message, message_id))
                return "status-message-1"

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda _target, _message: None,
                send_status_message=send_status_message,
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Chunk Movie",
                notification_target="telegram:12345",
                language="zh",
            )

            notifier.poll_once()
            notifier.poll_once()

            self.assertEqual(status_updates[0][0], "telegram:12345")
            self.assertEqual(status_updates[0][1], "mpilot-subtitle:%s" % job["job_id"])
            self.assertIn("字幕处理中：Chunk Movie", status_updates[0][2])
            self.assertIn("🕐 正在翻译字幕", status_updates[0][2])
            self.assertIn("翻译进度：3/14", status_updates[0][2])
            self.assertIsNone(status_updates[0][3])
            self.assertEqual(status_updates[1][3], "status-message-1")
            self.assertIn("🕓 正在翻译字幕", status_updates[1][2])
            self.assertEqual(len(status_updates), 2)
            watches = json.loads((Path(tmp) / "watches.json").read_text(encoding="utf-8"))["watches"]
            self.assertEqual(watches[0]["status_tracking"]["message_id"], "status-message-1")

    def test_notifier_defers_status_until_notification_not_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            status_updates = []

            def send_status_message(target, status_key, message, message_id=None):
                status_updates.append((target, status_key, message, message_id))
                return "status-message-1"

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda _target, _message: None,
                send_status_message=send_status_message,
            )
            future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds").replace("+00:00", "Z")
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Delayed Movie",
                notification_target="telegram:12345",
                language="zh",
                notification_not_before=future,
            )

            notifier.poll_once()

            self.assertEqual(status_updates, [])
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Delayed Movie",
                notification_target="telegram:12345",
                language="zh",
                notification_not_before=past,
            )

            notifier.poll_once()

            self.assertEqual(len(status_updates), 1)
            self.assertIn("字幕处理中：Delayed Movie", status_updates[0][2])

    def test_notifier_reloads_latest_watch_before_publishing_running_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            stale_watch = notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Race Movie",
                notification_target="telegram:12345",
                language="zh",
            )
            notification_store.mark_status_published(
                job_id=job["job_id"],
                notification_target="telegram:12345",
                message="字幕处理中：Race Movie\n旧状态",
                message_id="status-message-existing",
                tick=1,
            )
            status_updates = []

            def send_status_message(target, status_key, message, message_id=None):
                status_updates.append((target, status_key, message, message_id))
                return message_id

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda _target, _message: None,
                send_status_message=send_status_message,
            )

            notifier.publish_running_status(stale_watch, job_store.get(job["job_id"]))

            self.assertEqual(status_updates[0][3], "status-message-existing")
            self.assertIn("🕓 正在准备字幕处理", status_updates[0][2])
            watches = json.loads((Path(tmp) / "watches.json").read_text(encoding="utf-8"))["watches"]
            self.assertEqual(watches[0]["status_tracking"]["message_id"], "status-message-existing")

    def test_notifier_finalizes_status_message_before_completion_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []
            status_updates = []

            def send_status_message(target, status_key, message, message_id=None):
                status_updates.append((target, status_key, message, message_id))
                return "status-message-1"

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append((target, message)),
                send_status_message=send_status_message,
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Final Movie",
                notification_target="telegram:12345",
                language="zh",
            )

            notifier.poll_once()
            job_store.mark_succeeded(job["job_id"], {"ok": True})
            notifier.poll_once()

            self.assertEqual(len(status_updates), 2)
            self.assertIn("字幕处理中：Final Movie", status_updates[0][2])
            self.assertIn("字幕已完成：Final Movie", status_updates[1][2])
            self.assertEqual(status_updates[1][3], "status-message-1")
            self.assertEqual(len(sent), 1)

    def test_running_status_send_failures_pause_status_updates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            attempts = []

            def fail_status(_target, _status_key, _message, message_id=None):
                attempts.append(message_id)
                raise RuntimeError("telegram unavailable")

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda _target, _message: None,
                send_status_message=fail_status,
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Pause Movie",
                notification_target="telegram:12345",
                language="zh",
            )

            notifier.poll_once()
            notifier.poll_once()
            notifier.poll_once()
            notifier.poll_once()

            self.assertEqual(len(attempts), 3)
            watches = notification_store.pending_watches()
            self.assertEqual(len(watches), 1)
            tracking = watches[0]["status_tracking"]
            self.assertEqual(tracking["error_count"], 3)
            self.assertIsNotNone(tracking["paused_at"])

    def test_running_status_labels_cover_workflow_progress_stages(self):
        stages = [
            "reading_source_subtitle",
            "checking_local_subtitles",
            "checking_source_sidecar",
            "probing_embedded_subtitles",
            "extracting_embedded_subtitle",
            "using_remote_source_executor",
            "local_source_missing",
            "searching_online_subtitles",
            "online_subtitle_selected",
            "source_subtitle_ready",
            "normalizing_source",
            "media_resolved",
            "translating",
            "rendering_output",
            "output_ready",
            "writing_back",
            "write_back_complete",
            "refreshing_plex",
            "plex_refresh_complete",
        ]

        for stage in stages:
            with self.subTest(stage=stage):
                self.assertNotEqual(notifications._zh_stage_label(stage), "字幕任务正在处理")
                self.assertNotEqual(notifications._en_stage_label(stage), "Processing subtitles")
                self.assertNotEqual(notifications._fr_stage_label(stage), "Traitement des sous-titres")

    def test_activity_label_cycles_spinner_frames(self):
        self.assertEqual(notifications._activity_label("正在制作中", 1), "🕐 正在制作中")
        self.assertEqual(notifications._activity_label("正在制作中", 2), "🕓 正在制作中")
        self.assertEqual(notifications._activity_label("正在制作中", 3), "🕗 正在制作中")
        self.assertEqual(notifications._activity_label("正在制作中", 4), "🕐 正在制作中")

    def test_notification_store_updates_wait_for_cross_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watches.json"
            store = JobNotificationStore(path)
            store.upsert_watch(
                job_id="job_123",
                job_store_dir=Path(tmp) / "jobs",
                title="Locked Movie",
                notification_target="telegram:12345",
                language="zh",
            )
            ready_file = Path(tmp) / "lock-held"
            script = (
                "import fcntl, pathlib, time; "
                "path=pathlib.Path(%r); "
                "lock=path.open('a+'); "
                "fcntl.flock(lock, fcntl.LOCK_EX); "
                "(path.parent/'lock-held').write_text('1'); "
                "time.sleep(0.35); "
                "fcntl.flock(lock, fcntl.LOCK_UN)"
            ) % str(path.with_suffix(path.suffix + ".lock"))
            process = subprocess.Popen([sys.executable, "-c", script])
            try:
                deadline = time.monotonic() + 2.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_file.exists())

                started = time.monotonic()
                store.mark_status_published(
                    job_id="job_123",
                    notification_target="telegram:12345",
                    message="working",
                    message_id="status-1",
                    tick=1,
                )
                elapsed = time.monotonic() - started
            finally:
                process.wait(timeout=5)

            self.assertGreaterEqual(elapsed, 0.25)

    def test_notifier_sends_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_failed(job["job_id"], RuntimeError("media file is damaged"))
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append((target, message)),
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Broken Movie",
                notification_target="telegram:12345",
                language="en",
            )

            notifier.poll_once()

            self.assertEqual(
                sent,
                [("telegram:12345", "Subtitle processing failed: Broken Movie\nReason: media file is damaged")],
            )

    def test_notifier_keeps_watch_pending_when_send_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_succeeded(job["job_id"], {"ok": True})
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")

            def fail_send(_target, _message):
                raise RuntimeError("send failed")

            notifier = JobCompletionNotifier(store=notification_store, send_message=fail_send)
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Retry Movie",
                notification_target="telegram:12345",
                language="en",
            )

            notifier.poll_once()

            watches = notification_store.pending_watches()
            self.assertEqual(len(watches), 1)
            self.assertEqual(watches[0]["error_count"], 1)
            self.assertEqual(watches[0]["last_error"], "send failed")

    def test_notifier_sends_confirmation_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_needs_confirmation(job["job_id"], {"action": "confirm_low_confidence_subtitle"})
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append((target, message)),
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Risky Match",
                notification_target="telegram:12345",
                language="en",
            )

            notifier.poll_once()

            self.assertEqual(
                sent,
                [
                    (
                        "telegram:12345",
                        "I found a possible subtitle for Risky Match, but the timing may not match perfectly. "
                        "Please confirm if you want to try it.",
                    )
                ],
            )

    def test_resolve_notification_target_defaults_to_requester_id(self):
        self.assertEqual(resolve_notification_target(None, "telegram:12345"), "telegram:12345")
        self.assertEqual(resolve_notification_target("telegram:99999", "telegram:12345"), "telegram:99999")
        self.assertIsNone(resolve_notification_target(None, "friend-a"))

    def test_telegram_bot_token_prefers_profile_env_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            profile = Path(tmp) / "profile"
            (home / ".hermes").mkdir(parents=True)
            profile.mkdir()
            (home / ".hermes" / ".env").write_text("TELEGRAM_BOT_TOKEN=global-token\n", encoding="utf-8")
            (profile / ".env").write_text("TELEGRAM_BOT_TOKEN=profile-token\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "HERMES_HOME": str(home / ".hermes"),
                    "MST_HERMES_ENV_PATH": str(profile / ".env"),
                },
                clear=False,
            ):
                with patch.dict(os.environ, {"MST_TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_TOKEN": ""}, clear=False):
                    self.assertEqual(notifications._telegram_bot_token(), "profile-token")

    def test_status_message_falls_back_to_hermes_when_telegram_token_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_env = Path(tmp) / "missing.env"
            with patch.dict(
                os.environ,
                {
                    "MST_TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "MST_HERMES_ENV_PATH": str(missing_env),
                },
                clear=False,
            ), patch.object(notifications, "send_hermes_message") as send:
                message_id = notifications.send_status_message_from_env(
                    "telegram:12345",
                    "mpilot-subtitle:job_123",
                    "字幕处理中",
                )

            self.assertIsNone(message_id)
            send.assert_called_once_with("telegram:12345", "字幕处理中")

    def test_status_message_raises_when_telegram_send_fails_with_token(self):
        with patch.dict(
            os.environ,
            {
                "MST_TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_BOT_TOKEN": "",
            },
            clear=False,
        ), patch.object(
            notifications,
            "_send_or_edit_telegram_status_message",
            side_effect=RuntimeError("telegram unavailable"),
        ), patch.object(notifications, "send_hermes_message") as send:
            with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
                notifications.send_status_message_from_env(
                    "telegram:12345",
                    "mpilot-subtitle:job_123",
                    "字幕处理中",
                )

        send.assert_not_called()

    def test_send_hermes_message_times_out(self):
        with patch.dict(
            os.environ,
            {
                "MST_HERMES_BIN": "hermes",
                "MST_HERMES_SEND_TIMEOUT_SECONDS": "3",
            },
            clear=False,
        ), patch.object(
            notifications.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["hermes"], 3),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "hermes send timed out after 3"):
                notifications.send_hermes_message("telegram:12345", "hello")

        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_telegram_status_edit_rate_limit_raises_without_sending_replacement(self):
        calls = []

        def fake_post(_token, method, payload):
            calls.append((method, payload))
            raise RuntimeError("Too Many Requests: retry after 30")

        with patch.object(notifications, "_telegram_api_post", side_effect=fake_post):
            with self.assertRaisesRegex(RuntimeError, "Too Many Requests"):
                notifications._send_or_edit_telegram_status_message(
                    token="token",
                    chat_id="123",
                    thread_id=None,
                    message="working",
                    message_id="456",
                )

        self.assertEqual([call[0] for call in calls], ["editMessageText"])

    def test_telegram_status_edit_missing_message_sends_replacement(self):
        calls = []

        def fake_post(_token, method, payload):
            calls.append((method, payload))
            if method == "editMessageText":
                raise RuntimeError("Bad Request: message to edit not found")
            return {"ok": True, "result": {"message_id": 789}}

        with patch.object(notifications, "_telegram_api_post", side_effect=fake_post):
            message_id = notifications._send_or_edit_telegram_status_message(
                token="token",
                chat_id="123",
                thread_id=None,
                message="working",
                message_id="456",
            )

        self.assertEqual(message_id, "789")
        self.assertEqual([call[0] for call in calls], ["editMessageText", "sendMessage"])

    def test_reregistering_notified_watch_revives_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobNotificationStore(Path(tmp) / "watches.json")
            store.upsert_watch(
                job_id="job_1",
                job_store_dir=Path(tmp) / "jobs",
                notification_target="telegram:1",
            )
            store.mark_status_published(
                job_id="job_1",
                notification_target="telegram:1",
                message="running",
                message_id="42",
                tick=1,
            )
            store.mark_notified(
                job_id="job_1",
                notification_target="telegram:1",
                terminal_status="needs_confirmation",
            )
            self.assertEqual(store.pending_watches(), [])

            revived = store.upsert_watch(
                job_id="job_1",
                job_store_dir=Path(tmp) / "jobs",
                notification_target="telegram:1",
            )

            self.assertIsNone(revived["notified_at"])
            self.assertIsNone(revived["terminal_status"])
            self.assertIsNone(revived["terminal_notification_claimed_at"])
            self.assertEqual(revived["error_count"], 0)
            self.assertEqual(len(store.pending_watches()), 1)
            tracking = revived["status_tracking"]
            self.assertEqual(tracking.get("message_id"), "42")
            self.assertNotIn("last_message", tracking)

    def test_confirmed_job_sends_completion_notice_after_reregistration(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_needs_confirmation(job["job_id"], {"action": "confirm_low_confidence_subtitle"})
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            sent = []
            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: sent.append(message),
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Confirm Movie",
                notification_target="telegram:1",
                language="zh",
            )

            notifier.poll_once()
            self.assertEqual(len(sent), 1)
            self.assertIn("请回复确认", sent[0])

            # The user confirms: job_start re-registers the same watch and the
            # job runs again under the same job_id.
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Confirm Movie",
                notification_target="telegram:1",
                language="zh",
            )
            job_store.mark_running(job["job_id"])
            job_store.mark_succeeded(job["job_id"], {"ok": True})

            notifier.poll_once()
            self.assertEqual(len(sent), 2)
            self.assertIn("字幕已制作完成", sent[1])

    def test_claim_terminal_notification_expires_stale_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watches.json"
            store = JobNotificationStore(path)
            store.upsert_watch(
                job_id="job_1",
                job_store_dir=Path(tmp) / "jobs",
                notification_target="telegram:1",
            )

            first = store.claim_terminal_notification(
                job_id="job_1",
                notification_target="telegram:1",
                terminal_status="succeeded",
            )
            self.assertIsNotNone(first)
            second = store.claim_terminal_notification(
                job_id="job_1",
                notification_target="telegram:1",
                terminal_status="succeeded",
            )
            self.assertIsNone(second)

            # Simulate a sender that died after claiming: an old claim must not
            # block the terminal notification forever.
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["watches"][0]["terminal_notification_claimed_at"] = "2020-01-01T00:00:00Z"
            path.write_text(json.dumps(payload), encoding="utf-8")

            reclaimed = store.claim_terminal_notification(
                job_id="job_1",
                notification_target="telegram:1",
                terminal_status="succeeded",
            )
            self.assertIsNotNone(reclaimed)

    def test_non_editable_status_transport_sends_one_running_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_store = JobStore(Path(tmp) / "jobs")
            job = job_store.create("translate-plex", sample_request())
            job_store.mark_running(job["job_id"])
            job_store.mark_progress(
                job["job_id"],
                {"stage": "searching_online_subtitles", "message": "Searching online subtitles."},
            )
            notification_store = JobNotificationStore(Path(tmp) / "watches.json")
            status_updates = []

            def send_status(target, status_key, message, message_id=None):
                status_updates.append(message)
                return None

            notifier = JobCompletionNotifier(
                store=notification_store,
                send_message=lambda target, message: None,
                send_status_message=send_status,
            )
            notification_store.upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                title="Plain Movie",
                notification_target="telegram:1",
                language="en",
            )

            notifier.poll_once()
            job_store.mark_progress(
                job["job_id"],
                {
                    "stage": "translating",
                    "message": "Translating.",
                    "details": {"total_chunks": 4, "completed_chunks": 1},
                },
            )
            notifier.poll_once()

            self.assertEqual(len(status_updates), 1)

    def test_run_notification_daemon_waits_for_singleton_lock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "daemon.lock"
            wake_path = Path(tmp) / "wake"
            notifier = JobCompletionNotifier(
                store=JobNotificationStore(Path(tmp) / "watches.json"),
                send_message=lambda target, message: None,
            )

            holder = notifications.acquire_notification_daemon_lock(lock_path)
            self.assertIsNotNone(holder)

            blocked = notifications.run_notification_daemon(
                notifier=notifier,
                lock_path=lock_path,
                wake_path=wake_path,
                run_once=True,
                lock_acquire_timeout_seconds=0.0,
            )
            self.assertEqual(blocked["status"], "already_running")

            release_thread = threading.Thread(target=lambda: (time.sleep(0.4), holder.close()))
            release_thread.start()
            try:
                result = notifications.run_notification_daemon(
                    notifier=notifier,
                    lock_path=lock_path,
                    wake_path=wake_path,
                    run_once=True,
                    lock_acquire_timeout_seconds=5.0,
                )
            finally:
                release_thread.join()
            self.assertEqual(result["status"], "ran_once")

    def test_corrupt_watch_store_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watches.json"
            path.write_text("{not valid json", encoding="utf-8")
            store = JobNotificationStore(path)

            self.assertEqual(store.pending_watches(), [])

            corrupt_path = Path(tmp) / "watches.json.corrupt"
            self.assertTrue(corrupt_path.exists())
            self.assertEqual(corrupt_path.read_text(encoding="utf-8"), "{not valid json")
            self.assertFalse(path.exists())

    def test_notification_daemon_lock_is_singleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "notify-daemon.lock"

            first = notifications.acquire_notification_daemon_lock(lock_path)
            self.assertIsNotNone(first)
            self.addCleanup(first.close)

            second = notifications.acquire_notification_daemon_lock(lock_path)
            self.assertIsNone(second)

            first.close()
            third = notifications.acquire_notification_daemon_lock(lock_path)
            self.assertIsNotNone(third)
            third.close()

    def test_start_notification_daemon_from_env_spawns_detached_command(self):
        captured = {}

        class FakeProcess:
            pid = 2468

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return FakeProcess()

        process = notifications.start_notification_daemon_from_env(popen=fake_popen)

        self.assertEqual(process.pid, 2468)
        self.assertEqual(captured["command"][:3], [sys.executable, "-m", "mpilot.subtitles"])
        self.assertIn("notify-daemon", captured["command"])
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertTrue(captured["kwargs"]["close_fds"])

    def test_notification_paths_prefer_mpilot_env_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "MPILOT_SUBTITLE_JOB_NOTIFICATION_WATCHES_PATH": str(root / "mpilot-watches.json"),
                "BABELARR_JOB_NOTIFICATION_WATCHES_PATH": str(root / "babelarr-watches.json"),
                "MST_JOB_NOTIFICATION_WATCHES_PATH": str(root / "legacy-watches.json"),
                "MPILOT_SUBTITLE_JOB_NOTIFICATION_DAEMON_LOCK_PATH": str(root / "mpilot.lock"),
                "BABELARR_JOB_NOTIFICATION_DAEMON_LOCK_PATH": str(root / "mpilot.subtitles.lock"),
                "MST_JOB_NOTIFICATION_DAEMON_LOCK_PATH": str(root / "legacy.lock"),
                "MPILOT_SUBTITLE_JOB_NOTIFICATION_WAKE_PATH": str(root / "mpilot.wake"),
                "BABELARR_JOB_NOTIFICATION_WAKE_PATH": str(root / "mpilot.subtitles.wake"),
                "MST_JOB_NOTIFICATION_WAKE_PATH": str(root / "legacy.wake"),
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(notifications.default_notification_watch_store_path(), root / "mpilot-watches.json")
                self.assertEqual(notifications.default_notification_daemon_lock_path(), root / "mpilot.lock")
                self.assertEqual(notifications.default_notification_wake_path(), root / "mpilot.wake")


if __name__ == "__main__":
    unittest.main()
